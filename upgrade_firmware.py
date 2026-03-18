#!/usr/bin/env python3
"""
upgrade_firmware.py
-------------------
Applies a firmware update to a UniFi OS gateway using the local REST API + SSH.

Steps:
  1. Login and read current firmware version / model
  2. Locate firmware file in ./firmware/ matching the device model
  3. Enable SSH via PATCH /api/system
  4. SCP firmware to /tmp/fwupdate.bin
  5. Run: ubnt-systool fwupdate /tmp/fwupdate.bin  (device reboots)
  6. Wait for gateway to come back online
  7. Login again and disable SSH
  8. Print new firmware version to confirm

Usage:
    python3 upgrade_firmware.py --username admin --password password
    python3 upgrade_firmware.py --host 192.168.1.1 --username admin --password password
    python3 upgrade_firmware.py --username admin --password password --firmware firmware/UDRULT.bin
"""

import argparse
import sys
import time
from pathlib import Path

import paramiko
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_HOST = "192.168.1.1"
SSH_USER     = "root"
REBOOT_WAIT  = 30   # seconds to wait before polling after upgrade triggers
REBOOT_TIMEOUT = 300  # max seconds to wait for gateway to come back


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def login(session: requests.Session, base_url: str, username: str, password: str) -> None:
    resp = session.post(
        f"{base_url}/api/auth/login",
        json={"username": username, "password": password},
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        print(f"  [FAIL] Login failed: HTTP {resp.status_code}")
        sys.exit(1)

    csrf = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")
    if csrf:
        session.headers.update({"X-CSRF-Token": csrf})

    print(f"  [OK] Logged in as {username}")


def get_system_info(session: requests.Session, base_url: str) -> dict:
    resp = session.get(f"{base_url}/api/system", timeout=10)
    if resp.status_code != 200:
        print(f"  [FAIL] GET /api/system returned HTTP {resp.status_code}")
        sys.exit(1)
    return resp.json()


def ssh_set(session: requests.Session, base_url: str, enabled: bool) -> None:
    label = "enable" if enabled else "disable"
    resp = session.patch(
        f"{base_url}/api/system",
        json={"ssh": {"enabled": enabled}},
        timeout=10,
    )
    if resp.status_code == 200:
        print(f"  [OK] SSH {label}d")
    else:
        print(f"  [FAIL] Could not {label} SSH: HTTP {resp.status_code} — {resp.text[:200]}")
        sys.exit(1)


def wait_for_reboot(base_url: str) -> bool:
    """Poll GET /api/system until the gateway responds. Returns True if it came back."""
    print(f"  Waiting up to {REBOOT_TIMEOUT}s for gateway to come back online…", flush=True)
    time.sleep(REBOOT_WAIT)
    deadline = time.time() + REBOOT_TIMEOUT
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/system", verify=False, timeout=5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        print("  …still waiting", flush=True)
        time.sleep(10)
    return False


# ---------------------------------------------------------------------------
# Firmware helpers
# ---------------------------------------------------------------------------

def find_firmware(shortname: str, explicit_path: str | None) -> Path:
    """Return path to firmware file, or exit with a helpful message."""
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            print(f"  [FAIL] Firmware file not found: {p}")
            sys.exit(1)
        return p

    fw_dir = Path("firmware")
    if fw_dir.exists():
        for ext in ("*.bin", "*.tar.gz", "*.tar"):
            for f in fw_dir.glob(ext):
                if shortname.lower() in f.name.lower():
                    return f

    print(
        f"  [FAIL] No firmware file found for model '{shortname}' in ./firmware/\n"
        f"  Download it from https://ui.com/download and place it in ./firmware/{shortname}.bin\n"
        f"  Or pass the path explicitly with --firmware <path>"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# SSH / SCP
# ---------------------------------------------------------------------------

def upload_and_apply(host: str, password: str, firmware_path: Path) -> None:
    """SCP firmware to /tmp/fwupdate.bin then trigger the update over SSH."""
    print(f"  Connecting via SSH as {SSH_USER}@{host}…")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        host,
        port=22,
        username=SSH_USER,
        password=password,
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )

    print(f"  Uploading {firmware_path.name} ({firmware_path.stat().st_size / 1e6:.1f} MB)…")
    with ssh.open_sftp() as sftp:
        sftp.put(str(firmware_path), "/tmp/fwupdate.bin")
    print("  [OK] Upload complete")

    print("  Triggering firmware update (ubnt-systool fwupdate /tmp/fwupdate.bin)…")
    # Device reboots immediately — the command will not return a response
    ssh.exec_command("ubnt-systool fwupdate /tmp/fwupdate.bin")
    ssh.close()
    print("  [OK] Update triggered — gateway is rebooting")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Apply a firmware update to a UniFi OS gateway via SSH"
    )
    p.add_argument("--host",     default=DEFAULT_HOST)
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--firmware", default=None, help="Path to .bin file (auto-detected from ./firmware/ if omitted)")
    args = p.parse_args()

    base_url = f"https://{args.host}"
    session  = requests.Session()
    session.verify = False
    session.headers.update({"Content-Type": "application/json"})

    print(f"\n{'='*50}")
    print(f"  UniFi Gateway Firmware Upgrade")
    print(f"  Target: {base_url}")
    print(f"{'='*50}\n")

    # Step 1 — login and read current state
    login(session, base_url, args.username, args.password)
    info     = get_system_info(session, base_url)
    hw       = info.get("hardware", {})
    shortname = hw.get("shortname", "")
    fw_before = hw.get("firmwareVersion", "unknown")
    print(f"  Model:    {hw.get('name', shortname)}")
    print(f"  Firmware: {fw_before}\n")

    # Step 2 — find firmware file
    firmware_path = find_firmware(shortname, args.firmware)
    print(f"  Firmware file: {firmware_path}\n")

    # Step 3 — enable SSH
    ssh_set(session, base_url, True)

    # Step 4+5 — upload and trigger
    try:
        upload_and_apply(args.host, args.password, firmware_path)
    except Exception as exc:
        print(f"  [FAIL] SSH/SCP error: {exc}")
        print("  Attempting to disable SSH before exiting…")
        ssh_set(session, base_url, False)
        sys.exit(1)

    # Step 6 — wait for reboot
    came_back = wait_for_reboot(base_url)
    if not came_back:
        print(f"\n  [WARN] Gateway did not respond within {REBOOT_TIMEOUT}s.")
        print("  It may still be updating. SSH will remain enabled — disable it manually once it's back.")
        sys.exit(1)
    print("  [OK] Gateway is back online\n")

    # Step 7 — login again and disable SSH
    session = requests.Session()
    session.verify = False
    session.headers.update({"Content-Type": "application/json"})
    login(session, base_url, args.username, args.password)
    ssh_set(session, base_url, False)

    # Step 8 — confirm new firmware version
    info_after = get_system_info(session, base_url)
    fw_after = info_after.get("hardware", {}).get("firmwareVersion", "unknown")
    print(f"\n  Firmware before: {fw_before}")
    print(f"  Firmware after:  {fw_after}")

    if fw_after != fw_before:
        print("\n  [OK] Firmware updated successfully!")
    else:
        print("\n  [WARN] Firmware version unchanged — update may not have applied.")

    print()


if __name__ == "__main__":
    main()
