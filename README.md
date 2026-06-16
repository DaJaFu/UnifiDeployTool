# UniFi Deploy Tool

Automated deployment tool for UniFi Gateway (Max/Ultra) networks. Handles first-boot setup, VLAN creation, device adoption, port profile assignment, and inventory export — all from a single CLI command.

---

## What It Does

Runs a 6-step deployment sequence against a UniFi OS gateway:

1. **Initial Setup** — Detects factory-default state and bootstraps the device via the `/api/setup` endpoint (no browser required)
2. **Connect** — Authenticates with credential fallback (setup creds → defaults → interactive prompt)
3. **Switch Adoption** — Iteratively discovers and adopts switches in dependency order
4. **Pre-flight Scan** — Read-only device discovery with state validation; confirms before writing anything
5. **VLAN Configuration** — Creates VLANs with DHCP, DNS, and gateway settings from `config/vlan_config.yaml`
6. **Device Adoption** — Adopts pending APs, switches, and cameras with state polling
7. **Port Profiles** — Creates port profiles and applies them to switches (access ports first, uplink last to prevent adoption loops)
8. **Inventory Export** — Generates JSON, CSV, and Excel reports to `output/`

Additional utilities:
- `factory_reset.py` — Safe ordered reset (APs → switches → gateway)
- `upgrade_firmware.py` — Firmware upgrade via SSH with automatic SSH management
- `test_ssh_toggle.py` — Test SSH enable/disable endpoints

---

## Installation

**Requirements:** Python 3.9+

```bash
git clone https://github.com/BC10NetworkTools/UnifiDeployTool.git
cd UnifiDeployTool
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP API client |
| `PyYAML` | Config file parsing |
| `rich` | CLI tables, progress bars, prompts |
| `paramiko` | SSH/SFTP for firmware upgrades |
| `openpyxl` | Excel inventory export |

---

## Configuration

Edit the YAML files in `config/` before running:

- **`config/setup_config.yaml`** — Admin credentials, device name, country code, timezone for first-boot setup
- **`config/vlan_config.yaml`** — VLAN definitions (ID, name, subnet, DHCP range, purpose)
- **`config/device_profiles.yaml`** — Port profiles and device type settings

---

## Usage

### GUI (Windows + Linux)

A cross-platform desktop front-end is available. It scans the local machine's
network interfaces, auto-detects any UniFi device listening on each interface's
subnet (labelling it with the product name, e.g. *UniFi Cloud Gateway Ultra*),
verifies the connection, then lets you assign a port profile per switch before
deploying.

```bash
# Debian/Ubuntu/Mint: system Python is externally managed (PEP 668), so use a venv
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python gui.py
```

**Linux prerequisite:** since Qt 6.5 the `xcb` platform plugin needs the
`libxcb-cursor0` system library. On a fresh Debian/Ubuntu/Mint machine the GUI
aborts with *"Could not load the Qt platform plugin xcb"* until it is installed:

```bash
sudo apt install libxcb-cursor0
```

Pick the interface with your gateway and click **Connect**; enter credentials in
the popup (or leave them blank to try the device defaults). The connection is
verified before continuing. The **Configure Devices** page then lists every
device on the gateway, with a dropdown to choose a port profile
(from `config/device_profiles.yaml`) for each switch. **Edit profiles…** opens a
structured editor for those port profiles (comments/structure are preserved on
save), and the dropdowns refresh when you close it. Press **Deploy** to apply.

> **Note:** Deploy currently runs a dry-run simulation (it logs what would be
> pushed per device); live API writes are in progress. The config editor today
> covers switch port profiles; VLANs (gateway) and WLANs (APs) are scaffolded as
> follow-ups, and Cameras/NVR need a separate UniFi Protect integration. The ✕ at
> the top-right logs out and returns to the scan page.

The full automated CLI flow (VLAN creation, adoption, port profiles, inventory)
remains available via `python main.py` — see **Main Deployment** below.

### Main Deployment

```bash
# Full deployment (interactive)
python main.py --host 192.168.1.1

# Dry run — reads device state without making any changes
python main.py --host 192.168.1.1 --dry-run

# First-boot setup + deployment
python main.py --host 192.168.1.1 --setup --setup-config config/setup_config.yaml

# With explicit credentials
python main.py --host 192.168.1.1 --username admin --password yourpassword

# Custom output directory
python main.py --host 192.168.1.1 --output-dir /path/to/reports

# Debug mode (verbose HTTP logging)
python main.py --host 192.168.1.1 --debug
```

### Factory Reset

Resets all adopted devices in safe order (APs → switches → gateway):

```bash
python factory_reset.py --host 192.168.1.1 --username admin --password yourpassword
```

### Firmware Upgrade

Upgrades gateway firmware via SSH. Place firmware `.bin` files in `firmware/`:

```bash
python upgrade_firmware.py --host 192.168.1.1 --username admin --password yourpassword

# Explicit firmware file
python upgrade_firmware.py --host 192.168.1.1 --username admin --password yourpassword --firmware firmware/UDR.bin
```

The script automatically enables SSH before upload and disables it after completion.

---

## Deployment Playbook

See [`docs/PLAYBOOK.md`](docs/PLAYBOOK.md) for the full field deployment guide, including on-site cabling order, modem bridge mode configuration, and UniFi Cloud linking.

---

## Roadmap / To-Dos

From [`docs/improvements.md`](docs/improvements.md):

**Security**
- [ ] Credential management via OS keyring or Vault (avoid plaintext passwords on CLI)
- [ ] SSL certificate pinning after first contact
- [ ] Automatic factory credential rotation post-setup
- [ ] Firewall rule provisioning (currently VLANs are created but inter-VLAN rules require manual setup)
- [ ] Signed append-only audit log for regulated environments
- [ ] VLAN config schema validation (pydantic/cerberus)

**Scalability**
- [ ] Multi-site / multi-gateway support with a `sites.yaml` manifest
- [ ] Idempotent re-runs with config drift detection
- [ ] Non-interactive mode (`--yes` / `--non-interactive` flag) for CI pipelines
- [ ] AP WLAN provisioning via `/rest/wlangroup` and `/rest/wlan` (currently manual)
- [ ] Better uplink port detection using API metadata instead of port index heuristics
- [ ] Deployment state file for crash recovery / resume

**Ease of Use**
- [ ] Config file path as a CLI argument (`--config`)
- [ ] Post-deployment verification pass (requery and validate final state)
- [ ] Structured before/after deployment report
- [ ] Package as standalone binary (PyInstaller/Nuitka) or Docker container
- [ ] Disable unused ports as a hardening step (leave one spare Management port)

---

## Notes

- SSL verification is disabled by default (`verify_ssl=False`) since UniFi gateways use self-signed certificates. Pin the cert once known if operating in a higher-trust environment.
- The `output/` directory is gitignored — inventory reports stay local.
- Firmware binaries go in `firmware/` which is also gitignored.
- The first-boot `/api/setup` endpoint was discovered through direct API research on a live UCG Ultra (FW 4.0.6). See [`docs/playwright.md`](docs/playwright.md) for the full discovery notes.
