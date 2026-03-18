#!/usr/bin/env python3
"""
test_ssh_toggle.py
------------------
Enables SSH on a UniFi OS gateway, waits for Enter, then disables it.
Used to verify the /api/system/ssh/enable and /api/system/ssh/disable endpoints.

Usage:
    python3 test_ssh_toggle.py --username admin --password password
    python3 test_ssh_toggle.py --host 192.168.1.1 --username admin --password password
"""

import argparse
import sys
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_HOST = "192.168.1.1"


def login(session: requests.Session, base_url: str, username: str, password: str) -> None:
    resp = session.post(
        f"{base_url}/api/auth/login",
        json={"username": username, "password": password},
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        print(f"  [FAIL] Login failed: HTTP {resp.status_code} — {resp.text[:200]}")
        sys.exit(1)

    csrf = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")
    if csrf:
        session.headers.update({"X-CSRF-Token": csrf})

    print(f"  [OK] Logged in as {username}")


def ssh_state(session: requests.Session, base_url: str) -> bool | None:
    """Return current SSH enabled state from GET /api/system, or None on error."""
    resp = session.get(f"{base_url}/api/system", timeout=10)
    if resp.status_code == 200:
        return resp.json().get("ssh")
    return None


def ssh_set(session: requests.Session, base_url: str, enabled: bool) -> bool:
    """PATCH /api/system to enable or disable SSH. Returns True on success."""
    resp = session.patch(
        f"{base_url}/api/system",
        json={"ssh": {"enabled": enabled}},
        timeout=10,
    )
    if resp.status_code == 200:
        return True
    print(f"  [FAIL] HTTP {resp.status_code} — {resp.text[:300]}")
    return False


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    args = p.parse_args()

    base_url = f"https://{args.host}"
    session = requests.Session()
    session.verify = False
    session.headers.update({"Content-Type": "application/json"})

    print(f"\nTarget: {base_url}\n")

    login(session, base_url, args.username, args.password)

    print(f"  SSH currently: {ssh_state(session, base_url)}")

    if ssh_set(session, base_url, True):
        print(f"  [OK] SSH enabled  (state now: {ssh_state(session, base_url)})")

    input("\n  SSH is ON — press Enter to disable...\n")

    if ssh_set(session, base_url, False):
        print(f"  [OK] SSH disabled  (state now: {ssh_state(session, base_url)})")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
