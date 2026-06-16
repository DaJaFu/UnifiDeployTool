#!/usr/bin/env python3
"""
gui.py - Cross-platform (Windows + Linux) GUI front-end for the UniFi Deploy Tool.

Navigation:
  • Scan Interfaces page (launch point) - lists the local machine's network
    interfaces and detects any UniFi device listening on each subnet.
  • Select a detected device -> a login dialog pops up. Leave the fields blank to
    let the tool try the device defaults.
  • The app then switches to the Deployment page, where the standard flow runs
    (main.py in non-interactive --yes mode) with live log output. The ✕ button at
    the top-right returns to the Scan Interfaces page at any time.

Run:  python gui.py        (after: pip install -r requirements.txt)
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import detect
from unifi_client import UniFiClient, UniFiConnectionError

PROJECT_DIR = Path(__file__).resolve().parent

# Factory default credentials (mirror main.DEFAULT_USERNAME / DEFAULT_PASSWORD).
DEFAULT_USERNAME = "ubnt"
DEFAULT_PASSWORD = "ubnt"


# ---------------------------------------------------------------------------
# Background interface scan (keeps the UI responsive)
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

    For a configured device this attempts an actual login (provided credentials,
    or factory defaults when blank). For a factory-default device there is no
    account yet, so it only confirms the device is reachable — first-boot setup
    will create the admin account during deployment.
    """

    done = Signal(bool, str)   # success, message

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
                )
                return

            if self.username and self.password:
                user, pwd = self.username, self.password
            else:
                user, pwd = DEFAULT_USERNAME, DEFAULT_PASSWORD

            try:
                client.login(user, pwd)
            except UniFiConnectionError as exc:
                self.done.emit(False, f"Login failed: {exc}")
                return
            client.logout()
            self.done.emit(True, f"Authenticated as '{user}'.")
        except UniFiConnectionError as exc:
            self.done.emit(False, f"Cannot reach {self.host}: {exc}")
        except Exception as exc:   # noqa: BLE001 - surface anything unexpected to the user
            self.done.emit(False, str(exc))


# ---------------------------------------------------------------------------
# Login dialog (modal popup shown when a device is chosen)
# ---------------------------------------------------------------------------

class LoginDialog(QDialog):
    """Collects credentials and verifies the connection before accepting.

    Blank username/password fall back to the device defaults. The dialog only
    accepts (allowing navigation to the deployment page) once the connection has
    been confirmed; on failure it stays open and shows the error.
    """

    def __init__(self, device_label: str, host: str, configured: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Device login")
        self.setModal(True)
        self._configured = configured
        self._worker: ConnectWorker | None = None

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

    def _on_verified(self, success: bool, message: str) -> None:
        self._set_busy(False)
        if success:
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
# Page 2 · Deployment
# ---------------------------------------------------------------------------

class DeployPage(QWidget):
    """Runs main.py for the chosen device and streams its output."""

    back_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._host = ""
        self._username = ""
        self._password = ""

        layout = QVBoxLayout(self)

        # Header: device label (left) + close/back button (top-right)
        header = QHBoxLayout()
        self.title = QLabel("<h2>Deployment</h2>")
        header.addWidget(self.title)
        header.addStretch(1)
        self.back_btn = QPushButton("✕")
        self.back_btn.setToolTip("Return to Scan Interfaces")
        self.back_btn.setFixedSize(32, 28)
        self.back_btn.clicked.connect(self._go_back)
        header.addWidget(self.back_btn, alignment=Qt.AlignTop)
        layout.addLayout(header)

        # Options
        opts = QHBoxLayout()
        self.setup_check = QCheckBox("First-boot setup (config/setup_config.yaml)")
        self.dryrun_check = QCheckBox("Dry run (simulate, no changes)")
        opts.addWidget(self.setup_check)
        opts.addWidget(self.dryrun_check)
        opts.addStretch(1)
        layout.addLayout(opts)

        # Run controls
        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start deployment")
        self.start_btn.clicked.connect(self.start_deployment)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_deployment)
        self.run_status = QLabel("Idle.")
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addWidget(self.run_status, stretch=1)
        layout.addLayout(buttons)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace"))
        layout.addWidget(self.log, stretch=1)

    # -- configuration from the login step ------------------------------

    def configure(self, host: str, username: str, password: str, label: str) -> None:
        self._host = host
        self._username = username
        self._password = password
        self.title.setText(f"<h2>Deployment</h2><span>{label}</span>")
        self.run_status.setText("Idle.")
        self.log.clear()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # -- navigation ------------------------------------------------------

    def _is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def _go_back(self) -> None:
        if self._is_running():
            reply = QMessageBox.question(
                self,
                "Stop deployment?",
                "A deployment is still running. Stop it and return to Scan Interfaces?",
            )
            if reply != QMessageBox.Yes:
                return
            self._process.kill()
            self._process.waitForFinished(2000)
        self.back_requested.emit()

    # -- running main.py -------------------------------------------------

    def start_deployment(self) -> None:
        if self._is_running() or not self._host:
            return

        args = ["main.py", "--host", self._host, "--yes"]
        if self._username:
            args += ["--username", self._username]
        if self._password:
            args += ["--password", self._password]
        if self.setup_check.isChecked():
            args += ["--setup"]
        if self.dryrun_check.isChecked():
            args += ["--dry-run"]

        self.log.clear()
        self._append_log(f"$ {sys.executable} {' '.join(args)}\n")

        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(PROJECT_DIR))
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_process_output)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(
            lambda err: self._append_log(f"\n[process error: {err}]\n")
        )

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.run_status.setText("Running…")
        self._process.start(sys.executable, args)

    def stop_deployment(self) -> None:
        if self._is_running():
            self._process.kill()

    def _on_process_output(self) -> None:
        data = bytes(self._process.readAllStandardOutput()).decode(errors="replace")
        self._append_log(data)

    def _on_process_finished(self, exit_code: int, _status) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if exit_code == 0:
            self.run_status.setText("Finished successfully.")
        else:
            self.run_status.setText(f"Exited with code {exit_code}.")

    def _append_log(self, text: str) -> None:
        self.log.moveCursor(self.log.textCursor().End)
        self.log.insertPlainText(text)
        self.log.moveCursor(self.log.textCursor().End)


# ---------------------------------------------------------------------------
# Main window — hosts the two pages
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("UniFi Deploy Tool")
        self.resize(840, 720)

        self.stack = QStackedWidget()
        self.scan_page = ScanPage()
        self.deploy_page = DeployPage()
        self.stack.addWidget(self.scan_page)
        self.stack.addWidget(self.deploy_page)
        self.setCentralWidget(self.stack)

        self.scan_page.connect_requested.connect(self._open_login)
        self.deploy_page.back_requested.connect(
            lambda: self.stack.setCurrentWidget(self.scan_page)
        )

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
        host, username, password = dialog.values()
        if not host:
            return
        self.deploy_page.configure(host, username, password, label)
        self.stack.setCurrentWidget(self.deploy_page)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
