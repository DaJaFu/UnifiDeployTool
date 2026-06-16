"""
detect.py - Local network-interface enumeration + UniFi device detection.

The GUI's entry point is a list of the machine's network interfaces. For each
interface we probe the likely gateway address on its subnet for the UniFi OS
``/api/system`` endpoint, which is served **unauthenticated** on factory-default
devices and returns the human-readable product name (e.g. "UniFi Cloud Gateway
Ultra"). This lets us label an interface with the device plugged into it before
the user has entered any credentials.

Pure Python (no Qt) so it can be reused headless and unit-tested:

    python -c "import detect, json; print(json.dumps(detect.scan(), indent=2))"
"""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

import psutil
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Factory-default UniFi gateways serve DHCP on 192.168.1.0/24 at 192.168.1.1, so
# the subnet ".1" is the primary probe target. Kept in sync with main.DEFAULT_HOST.
DEFAULT_GATEWAY = "192.168.1.1"

# Short timeout so a full scan stays responsive even when nothing answers.
PROBE_TIMEOUT = 2.5


def list_interfaces() -> list[dict]:
    """Return up interfaces that have an IPv4 address (loopback excluded).

    Each entry: ``{"name", "ipv4", "netmask"}``.
    """
    stats = psutil.net_if_stats()
    interfaces: list[dict] = []
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if st is None or not st.isup:
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address
            if not ip or ip.startswith("127."):
                continue
            interfaces.append({"name": name, "ipv4": ip, "netmask": addr.netmask})
            break  # one IPv4 per interface is enough for our purposes
    return interfaces


def candidate_gateways(ipv4: str, netmask: str | None) -> list[str]:
    """Likely gateway IPs for the subnet the interface lives on.

    Returns the subnet's ".1" host plus the project default, de-duplicated and
    excluding the machine's own address.
    """
    candidates: list[str] = []
    try:
        net = ipaddress.IPv4Network(f"{ipv4}/{netmask or 24}", strict=False)
        gateway = str(net.network_address + 1)
        candidates.append(gateway)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        pass
    if DEFAULT_GATEWAY not in candidates:
        candidates.append(DEFAULT_GATEWAY)
    return [c for c in candidates if c != ipv4]


def probe_gateway(ip: str, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """Probe ``GET https://{ip}/api/system`` for a UniFi OS device.

    Returns ``{"ip", "model", "configured"}`` when a UniFi device answers, or
    ``None`` when nothing UniFi-like is reachable at ``ip``.
    """
    try:
        resp = requests.get(
            f"https://{ip}/api/system", verify=False, timeout=timeout
        )
    except requests.exceptions.RequestException:
        return None

    if resp.status_code in (200, 201):
        # /api/system answered with system info (some firmware serves it even on a
        # configured device). Read isSetup from the body to tell factory-default
        # from already-configured rather than inferring from the HTTP status.
        try:
            body = resp.json()
        except ValueError:
            body = {}
        hw = body.get("hardware", {})
        model = hw.get("name") or hw.get("shortname") or "UniFi device"
        # A device is factory-default only when isSetup is present AND false.
        # When the key is absent the device is already configured (it serves a
        # limited unauthenticated view). Mirrors UniFiClient.needs_initial_setup().
        configured = bool(body.get("isSetup", True))
        return {"ip": ip, "model": model, "configured": configured}

    if resp.status_code in (401, 403):
        # UniFi OS is present but the endpoint now requires auth — it has been set
        # up. We can't read the model name without logging in.
        return {"ip": ip, "model": "UniFi device (configured)", "configured": True}

    return None


def _scan_interface(iface: dict) -> dict:
    """Probe an interface's candidate gateways; annotate with the first hit."""
    result = {**iface, "device": None}
    for ip in candidate_gateways(iface["ipv4"], iface.get("netmask")):
        found = probe_gateway(ip)
        if found:
            result["device"] = found
            break
    return result


def scan(progress_cb=None) -> list[dict]:
    """Enumerate interfaces and probe each concurrently for a UniFi device.

    ``progress_cb`` if given is called as ``progress_cb(done, total)`` after each
    interface finishes. Returns one dict per interface with a ``"device"`` key
    (the probe result, or ``None``).
    """
    interfaces = list_interfaces()
    total = len(interfaces)
    if total == 0:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, total)) as pool:
        futures = {pool.submit(_scan_interface, iface): iface for iface in interfaces}
        for done, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if progress_cb:
                progress_cb(done, total)

    results.sort(key=lambda r: r["name"])
    return results


if __name__ == "__main__":
    import json

    print(json.dumps(scan(), indent=2))
