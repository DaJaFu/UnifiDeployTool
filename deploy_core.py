"""
deploy_core.py - Console-free deployment primitives shared by the CLI and GUI.

These functions take a live UniFiClient and structured config, perform the actual
API work (VLAN/network creation, port-profile creation, switch port overrides),
and report progress through a ``log(level, message)`` callback instead of printing
directly. main.py wraps them with its rich console output; the GUI wraps them with
its log pane.

Switch configuration uses the "switch profile" model: a profile defines a default
port profile applied to every port, plus per-port exceptions. resolve_switch_overrides()
turns that, against a device's real port_table, into the UniFi port_overrides list.
"""

from __future__ import annotations

from typing import Callable

from unifi_client import UniFiClient, UniFiAPIError

# log(level, message); level in {"info", "ok", "warn", "fail"}
Logger = Callable[[str, str], None]


def _emit(log: Logger | None, level: str, message: str) -> None:
    if log is not None:
        log(level, message)


# ---------------------------------------------------------------------------
# VLANs / networks
# ---------------------------------------------------------------------------

def build_network_payload(vlan_cfg: dict, existing_networks: list[dict]) -> dict | None:
    """Convert a vlan_config.yaml entry to a UniFi networkconf payload.

    Returns None if a network with the same VLAN ID already exists.
    """
    vlan_id = vlan_cfg["id"]
    for net in existing_networks:
        if net.get("vlan") == vlan_id:
            return None

    purpose = vlan_cfg.get("purpose", "corporate")
    payload: dict = {
        "name": vlan_cfg["name"],
        "purpose": purpose,
        "vlan": vlan_id,
        "vlan_enabled": True,
        "networkgroup": "LAN",
        "igmp_snooping": vlan_cfg.get("igmp_snooping", True),
    }

    if vlan_cfg.get("dhcp_enabled"):
        gw = vlan_cfg["gateway"]
        prefix = vlan_cfg["subnet"].split("/")[1]
        payload["ip_subnet"] = f"{gw}/{prefix}"
        payload["dhcpd_enabled"] = True
        payload["dhcpd_start"] = vlan_cfg["dhcp_start"]
        payload["dhcpd_stop"] = vlan_cfg["dhcp_stop"]
        payload["dhcpd_leasetime"] = vlan_cfg.get("dhcp_lease_seconds", 86400)
        dns = vlan_cfg.get("dns", ["1.1.1.1"])
        payload["dhcpd_dns_1"] = dns[0] if len(dns) > 0 else "1.1.1.1"
        if len(dns) > 1:
            payload["dhcpd_dns_2"] = dns[1]

    if purpose == "guest":
        payload["ap_isolation"] = vlan_cfg.get("client_isolation", True)

    return payload


def sync_vlans(
    client: UniFiClient,
    vlan_configs: list[dict],
    dry_run: bool = False,
    log: Logger | None = None,
) -> dict[int, dict]:
    """Create any missing VLANs. Returns a map of vlan_id -> network object."""
    existing = [] if dry_run else client.get_networks()
    network_map: dict[int, dict] = {}
    for net in existing:
        vid = net.get("vlan")
        if vid:
            network_map[vid] = net

    for vcfg in vlan_configs:
        vlan_id = vcfg["id"]
        name = vcfg["name"]

        if vlan_id in network_map:
            _emit(log, "warn", f"VLAN {vlan_id} ({name}) already exists – skipping creation")
            continue

        payload = build_network_payload(vcfg, existing)
        if payload is None:
            _emit(log, "warn", f"VLAN {vlan_id} ({name}) already exists – skipping")
            continue

        if dry_run:
            _emit(log, "ok", f"[DRY RUN] Would create VLAN {vlan_id} – {name}")
            network_map[vlan_id] = {"_id": f"dry-run-{vlan_id}", "vlan": vlan_id, **payload}
            continue

        try:
            created = client.create_network(payload)
            network_map[vlan_id] = created
            _emit(log, "ok", f"Created VLAN {vlan_id} – {name}  (id={created.get('_id', '?')})")
        except UniFiAPIError as exc:
            _emit(log, "fail", f"Failed to create VLAN {vlan_id} ({name}): {exc}")

    return network_map


# ---------------------------------------------------------------------------
# Port profiles (portconf objects on the controller)
# ---------------------------------------------------------------------------

def ensure_port_profile(
    client: UniFiClient,
    profile_def: dict,
    existing_profiles: list[dict],
    network_map: dict[int, dict],
    dry_run: bool = False,
    log: Logger | None = None,
) -> str | None:
    """Find or create a port profile (portconf). Returns its _id, or None on failure."""
    profile_name = profile_def["name"]

    for p in existing_profiles:
        if p.get("name") == profile_name:
            return p["_id"]

    native_vlan_id = profile_def.get("native_vlan_id")
    native_net = network_map.get(native_vlan_id)
    if not native_net:
        _emit(log, "warn", f"Network for native VLAN {native_vlan_id} not found; skipping profile '{profile_name}'")
        return None

    tagged_ids = []
    for vid in profile_def.get("tagged_vlan_ids", []) or []:
        net = network_map.get(vid)
        if net:
            tagged_ids.append(net["_id"])
        else:
            _emit(log, "warn", f"Network for tagged VLAN {vid} not found; omitting from '{profile_name}'")

    if dry_run:
        _emit(log, "ok", f"[DRY RUN] Would create port profile '{profile_name}'")
        return native_net["_id"]   # placeholder so resolution can proceed in a dry run

    payload = {
        "name": profile_name,
        "forward": "customize",
        "op_mode": "switch",
        "native_networkconf_id": native_net["_id"],
        "tagged_networkconf_ids": tagged_ids,
    }
    try:
        created = client.create_port_profile(payload)
        return created.get("_id")
    except UniFiAPIError as exc:
        _emit(log, "fail", f"Could not create port profile '{profile_name}': {exc}")
        return None


