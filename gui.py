#!/usr/bin/env python3
"""
gui.py - Cross-platform (Windows + Linux) GUI front-end for the UniFi Deploy Tool.

Navigation:
  • Scan Interfaces page (launch point) - lists the local machine's network
    interfaces and detects any UniFi device listening on each subnet.
  • Select a detected device -> a login dialog pops up. Leave the fields blank to
    let the tool try the device defaults. The connection is verified before the
    dialog accepts, and the authenticated session is kept alive.
  • The Configure Devices page lists every device on the gateway with a dropdown
    to choose a port profile per switch. "Deploy" currently simulates the push
    (dry-run); real API writes come once the config editor lands. The ✕ button at
    the top-right logs out and returns to the Scan Interfaces page.

Run:  python gui.py        (after: pip install -r requirements.txt)
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from ruamel.yaml.comments import CommentedMap, CommentedSeq

import config_io
import detect
from unifi_client import UniFiClient, UniFiConnectionError

PROJECT_DIR = Path(__file__).resolve().parent

# Factory default credentials (mirror main.DEFAULT_USERNAME / DEFAULT_PASSWORD).
DEFAULT_USERNAME = "ubnt"
DEFAULT_PASSWORD = "ubnt"

# Device types a port profile can be applied to today (switches).
APPLICABLE_TYPES = {"usw"}

DEVICE_TYPE_LABELS = {
    "usw": "Switch",
    "uap": "Access Point",
    "uvc": "Camera",
    "udm": "Gateway",
    "uxg": "Gateway",
    "ugw": "Gateway",
    "ucg": "Gateway",
}

STATE_LABELS = {
    0: "Disconnected",
    1: "Connected",
    2: "Pending Adoption",
    4: "Upgrading",
    5: "Provisioning",
    6: "Heartbeat Missed",
    7: "Adopting",
    10: "Adopt Failed",
}


def load_port_profiles() -> dict:
    """Return the port_profiles mapping {key: definition} from device_profiles.yaml."""
    try:
        data = config_io.load_device_profiles() or {}
    except Exception:   # noqa: BLE001 - missing/invalid file → empty set
        return {}
    return data.get("port_profiles", {}) or {}


# ---------------------------------------------------------------------------
# Background workers (keep the UI responsive during network I/O)
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    """Runs detect.scan() off the UI thread."""

    progress = Signal(int, int)        # done, total
    finished_scan = Signal(list)       # list of interface result dicts

    def run(self) -> None:
        results = detect.scan(progress_cb=lambda d, t: self.progress.emit(d, t))
        self.finished_scan.emit(results)


class ConnectWorker(QThread):
    """Validates the connection/credentials off the UI thread before navigating.

    For a configured device this performs a real login (provided credentials, or
    factory defaults when blank) and, on success, hands back the authenticated
    client so later pages can reuse the session. For a factory-default device
    there is no account yet, so it only confirms reachability (client is None).
    """

    done = Signal(bool, str, object)   # success, message, client | None

    def __init__(self, host: str, username: str, password: str, configured: bool) -> None:
        super().__init__()
        self.host = host
        self.username = username
        self.password = password
        self.configured = configured

    def run(self) -> None:
        client = UniFiClient(self.host, verify_ssl=False)
        try:
            if not self.configured:
                client.check_setup_status()   # reachability; raises if unreachable
                self.done.emit(
                    True,
                    "Device reachable — factory-default state. First-boot setup "
                    "will create the admin account during deployment.",
                    None,
                )
                return

            if self.username and self.password:
                user, pwd = self.username, self.password
            else:
                user, pwd = DEFAULT_USERNAME, DEFAULT_PASSWORD

            try:
                client.login(user, pwd)
            except UniFiConnectionError as exc:
                self.done.emit(False, f"Login failed: {exc}", None)
                return
            self.done.emit(True, f"Authenticated as '{user}'.", client)
        except UniFiConnectionError as exc:
            self.done.emit(False, f"Cannot reach {self.host}: {exc}", None)
        except Exception as exc:   # noqa: BLE001 - surface anything unexpected to the user
            self.done.emit(False, str(exc), None)


class DeviceLoadWorker(QThread):
    """Fetches the device list off the UI thread."""

    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, client: UniFiClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            self.loaded.emit(self.client.get_devices())
        except Exception as exc:   # noqa: BLE001
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# Login dialog (modal popup shown when a device is chosen)
# ---------------------------------------------------------------------------

class LoginDialog(QDialog):
    """Collects credentials and verifies the connection before accepting.

    Blank username/password fall back to the device defaults. The dialog only
    accepts (allowing navigation) once the connection has been confirmed; on
    failure it stays open and shows the error. On success the authenticated
    client (or None for a reachable factory device) is exposed as ``.client``.
    """

    def __init__(self, device_label: str, host: str, configured: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Device login")
        self.setModal(True)
        self._configured = configured
        self._worker: ConnectWorker | None = None
        self.client: UniFiClient | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{device_label}</b>"))

        form = QFormLayout()
        self.host_edit = QLineEdit(host)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("blank → try device defaults")
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText("blank → try device defaults")
        form.addRow("Host", self.host_edit)
        form.addRow("Username", self.user_edit)
        form.addRow("Password", self.pass_edit)
        layout.addLayout(form)

        hint = QLabel(
            "Leave username and password blank to try the factory/default "
            "credentials. The connection is verified before continuing."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Continue →")
        self.buttons.accepted.connect(self._verify_and_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.user_edit.setFocus()

    def values(self) -> tuple[str, str, str]:
        """Return (host, username, password) with surrounding whitespace stripped."""
        return (
            self.host_edit.text().strip(),
            self.user_edit.text().strip(),
            self.pass_edit.text(),
        )

    # -- connection verification ----------------------------------------

    def _set_busy(self, busy: bool) -> None:
        for w in (self.host_edit, self.user_edit, self.pass_edit, self.buttons):
            w.setEnabled(not busy)

    def _verify_and_accept(self) -> None:
        host, username, password = self.values()
        if not host:
            self.status.setText("<span style='color:#b00;'>Enter a host.</span>")
            return

        self._set_busy(True)
        self.status.setText("Connecting…")

        self._worker = ConnectWorker(host, username, password, self._configured)
        self._worker.done.connect(self._on_verified)
        self._worker.start()

    def _on_verified(self, success: bool, message: str, client) -> None:
        self._set_busy(False)
        if success:
            self.client = client
            self.status.setText(f"<span style='color:#0a0;'>{message}</span>")
            self.accept()
        else:
            self.status.setText(f"<span style='color:#b00;'>{message}</span>")


# ---------------------------------------------------------------------------
# Page 1 · Scan Interfaces (launch point)
# ---------------------------------------------------------------------------

class ScanPage(QWidget):
    """Lists interfaces and emits ``connect_requested`` with the chosen row."""

    connect_requested = Signal(dict)   # the selected interface result dict

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scan_worker: ScanWorker | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Scan Interfaces</h2>"))
        layout.addWidget(QLabel(
            "Select a detected UniFi device to begin deployment."
        ))

        top = QHBoxLayout()
        self.scan_btn = QPushButton("Scan interfaces")
        self.scan_btn.clicked.connect(self.start_scan)
        self.scan_status = QLabel("")
        top.addWidget(self.scan_btn)
        top.addWidget(self.scan_status, stretch=1)
        layout.addLayout(top)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Interface", "IP address", "Detected device"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._update_connect_enabled)
        self.table.cellDoubleClicked.connect(lambda *_: self._emit_connect())
        layout.addWidget(self.table, stretch=1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.connect_btn = QPushButton("Connect to selected device →")
        self.connect_btn.setEnabled(False)
        self.connect_btn.clicked.connect(self._emit_connect)
        bottom.addWidget(self.connect_btn)
        layout.addLayout(bottom)

    def start_scan(self) -> None:
        if self._scan_worker and self._scan_worker.isRunning():
            return
        self.scan_btn.setEnabled(False)
        self.connect_btn.setEnabled(False)
        self.scan_status.setText("Scanning…")
        self.table.setRowCount(0)

        self._scan_worker = ScanWorker()
        self._scan_worker.progress.connect(
            lambda d, t: self.scan_status.setText(f"Scanning… {d}/{t} interfaces")
        )
        self._scan_worker.finished_scan.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_done(self, results: list) -> None:
        self.scan_btn.setEnabled(True)
        found = sum(1 for r in results if r.get("device"))
        self.scan_status.setText(
            f"Found {len(results)} interface(s), {found} with a UniFi device."
        )

        self.table.setRowCount(len(results))
        for row, r in enumerate(results):
            device = r.get("device")
            if device:
                state = "configured" if device.get("configured") else "factory-default"
                label = f"{device['model']} ({state})"
            else:
                label = "—"

            name_item = QTableWidgetItem(r["name"])
            ip_item = QTableWidgetItem(r["ipv4"])
            dev_item = QTableWidgetItem(label)

            if device:
                # Stash the whole result on the row so connecting has everything.
                name_item.setData(Qt.UserRole, r)
                for item in (name_item, ip_item, dev_item):
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)

            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, ip_item)
            self.table.setItem(row, 2, dev_item)

        for row, r in enumerate(results):
            if r.get("device"):
                self.table.selectRow(row)
                break
        self._update_connect_enabled()

    def _selected_result(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        name_item = self.table.item(row, 0)
        return name_item.data(Qt.UserRole) if name_item else None

    def _update_connect_enabled(self) -> None:
        self.connect_btn.setEnabled(self._selected_result() is not None)

    def _emit_connect(self) -> None:
        result = self._selected_result()
        if result:
            self.connect_requested.emit(result)


# ---------------------------------------------------------------------------
# Config editor (modal) — structured editing of the YAML profiles
# ---------------------------------------------------------------------------

class ProfileEditorDialog(QDialog):
    """Structured editor for the config profiles.

    Tabbed by device class (Switches / Gateway / APs / Cameras). Only the Port
    Profiles tab (switches) is implemented today; the rest are placeholders that
    reserve space for VLANs, WLANs, and a future UniFi Protect integration.
    Saving writes back via ruamel round-trip so comments/structure are preserved.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit configuration profiles")
        self.setModal(True)
        self.resize(680, 440)
        self._loading = False

        layout = QVBoxLayout(self)

        try:
            self._doc = config_io.load_device_profiles()
        except Exception as exc:   # noqa: BLE001
            self._doc = None
            layout.addWidget(QLabel(f"Could not load device_profiles.yaml:\n{exc}"))
            box = QDialogButtonBox(QDialogButtonBox.Close)
            box.rejected.connect(self.reject)
            layout.addWidget(box)
            return

        if not isinstance(self._doc.get("port_profiles"), CommentedMap):
            self._doc["port_profiles"] = CommentedMap()
        self._profiles = self._doc["port_profiles"]

        tabs = QTabWidget()
        tabs.addTab(self._build_port_tab(), "Port Profiles (Switches)")
        tabs.addTab(self._placeholder(
            "VLANs / Networks — Gateway",
            "Editing VLANs (config/vlan_config.yaml) is coming next.",
        ), "VLANs (Gateway)")
        tabs.addTab(self._placeholder(
            "WLANs / SSIDs — Access Points",
            "Editing WLANs is coming next. Pushing them to APs also needs the "
            "wlangroup/wlan endpoints, which aren't implemented yet "
            "(see docs/improvements.md).",
        ), "WLANs (APs)")
        cam_idx = tabs.addTab(self._placeholder(
            "Cameras / NVR — UniFi Protect",
            "UniFi Protect devices use a separate API (/proxy/protect) and can't "
            "be configured through this tool yet. Flagged to investigate later.",
        ), "Cameras / NVR")
        tabs.setTabEnabled(cam_idx, False)
        layout.addWidget(tabs)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._reload_list()

    # -- tab builders ---------------------------------------------------

    @staticmethod
    def _placeholder(title: str, body: str) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        head = QLabel(f"<b>{title}</b>")
        text = QLabel(body)
        text.setWordWrap(True)
        text.setStyleSheet("color: gray;")
        v.addWidget(head)
        v.addWidget(text)
        v.addStretch(1)
        return w

    def _build_port_tab(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)

        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        left.addWidget(self.list)
        row = QHBoxLayout()
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        dup_btn = QPushButton("Duplicate")
        dup_btn.clicked.connect(self._duplicate)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete)
        row.addWidget(add_btn)
        row.addWidget(dup_btn)
        row.addWidget(del_btn)
        left.addLayout(row)
        h.addLayout(left, 1)

        self.form_wrap = QWidget()
        form = QFormLayout(self.form_wrap)
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._on_field_change)
        self.native_spin = QSpinBox()
        self.native_spin.setRange(1, 4094)
        self.native_spin.valueChanged.connect(self._on_field_change)
        self.tagged_edit = QLineEdit()
        self.tagged_edit.setPlaceholderText("e.g. 20, 30")
        self.tagged_edit.textChanged.connect(self._on_field_change)
        self.poe_check = QCheckBox()
        self.poe_check.toggled.connect(self._on_field_change)
        self.stp_check = QCheckBox()
        self.stp_check.toggled.connect(self._on_field_change)
        form.addRow("Name", self.name_edit)
        form.addRow("Native VLAN", self.native_spin)
        form.addRow("Tagged VLANs", self.tagged_edit)
        form.addRow("PoE enabled", self.poe_check)
        form.addRow("Spanning tree", self.stp_check)
        self.err = QLabel("")
        self.err.setWordWrap(True)
        self.err.setStyleSheet("color:#b00;")
        form.addRow("", self.err)
        h.addWidget(self.form_wrap, 2)
        self.form_wrap.setEnabled(False)
        return w

    # -- profile list management ----------------------------------------

    def _reload_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for key, prof in self._profiles.items():
            item = QListWidgetItem((prof or {}).get("name", key))
            item.setData(Qt.UserRole, key)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.form_wrap.setEnabled(False)

    def _current_key(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _unique_key(self, base: str) -> str:
        base = base or "profile"
        key, i = base, 2
        while key in self._profiles:
            key, i = f"{base}_{i}", i + 1
        return key

    def _on_select(self, current, _previous) -> None:
        if current is None:
            self.form_wrap.setEnabled(False)
            return
        prof = self._profiles.get(current.data(Qt.UserRole)) or {}
        self._loading = True
        self.name_edit.setText(str(prof.get("name", current.data(Qt.UserRole))))
        self.native_spin.setValue(int(prof.get("native_vlan_id", 1) or 1))
        tagged = prof.get("tagged_vlan_ids") or []
        self.tagged_edit.setText(", ".join(str(v) for v in tagged))
        self.poe_check.setChecked(bool(prof.get("poe_enabled", False)))
        self.stp_check.setChecked(bool(prof.get("spanning_tree", True)))
        self._loading = False
        self.err.setText("")
        self.form_wrap.setEnabled(True)

    @staticmethod
    def _parse_tagged(text: str) -> list[int]:
        out: list[int] = []
        for part in text.replace(",", " ").split():
            n = int(part)
            if not 1 <= n <= 4094:
                raise ValueError(f"{n} out of range")
            out.append(n)
        return out

    def _on_field_change(self, *_) -> None:
        if self._loading:
            return
        key = self._current_key()
        if key is None:
            return
        prof = self._profiles.get(key)
        if not isinstance(prof, CommentedMap):
            prof = CommentedMap()
            self._profiles[key] = prof

        prof["name"] = self.name_edit.text()
        prof["native_vlan_id"] = self.native_spin.value()
        try:
            seq = CommentedSeq(self._parse_tagged(self.tagged_edit.text()))
            seq.fa.set_flow_style()
            prof["tagged_vlan_ids"] = seq
            self.err.setText("")
        except ValueError:
            self.err.setText("Tagged VLANs must be comma-separated numbers (1–4094).")
        prof["poe_enabled"] = self.poe_check.isChecked()
        prof["spanning_tree"] = self.stp_check.isChecked()

        item = self.list.currentItem()
        if item:
            item.setText(self.name_edit.text() or key)

    def _add(self) -> None:
        key = self._unique_key("new_profile")
        prof = CommentedMap()
        prof["name"] = "New Profile"
        prof["native_vlan_id"] = 1
        seq = CommentedSeq()
        seq.fa.set_flow_style()
        prof["tagged_vlan_ids"] = seq
        prof["poe_enabled"] = False
        prof["spanning_tree"] = True
        self._profiles[key] = prof
        item = QListWidgetItem(prof["name"])
        item.setData(Qt.UserRole, key)
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _duplicate(self) -> None:
        from copy import deepcopy
        key = self._current_key()
        if key is None:
            return
        src = self._profiles.get(key) or CommentedMap()
        new_key = self._unique_key(f"{key}_copy")
        prof = deepcopy(src)
        prof["name"] = f"{src.get('name', key)} (copy)"
        self._profiles[new_key] = prof
        item = QListWidgetItem(prof["name"])
        item.setData(Qt.UserRole, new_key)
        self.list.addItem(item)
        self.list.setCurrentItem(item)

    def _delete(self) -> None:
        key = self._current_key()
        if key is None:
            return
        self._profiles.pop(key, None)
        self.list.takeItem(self.list.currentRow())
        if self.list.count() == 0:
            self.form_wrap.setEnabled(False)

    def _save(self) -> None:
        try:
            config_io.save_device_profiles(self._doc)
        except Exception as exc:   # noqa: BLE001
            self.err.setText(f"Save failed: {exc}")
            return
        self.accept()


# ---------------------------------------------------------------------------
# Page 2 · Configure Devices
# ---------------------------------------------------------------------------

class ConfigPage(QWidget):
    """Lists gateway devices and assigns a port profile per switch, then deploys."""

    back_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._client: UniFiClient | None = None
        self._loader: DeviceLoadWorker | None = None
        self._profiles = load_port_profiles()          # {key: definition}
        self._rows: list[tuple[dict, QComboBox]] = []   # applicable (device, combo)

        layout = QVBoxLayout(self)

        # Header: title (left) + refresh + close/back (top-right)
        header = QHBoxLayout()
        self.title = QLabel("<h2>Configure Devices</h2>")
        header.addWidget(self.title)
        header.addStretch(1)
        self.edit_btn = QPushButton("Edit profiles…")
        self.edit_btn.setToolTip("Edit the port profiles applied to switches")
        self.edit_btn.clicked.connect(self._open_editor)
        header.addWidget(self.edit_btn, alignment=Qt.AlignTop)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Re-fetch the device list from the gateway")
        self.refresh_btn.clicked.connect(self._load_devices)
        header.addWidget(self.refresh_btn, alignment=Qt.AlignTop)
        self.back_btn = QPushButton("✕")
        self.back_btn.setToolTip("Log out and return to Scan Interfaces")
        self.back_btn.setFixedSize(32, 28)
        self.back_btn.clicked.connect(self.back_requested.emit)
        header.addWidget(self.back_btn, alignment=Qt.AlignTop)
        layout.addLayout(header)

        self.status = QLabel("")
        layout.addWidget(self.status)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Device", "Type", "IP", "State", "Config profile"]
        )
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        layout.addWidget(self.table, stretch=1)

        controls = QHBoxLayout()
        self.deploy_btn = QPushButton("Deploy")
        self.deploy_btn.setEnabled(False)
        self.deploy_btn.clicked.connect(self.deploy)
        controls.addWidget(self.deploy_btn)
        controls.addWidget(QLabel("Assign a profile to every switch to enable Deploy."))
        controls.addStretch(1)
        layout.addLayout(controls)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("monospace"))
        self.output.setMaximumHeight(200)
        layout.addWidget(self.output)

    # -- session handoff -------------------------------------------------

    def set_client(self, client: UniFiClient | None, label: str) -> None:
        self._client = client
        self.title.setText(f"<h2>Configure Devices</h2><span>{label}</span>")
        self.output.clear()
        self._load_devices()

    # -- device loading --------------------------------------------------

    def _load_devices(self) -> None:
        self._rows = []
        self.table.setRowCount(0)
        self.deploy_btn.setEnabled(False)

        if self._client is None:
            self.status.setText(
                "No authenticated session (factory-default device). "
                "Complete first-boot setup before configuring devices."
            )
            self.refresh_btn.setEnabled(False)
            return

        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading devices…")
        self._loader = DeviceLoadWorker(self._client)
        self._loader.loaded.connect(self._on_devices_loaded)
        self._loader.failed.connect(self._on_load_failed)
        self._loader.start()

    def _on_load_failed(self, message: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Failed to load devices: {message}")

    def _on_devices_loaded(self, devices: list) -> None:
        self.refresh_btn.setEnabled(True)
        self._rows = []
        self.table.setRowCount(len(devices))

        for row, dev in enumerate(devices):
            dtype = dev.get("type", "")
            name = dev.get("name") or dev.get("mac", "?")
            state = STATE_LABELS.get(dev.get("state"), str(dev.get("state", "?")))

            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(DEVICE_TYPE_LABELS.get(dtype, dtype or "?")))
            self.table.setItem(row, 2, QTableWidgetItem(dev.get("ip", "—")))
            self.table.setItem(row, 3, QTableWidgetItem(state))

            combo = QComboBox()
            if dtype in APPLICABLE_TYPES and self._profiles:
                combo.addItem("— select —", None)
                for key, definition in self._profiles.items():
                    combo.addItem(definition.get("name", key), key)
                combo.currentIndexChanged.connect(self._update_deploy_enabled)
                self._rows.append((dev, combo))
            else:
                reason = "no profiles loaded" if dtype in APPLICABLE_TYPES else "not yet supported"
                combo.addItem(f"n/a ({reason})", None)
                combo.setEnabled(False)
            self.table.setCellWidget(row, 4, combo)

        configurable = len(self._rows)
        self.status.setText(
            f"{len(devices)} device(s) · {configurable} configurable switch(es)."
        )
        self._update_deploy_enabled()

    def _update_deploy_enabled(self) -> None:
        ready = bool(self._rows) and all(
            combo.currentData() is not None for _, combo in self._rows
        )
        self.deploy_btn.setEnabled(ready)

    # -- profile editing -------------------------------------------------

    def _open_editor(self) -> None:
        dialog = ProfileEditorDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.reload_profiles()

    def reload_profiles(self) -> None:
        """Re-read profiles and refresh each switch dropdown, keeping selections."""
        self._profiles = load_port_profiles()
        for _dev, combo in self._rows:
            prev_key = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— select —", None)
            for key, definition in self._profiles.items():
                combo.addItem(definition.get("name", key), key)
            idx = combo.findData(prev_key)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
        self._update_deploy_enabled()

    # -- deploy (dry-run simulation) ------------------------------------

    def deploy(self) -> None:
        self.output.clear()
        self.output.appendPlainText(
            "[DRY RUN] Simulating deployment — no changes will be pushed to the gateway.\n"
        )
        for dev, combo in self._rows:
            key = combo.currentData()
            definition = self._profiles.get(key, {})
            name = dev.get("name") or dev.get("mac", "?")
            native = definition.get("native_vlan_id", "?")
            tagged = definition.get("tagged_vlan_ids", []) or []
            self.output.appendPlainText(
                f"  • {name} ({dev.get('mac', '?')}) → '{definition.get('name', key)}'"
                f"  [native VLAN {native}, tagged {tagged}]"
            )
        self.output.appendPlainText(
            f"\nSimulated {len(self._rows)} switch(es). "
            "Live push will be wired up after the config editor."
        )


