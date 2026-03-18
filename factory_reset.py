#!/usr/bin/env python3
"""
factory_reset.py
----------------
Factory resets all UniFi devices connected to the gateway in safe dependency order:
  1. Access Points  (uap)
  2. Switches       (usw)
  3. Gateway        (udm / uxg / ugw)  ← last, since it's the controller

For each non-gateway device the script:
  - Sends a reset command via the Network app API
  - Polls until the device is no longer adopted (pending adoption or gone)
  - Confirms before moving to the next device type

For the gateway the script:
  - Sends a factory reset via the UniFi OS API
  - Polls until /api/system reports isSetup=false (factory-default state)

Usage:
    python3 factory_reset.py --username admin --password password
    python3 factory_reset.py --host 192.168.1.1 --username admin --password password
"""

import argparse
import sys
import time

import requests
import urllib3

from unifi_client import UniFiClient, UniFiAPIError, UniFiConnectionError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_HOST = "192.168.1.1"

# Order in which device types are reset (gateway handled separately at the end)
RESET_ORDER = ["uap", "usw"]

GATEWAY_TYPES = {"udm", "uxg", "ugw"}

TYPE_LABELS = {
    "uap": "Access Point",
    "usw": "Switch",
    "udm": "Gateway",
    "uxg": "Gateway",
    "ugw": "Gateway",
}