def ensure_port_profiles(
    client: UniFiClient,
    profile_defs: dict[str, dict],
    network_map: dict[int, dict],
    keys: set[str] | None = None,
    dry_run: bool = False,
    log: Logger | None = None,
) -> dict[str, str]:
    """Ensure each named port profile exists. Returns {profile_key: portconf_id}.

    If ``keys`` is given, only those profile keys are processed.
    """
    existing = [] if dry_run else client.get_port_profiles()
    out: dict[str, str] = {}
    for key, pp_def in profile_defs.items():
        if keys is not None and key not in keys:
            continue
        pid = ensure_port_profile(client, pp_def, existing, network_map, dry_run, log)
        if pid:
            out[key] = pid
            _emit(log, "ok", f"Port profile ready: {pp_def.get('name', key)}")
    return out


# ---------------------------------------------------------------------------
# Switch profiles (default port profile + per-port exceptions)
# ---------------------------------------------------------------------------

def referenced_profile_keys(switch_profile: dict) -> set[str]:
    """Port-profile keys referenced by a switch profile (default + exceptions)."""
    keys: set[str] = set()
    default = switch_profile.get("default_port_profile")
    if default:
        keys.add(default)
    for key in (switch_profile.get("ports") or {}).values():
        if key:
            keys.add(key)
    return keys


def resolve_switch_overrides(
    switch_profile: dict,
    port_table: list[dict],
    profile_id_map: dict[str, str],
) -> tuple[list[dict], list[str]]:
    """Resolve a switch profile against a device's real ports.

    Returns (port_overrides, warnings). Each override is
    {"port_idx", "portconf_id"}. Ports with no default and no exception are left
    unchanged (omitted). Unresolvable profile references are reported as warnings.
    """
    default_key = switch_profile.get("default_port_profile")
    # YAML may give int or str keys; normalise exception keys to int.
    exceptions: dict[int, str] = {}
    for k, v in (switch_profile.get("ports") or {}).items():
        try:
            exceptions[int(k)] = v
        except (TypeError, ValueError):
            continue

    overrides: list[dict] = []
    warnings: list[str] = []
    for port in port_table:
        idx = port.get("port_idx")
        if idx is None:
            continue
        key = exceptions.get(idx, default_key)
        if not key:
            continue   # leave this port unchanged
        pid = profile_id_map.get(key)
        if not pid:
            warnings.append(f"port {idx}: profile '{key}' not available — left unchanged")
            continue
        overrides.append({"port_idx": idx, "portconf_id": pid})
    return overrides, warnings


def apply_switch_profile(
    client: UniFiClient,
    device: dict,
    switch_profile: dict,
    profile_id_map: dict[str, str],
    dry_run: bool = False,
    log: Logger | None = None,
) -> list[dict]:
    """Resolve and apply a switch profile to one switch. Returns the overrides."""
    name = device.get("name") or device.get("mac", "?")
    ports = device.get("port_table", []) or []
    overrides, warnings = resolve_switch_overrides(switch_profile, ports, profile_id_map)
    for w in warnings:
        _emit(log, "warn", f"{name}: {w}")

    if not overrides:
        _emit(log, "warn", f"{name}: nothing to apply")
        return overrides

    if dry_run:
        _emit(log, "ok", f"[DRY RUN] Would set {len(overrides)} port(s) on {name}")
        return overrides

    try:
        client.update_device_port_overrides(device["_id"], overrides)
        _emit(log, "ok", f"Applied {len(overrides)} port override(s) to {name}")
    except UniFiAPIError as exc:
        _emit(log, "fail", f"Could not configure {name}: {exc}")
    return overrides


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def deploy_switch_assignments(
    client: UniFiClient,
    assignments: list[dict],
    vlan_configs: list[dict],
    port_profile_defs: dict[str, dict],
    dry_run: bool = False,
    log: Logger | None = None,
) -> None:
    """Full switch deploy: sync VLANs → ensure referenced port profiles → apply.

    ``assignments`` is a list of {"device": <device dict>, "switch_profile": <def>}.
    """
    _emit(log, "info", "Syncing VLANs…")
    network_map = sync_vlans(client, vlan_configs, dry_run, log)

    needed: set[str] = set()
    for a in assignments:
        needed |= referenced_profile_keys(a["switch_profile"])

    _emit(log, "info", "Ensuring port profiles…")
    profile_id_map = ensure_port_profiles(
        client, port_profile_defs, network_map, keys=needed, dry_run=dry_run, log=log
    )

    _emit(log, "info", "Applying switch profiles…")
    for a in assignments:
        apply_switch_profile(client, a["device"], a["switch_profile"], profile_id_map, dry_run, log)