# ---------------------------------------------------------------------------
# Main window — hosts the pages and owns the authenticated session
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("UniFi Deploy Tool")
        self.resize(900, 760)

        self._client: UniFiClient | None = None

        self.stack = QStackedWidget()
        self.scan_page = ScanPage()
        self.config_page = ConfigPage()
        self.stack.addWidget(self.scan_page)
        self.stack.addWidget(self.config_page)
        self.setCentralWidget(self.stack)

        self.scan_page.connect_requested.connect(self._open_login)
        self.config_page.back_requested.connect(self._back_to_scan)

        # The scan page is the launch point — kick off a scan immediately.
        self.scan_page.start_scan()

    def _open_login(self, result: dict) -> None:
        device = result.get("device") or {}
        host = device.get("ip") or detect.DEFAULT_GATEWAY
        label = f"{device.get('model', 'UniFi device')} @ {host}"
        configured = bool(device.get("configured", True))

        dialog = LoginDialog(label, host, configured, self)
        if dialog.exec() != QDialog.Accepted:
            return

        self._set_client(dialog.client)
        self.config_page.set_client(self._client, label)
        self.stack.setCurrentWidget(self.config_page)

    def _set_client(self, client: UniFiClient | None) -> None:
        """Adopt a new session, logging out any previous one first."""
        if self._client is not None and self._client is not client:
            try:
                self._client.logout()
            except Exception:   # noqa: BLE001 - best-effort cleanup
                pass
        self._client = client

    def _back_to_scan(self) -> None:
        self._set_client(None)
        self.stack.setCurrentWidget(self.scan_page)

    def closeEvent(self, event) -> None:   # noqa: N802 - Qt override
        self._set_client(None)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
