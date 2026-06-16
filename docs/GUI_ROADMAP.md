# GUI Roadmap — toward v1

Status and planned work for the cross-platform PySide6 GUI (`gui.py`) that sits on
top of the existing deployment logic. This is a living document; items in
**Proposed** are candidates for v1 and not yet committed to.

---

## Works today

- **Scan Interfaces** launch page — enumerates local NICs (`detect.py`) and probes
  each subnet's gateway via the unauthenticated `/api/system` endpoint, labelling
  detected devices and distinguishing **factory-default** vs **configured**
  (`isSetup`).
- **Connect / login dialog** — verifies the connection before navigating. For a
  factory device it runs **first-boot setup** (creates the admin account from
  `config/setup_config.yaml`, editable in the dialog) and logs in, retrying for the
  post-setup transition window. Falls back to login if the device is already
  configured.
- **Configure Devices** page — lists every device on the gateway (name, type, IP,
  state) with a per-switch port-profile dropdown. Gateway IP falls back to the
  connected host. Non-switch types are shown but marked unsupported.
- **Config editor** (`Edit profiles…`) — structured **Port Profiles** tab (add /
  duplicate / delete; name, native VLAN, tagged VLANs, PoE, STP). Round-trip YAML
  save preserves comments/structure (`config_io.py`).
- **Deploy** — currently a **dry-run simulation** only (logs what would be pushed).
- **`deploy_core.py`** — shared, console-free push primitives (VLAN sync, port-
  profile ensure, switch-profile resolution). `main.py` reuses these.

---

## Planned / decided (in progress)

1. **Real Deploy** via `deploy_core`, replacing the dry-run simulation:
   - Chain: **sync VLANs** (from `vlan_config.yaml`) → **ensure port profiles** on
     the controller → **apply per-port overrides** to each switch.
   - **Switch profile model**: each switch gets one reusable profile =
     a *default* port profile applied to all ports + *per-port exceptions*
     (e.g. uplink → trunk). Resolved against the device's real `port_table`.
     (`deploy_core.resolve_switch_overrides` already implements this.)
   - Guardrails: dry-run toggle, confirmation, safe ordering (access ports before
     trunk/uplink), and clear per-device results.

2. **Config editor — remaining tabs** (Port Profiles done):
   - **Switch Profiles** — default port profile + per-port exceptions, referencing
     the existing port profiles.
   - **VLANs / Networks (Gateway)** — edit `vlan_config.yaml` (id, name, subnet,
     DHCP range, DNS).
   - **WLANs / SSIDs (Access Points)** — edit SSID → VLAN/security. Note: pushing
     WLANs to APs also needs the `wlangroup`/`wlan` endpoints (see
     `docs/improvements.md` #10), which are not yet implemented.

3. **Cameras / NVR** — managed by **UniFi Protect**, a separate API
   (`/proxy/protect`), not the Network API this tool uses. Out of scope for now;
   investigate a Protect client as a future, separate effort. The standard the
   site uses is captured in `docs/CameraSettings.md`.

---

## New: requested features

### Export to CSV (Configure Devices page)

An **Export** button on the Configure Devices page that writes a CSV of all
devices currently listed, one row per device. Proposed columns:

| Column | Source (`/stat/device` field) | Notes |
|---|---|---|
| Device name | `name` | falls back to MAC |
| Device type | `type` → friendly label | Switch / AP / Gateway / Camera |
| Model | `model` | e.g. `USW24POE` |
| MAC | `mac` | |
| IP | `ip` | show the IP, or `DHCP` if the device uses DHCP |
| IP assignment | `config_network.type` | `static` vs `dhcp` — drives the IP column*; verify exact field on a real device |
| Firmware | `version` | |
| State | `state` → label | Connected / Disconnected / … |
| Adopted | `adopted` | |
| Uptime | `uptime` | seconds → human |
| Serial | `serial` | |

\* If `config_network.type == "dhcp"`, render the IP column as `DHCP`; if `static`,
render the actual IP. Confirm the field name against a live device during
implementation (UniFi has used `config_network` and `ip` on the device record).

Implementation note: `inventory.py` already collects/export device inventory
(`collect_inventory`, `generate_csv`). The GUI export should reuse that where it
fits rather than re-deriving the logic, extended with the static/DHCP distinction.

---

## Proposed for v1 (for review — not yet committed)

Ordered roughly by how much they define a usable v1:

1. **Device adoption in the GUI** *(high)* — the page lists devices but cannot
   adopt pending ones. A switch must be adopted before it can be configured. Wrap
   the CLI's adoption loop (`adopt_device` + state polling) with GUI progress.
2. **Packaging** *(high)* — PyInstaller build so the app ships as a standalone
   executable on Windows and Linux without a Python install. Important for handing
   v1 to a non-developer.
3. **Pre-deploy state check** *(medium)* — before pushing, flag devices in problem
   states (disconnected, adopt-failed, provisioning), mirroring the CLI preflight.
4. **Deploy progress + saved report** *(medium)* — progress feedback during the
   push and a written deployment report (device → profile → result) for the record.
5. **Save / load a deployment plan** *(medium)* — persist per-device profile
   assignments so a site is repeatable and reviewable before applying.
6. **Session resilience** *(low)* — detect an expired/invalid session and re-auth
   gracefully instead of failing a device load or deploy.
7. **Credential handling** *(low)* — optional OS keyring storage; today credentials
   live only in the dialog and are not persisted.

## Explicitly out of scope for v1

- Firmware upgrade from the GUI (CLI `upgrade_firmware.py` exists but the upgrade
  path itself is unresolved — see `docs/improvements.md`).
- Multi-site / multiple config sets.
- UniFi Protect (camera/NVR) configuration — needs the separate Protect API.