POLL_INTERVAL   = 5    # seconds between polls
DEVICE_TIMEOUT  = 180  # seconds to wait for a device to reset
GATEWAY_TIMEOUT = 300  # seconds to wait for gateway to come back up


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def banner(msg: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {msg}")
    print(f"{'─' * 55}")


def confirm(msg: str) -> bool:
    try:
        reply = input(f"\n  {msg} [y/N] ").strip().lower()
        return reply == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def device_label(dev: dict) -> str:
    name = dev.get("name") or dev.get("mac", "?")
    mac  = dev.get("mac", "")
    return f"{name} ({mac})"


# ---------------------------------------------------------------------------
# Reset commands (non-gateway devices via Network app devmgr)
# ---------------------------------------------------------------------------

def send_reset(client: UniFiClient, dev: dict) -> bool:
    """Try reset-default, then factory-reset. Returns True if a command was accepted."""
    mac = dev.get("mac", "").lower()
    for cmd in ("reset-default", "factory-reset"):
        try:
            client._net_cmd({"cmd": cmd, "mac": mac})
            print(f"    [OK] Command '{cmd}' accepted")
            return True
        except UniFiAPIError as exc:
            print(f"    [--] '{cmd}' rejected: {exc}")
    return False


def wait_for_device_reset(client: UniFiClient, mac: str, label: str) -> bool:
    """
    Poll until the device is no longer adopted.
    A successful reset shows as adopted=False / state=2 (Pending Adoption) or disappears.
    Returns True if confirmed reset, False on timeout.
    """
    mac = mac.lower()
    deadline = time.time() + DEVICE_TIMEOUT
    print(f"    Waiting up to {DEVICE_TIMEOUT}s for {label} to reset…", flush=True)

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            devices = client.get_devices()
        except Exception:
            continue

        match = next((d for d in devices if d.get("mac", "").lower() == mac), None)

        if match is None:
            print(f"    [OK] {label} no longer visible — reset confirmed")
            return True

        adopted = match.get("adopted", True)
        state   = match.get("state", -1)

        if not adopted or state == 2:
            print(f"    [OK] {label} is pending adoption — reset confirmed")
            return True

        state_names = {0: "Disconnected", 1: "Connected", 4: "Upgrading",
                       5: "Provisioning", 6: "Heartbeat Missed", 7: "Adopting", 10: "Adopt Failed"}
        print(f"    … {label} state={state_names.get(state, state)}  adopted={adopted}", flush=True)

    return False


# ---------------------------------------------------------------------------
# Gateway factory reset
# ---------------------------------------------------------------------------

def reset_gateway(host: str, session: requests.Session, base_url: str) -> bool:
    """
    Send factory reset to the gateway via POST /api/system/factory-reset.
    The gateway immediately reboots — connection will drop.
    Returns True if the command was accepted.
    """
    resp = session.post(f"{base_url}/api/system/factory-reset", json={}, timeout=10)
    if resp.status_code in (200, 201):
        print(f"    [OK] Factory reset accepted — gateway is rebooting")
        return True
    print(f"    [FAIL] HTTP {resp.status_code} — {resp.text[:300]}")
    return False


def wait_for_gateway_factory_default(base_url: str) -> bool:
    """
    Poll GET /api/system until isSetup=false (factory-default state).
    Returns True if confirmed, False on timeout.
    """
    print(f"    Waiting up to {GATEWAY_TIMEOUT}s for gateway to come back in factory-default state…", flush=True)
    time.sleep(30)  # give it time to start rebooting before polling
    deadline = time.time() + GATEWAY_TIMEOUT

    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/system", verify=False, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("isSetup", True):
                    print(f"    [OK] Gateway is in factory-default state (isSetup=false)")
                    return True
                fw = data.get("hardware", {}).get("firmwareVersion", "?")
                print(f"    … gateway responding (FW {fw}) but isSetup=true — still resetting", flush=True)
        except Exception:
            print(f"    … gateway not yet reachable", flush=True)
        time.sleep(POLL_INTERVAL)

    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Factory reset all UniFi devices in safe order")
    p.add_argument("--host",     default=DEFAULT_HOST)
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    args = p.parse_args()

    base_url = f"https://{args.host}"

    print(f"\n{'=' * 55}")
    print(f"  UniFi Factory Reset Utility")
    print(f"  Target: {base_url}")
    print(f"{'=' * 55}")
    print(f"\n  WARNING: This will factory reset ALL connected devices.")
    print(f"  All configuration will be lost.\n")

    if not confirm("Are you sure you want to continue?"):
        print("\n  Aborted.\n")
        sys.exit(0)

    # Connect
    client = UniFiClient(args.host, verify_ssl=False)
    try:
        client.login(args.username, args.password)
        print(f"  [OK] Logged in as {args.username}")
    except UniFiConnectionError as exc:
        print(f"  [FAIL] Login failed: {exc}")
        sys.exit(1)

    # Fetch all devices
    all_devices = client.get_devices()
    if not all_devices:
        print("  No devices found.")
        sys.exit(0)

    # Separate gateway from the rest
    gateway_dev = next((d for d in all_devices if d.get("type") in GATEWAY_TYPES), None)
    other_devices = [d for d in all_devices if d.get("type") not in GATEWAY_TYPES]

    # Print summary
    print(f"\n  Devices found:")
    for dev in all_devices:
        dtype = dev.get("type", "?")
        label = TYPE_LABELS.get(dtype, dtype)
        print(f"    • {label:15}  {device_label(dev)}")

    # -----------------------------------------------------------------------
    # Reset non-gateway devices in order: APs → Switches
    # -----------------------------------------------------------------------
    for device_type in RESET_ORDER:
        targets = [d for d in other_devices if d.get("type") == device_type]
        if not targets:
            continue

        type_name = f"{TYPE_LABELS.get(device_type, device_type)}s"
        banner(f"Resetting {type_name} ({len(targets)} device(s))")

        if not confirm(f"Reset all {type_name}?"):
            print(f"  Skipping {type_name}.")
            continue

        all_ok = True
        for dev in targets:
            label = device_label(dev)
            mac   = dev.get("mac", "")
            print(f"\n  → {label}")

            if not send_reset(client, dev):
                print(f"    [FAIL] Could not send reset command to {label}")
                all_ok = False
                continue

            if not wait_for_device_reset(client, mac, label):
                print(f"    [WARN] Timed out waiting for {label} to reset")
                all_ok = False

        if all_ok:
            print(f"\n  [OK] All {type_name} reset confirmed")
        else:
            if not confirm(f"Some {type_name} did not confirm reset. Continue anyway?"):
                print("\n  Aborted.\n")
                sys.exit(1)

    # -----------------------------------------------------------------------
    # Reset gateway last
    # -----------------------------------------------------------------------
    banner("Resetting Gateway (final step)")

    if not gateway_dev:
        print("  No gateway device found in device list — skipping.")
    else:
        print(f"\n  Gateway: {device_label(gateway_dev)}")
        print(f"\n  NOTE: Resetting the gateway will disconnect all devices and")
        print(f"  return it to factory-default state. This is the point of no return.")

        if not confirm("Reset the gateway?"):
            print("\n  Gateway reset skipped. All other resets completed.\n")
            sys.exit(0)

        # Build a raw session for the gateway OS API (client session is logged in)
        raw_session = requests.Session()
        raw_session.verify = False
        raw_session.headers.update({"Content-Type": "application/json"})

        # Re-use the existing auth session for the reset command
        raw_session.cookies.update(client.session.cookies)
        raw_session.headers.update(client.session.headers)

        if not reset_gateway(args.host, raw_session, base_url):
            print(f"  [FAIL] Gateway reset command rejected.")
            sys.exit(1)

        if wait_for_gateway_factory_default(base_url):
            print(f"\n  [OK] Gateway is back in factory-default state.")
            print(f"  You can now run:  python3 main.py --setup")
        else:
            print(f"\n  [WARN] Gateway did not confirm factory-default state within {GATEWAY_TIMEOUT}s.")
            print(f"  It may still be resetting — check manually at https://{args.host}")

    print(f"\n{'=' * 55}")
    print(f"  Factory reset complete.")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    main()
