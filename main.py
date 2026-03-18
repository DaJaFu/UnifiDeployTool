#!/usr/bin/env python3
"""
BC10 Deploy Tool
----------------
Automated deployment tool for UniFi Gateway Max / Ultra (factory-default state).

Usage:
    python main.py [--host 192.168.1.1] [--dry-run] [--output-dir ./output]

Steps performed:
    0. Initial device setup (factory-default only, requires --setup)
    1. Connect to gateway (try defaults, prompt on failure)
    1b. Adopt switches first (iterates until all switches adopted, so downstream
        switches and APs behind them are visible before pre-flight)
    2. Pre-flight device scan (read-only; confirm before any writes)
    3. Configure 3 VLANs from vlan_config.yaml
    4. Discover, adopt, and configure connected UniFi devices
    5. Apply VLAN port profiles (safe ordering to prevent adoption loops)
    6. Generate inventory documentation

NOTE – Firmware upgrade is NOT implemented.
    Ubiquiti's public firmware CDN URLs are no longer valid, SSH is disabled
    by default on current UniFi OS builds, and the local REST API does not
    accept firmware file uploads. The upgrade path needs to be determined
    before this feature can be added. See docs/improvements.md for details.
"""

import argparse
import getpass
import logging
import sys
import time
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.prompt import Prompt, Confirm
from rich.table import Table

from unifi_client import UniFiClient, UniFiConnectionError, UniFiAPIError
import inventory as inv

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_HOST = "192.168.1.1"
DEFAULT_USERNAME = "ubnt"
DEFAULT_PASSWORD = "ubnt"

console = Console()
log = logging.getLogger("bc10deploy")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict | list:
    with open(path) as f:
        return yaml.safe_load(f)


def step_header(title: str) -> None:
    console.print()
    console.rule(f"[bold cyan]{title}[/bold cyan]")


def ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]⚠[/yellow]  {msg}")


def fail(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


# ---------------------------------------------------------------------------
# Step 0 – Initial Setup (factory-default devices only)
# ---------------------------------------------------------------------------

def initial_setup(host: str, setup_cfg: dict, dry_run: bool) -> tuple[str, str]:
    """
    Check whether the device needs first-boot setup and, if so, run it.
    Returns (username, password) to use for the subsequent login in Step 1.
    Safe to call on an already-configured device — detects state via /api/system.
    """
    step_header("Step 0 · Initial Device Setup")

    username = setup_cfg["admin_username"]
    password = setup_cfg["admin_password"]

    if dry_run:
        warn("[DRY RUN] Skipping setup check")
        return username, password

    client = UniFiClient(host, verify_ssl=False)

    try:
        info = client.check_setup_status()
    except UniFiConnectionError as exc:
        fail(f"Cannot reach device at {host}: {exc}")
        sys.exit(1)

    hw = info.get("hardware", {})
    device_label = hw.get("name") or hw.get("shortname") or "Unknown"
    console.print(
        f"  Device: [bold]{device_label}[/bold]  "
        f"FW: {hw.get('firmwareVersion', '?')}  "
        f"MAC: {hw.get('mac', info.get('mac', '?'))}"
    )

    if not info.get("isSetup", True):
        console.print("  Device is in factory-default state. Running initial setup…")
        try:
            client.initial_setup(
                device_name=setup_cfg.get("device_name", "BC10-Gateway"),
                username=username,
                password=password,
                country=setup_cfg.get("country", 840),
                timezone=setup_cfg.get("timezone"),
            )
            ok(f"Device configured — admin user '[bold]{username}[/bold]' created")
        except UniFiAPIError as exc:
            fail(f"Initial setup failed: {exc}")
            sys.exit(1)
    else:
        ok("Device already configured — skipping initial setup")

    return username, password


# ---------------------------------------------------------------------------
# Step 1 – Connect
# ---------------------------------------------------------------------------

def connect_to_gateway(
    host: str,
    dry_run: bool,
    preferred_username: str | None = None,
    preferred_password: str | None = None,
) -> UniFiClient:
    step_header("Step 1 · Connect to Gateway")
    console.print(f"  Target: [bold]{host}[/bold]")

    if dry_run:
        warn("DRY RUN – skipping real connection")
        return UniFiClient(host)

    # Warn about self-signed cert
    warn("SSL certificate verification is disabled (self-signed cert expected on factory device).")

    client = UniFiClient(host, verify_ssl=False)

    # If setup_config credentials were supplied (via --setup), try them first
    if preferred_username and preferred_password:
        try:
            console.print(f"  Trying setup credentials ({preferred_username}/****)…")
            client.login(preferred_username, preferred_password)
            ok(f"Connected as [bold]{preferred_username}[/bold]")
            return client
        except UniFiConnectionError as exc:
            warn(f"Setup credentials rejected: {exc}")

    # Try factory defaults
    try:
        console.print(f"  Trying default credentials ({DEFAULT_USERNAME}/{DEFAULT_PASSWORD})…")
        client.login(DEFAULT_USERNAME, DEFAULT_PASSWORD)
        ok("Connected with default credentials")
        return client
    except UniFiConnectionError as exc:
        if "Authentication failed" in str(exc):
            warn("Default credentials rejected. Prompting for credentials.")
        else:
            warn(f"Cannot reach gateway: {exc}")
            if not Confirm.ask("  Gateway unreachable. Retry with custom host/credentials?"):
                console.print("[red]Aborting.[/red]")
                sys.exit(1)
            host = Prompt.ask("  Gateway IP or hostname", default=host)
            client = UniFiClient(host, verify_ssl=False)

    # Prompt for credentials
    for attempt in range(3):
        username = Prompt.ask("  Username", default="admin")
        password = getpass.getpass("  Password: ")
        try:
            client.login(username, password)
            ok(f"Connected as [bold]{username}[/bold]")
            return client
        except UniFiConnectionError as exc:
            fail(str(exc))
            if attempt == 2:
                console.print("[red]Too many failed attempts. Aborting.[/red]")
                sys.exit(1)

    # Unreachable, but satisfies type checker
    sys.exit(1)


# ---------------------------------------------------------------------------
# Step 1b – Adopt switches before pre-flight
# ---------------------------------------------------------------------------

def adopt_switches(client: UniFiClient, dry_run: bool) -> None:
    """
    Iteratively discover and adopt all switches before the pre-flight check.

    Loops until no pending switches remain so that stacked/daisy-chained
    switches — which only become visible once an upstream switch is adopted
    and online — are all caught before we run the full device scan.
    """
    step_header("Step 1b · Adopt Switches")

    if dry_run:
        warn("[DRY RUN] Skipping switch adoption")
        return

    gw_product_name = client.get_gateway_product_name()
    round_num = 0

    while True:
        round_num += 1
        all_devices = client.get_devices()

        table = Table(
            title=f"Device Scan – Round {round_num}",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Type")
        table.add_column("Name / MAC")
        table.add_column("IP")
        table.add_column("Status")

        for dev in all_devices:
            dtype = dev.get("type", "")
            type_label = (
                gw_product_name if dtype == "udm"
                else DEVICE_TYPE_LABELS.get(dtype, dtype or "?")
            )
            state = dev.get("state", -1)
            state_str, _ = STATE_DISPLAY.get(state, (f"[dim]{state}[/dim]", False))
            table.add_row(
                type_label,
                dev.get("name") or dev.get("mac", ""),
                dev.get("ip", ""),
                state_str,
            )

        console.print(table)

        pending_switches = [
            d for d in all_devices
            if d.get("type") == "usw" and not d.get("adopted", True)
        ]

        if not pending_switches:
            ok("All switches adopted — proceeding to pre-flight")
            break

        console.print(f"\n  [bold]{len(pending_switches)}[/bold] switch(es) pending adoption:")
        adopted_macs: set[str] = set()

        for dev in pending_switches:
            mac = dev.get("mac", "")
            name = dev.get("name") or mac
            if Confirm.ask(f"  Adopt switch [bold]{name}[/bold] ({mac})?", default=True):
                try:
                    client.adopt_device(mac)
                    ok(f"Adoption command sent to {name}")
                    adopted_macs.add(mac.lower())
                except UniFiAPIError as exc:
                    fail(f"Could not adopt {name}: {exc}")

        if not adopted_macs:
            break  # user declined all — move on

        console.print("\n  Waiting for switches to come online (up to 3 min)…")
        deadline = time.time() + 180
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), transient=True) as prog:
            task = prog.add_task("Waiting for switches…", total=180)
            while time.time() < deadline:
                prog.update(task, completed=180 - (deadline - time.time()))
                time.sleep(5)
                current_devices = client.get_devices()
                still_pending = [
                    d for d in current_devices
                    if d.get("mac", "").lower() in adopted_macs and d.get("state") != 1
                ]
                if not still_pending:
                    break
                prog.update(
                    task,
                    description=f"Waiting… still provisioning: {[d.get('mac') for d in still_pending]}",
                )

        for dev in client.get_devices():
            if dev.get("mac", "").lower() in adopted_macs:
                state = dev.get("state")
                name = dev.get("name") or dev.get("mac", "")
                if state == 1:
                    ok(f"{name} online")
                else:
                    label = {10: "Adopt Failed", 5: "Provisioning", 6: "Heartbeat Missed"}.get(state, str(state))
                    warn(f"{name} not yet fully online (state={label}) — will retry loop")
        # Loop back to scan for any newly visible downstream switches


# ---------------------------------------------------------------------------
# Step 2 – Pre-flight device scan
# ---------------------------------------------------------------------------

PROBLEM_STATES = {0, 6, 10}  # Disconnected, Heartbeat Missed, Adopt Failed

STATE_DISPLAY = {
    0:  ("[yellow]Disconnected[/yellow]",    True),
    1:  ("[green]Connected[/green]",          False),
    2:  ("[yellow]Pending Adoption[/yellow]", False),
    4:  ("[blue]Upgrading[/blue]",            False),
    5:  ("[blue]Provisioning[/blue]",         False),
    6:  ("[yellow]Heartbeat Missed[/yellow]", True),
    7:  ("[blue]Adopting[/blue]",             False),
    10: ("[red]Adopt Failed[/red]",           True),
}


def preflight_check(client: UniFiClient) -> None:
    """
    Query all devices known to the controller and display a summary table.
    Flags devices in problematic states, then prompts the operator to confirm
    before any writes are made. Exits cleanly if declined.
    """
    step_header("Step 2 · Pre-flight Device Scan")

    devices = client.get_devices()
    gw_product_name = client.get_gateway_product_name()

    if not devices:
        warn("No devices found on the controller. Ensure all devices are connected and powered on.")
    else:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Type")
        table.add_column("Name")
        table.add_column("MAC")
        table.add_column("IP")
        table.add_column("Status")

        problems: list[str] = []

        for dev in devices:
            dtype = dev.get("type", "")
            type_label = (
                gw_product_name if dtype == "udm"
                else DEVICE_TYPE_LABELS.get(dtype, dtype or "Unknown")
            )
            name = dev.get("name") or "—"
            mac  = dev.get("mac", "—")
            ip   = dev.get("ip", "—")
            state = dev.get("state", -1)

            state_str, is_problem = STATE_DISPLAY.get(
                state, (f"[dim]Unknown ({state})[/dim]", True)
            )

            if is_problem:
                problems.append(f"{name}  {mac}  →  {state_str}")

            table.add_row(type_label, name, mac, ip, state_str)

        console.print(table)

        if problems:
            console.print()
            warn("One or more devices are in a problematic state:")
            for p in problems:
                console.print(f"    [yellow]•[/yellow] {p}")

    console.print()
    if not Confirm.ask("  All devices recognised? Proceed with deployment?", default=True):
        console.print("\n[yellow]Deployment cancelled.[/yellow]")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Step 3 – Configure VLANs
# ---------------------------------------------------------------------------

def _build_network_payload(vlan_cfg: dict, existing_networks: list[dict]) -> dict | None:
    """Convert a vlan_config.yaml entry to a UniFi API networkconf payload.

    Returns None if the VLAN already exists (matched by VLAN ID).
    """
    vlan_id = vlan_cfg["id"]
    for net in existing_networks:
        if net.get("vlan") == vlan_id:
            return None  # already configured

    purpose = vlan_cfg.get("purpose", "corporate")
    payload: dict = {
        "name": vlan_cfg["name"],
        "purpose": purpose,
        "vlan": vlan_id,
        "vlan_enabled": True,
        "networkgroup": "LAN",
        "igmp_snooping": vlan_cfg.get("igmp_snooping", True),
        # dhcpguard_enabled omitted: enabling it requires a trusted DHCP server IP
        # which the API enforces at creation time (api.err.MissingIPAddress).
    }

    # Add L3/DHCP config (not needed for pure VLAN-only networks)
    if vlan_cfg.get("dhcp_enabled"):
        gw = vlan_cfg["gateway"]
        # UniFi expects "x.x.x.x/prefix" format using the gateway IP
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


def configure_vlans(client: UniFiClient, vlan_configs: list[dict], dry_run: bool) -> dict[int, dict]:
    """Create VLANs on the controller.  Returns map of vlan_id -> network object."""
    step_header("Step 3 · Configure VLANs")

    existing = [] if dry_run else client.get_networks()
    network_map: dict[int, dict] = {}

    # Pre-populate existing entries
    for net in existing:
        vid = net.get("vlan")
        if vid:
            network_map[vid] = net

    for vcfg in vlan_configs:
        vlan_id = vcfg["id"]
        name = vcfg["name"]

        if vlan_id in network_map:
            warn(f"VLAN {vlan_id} ({name}) already exists – skipping creation")
            continue

        payload = _build_network_payload(vcfg, existing)
        if payload is None:
            warn(f"VLAN {vlan_id} ({name}) already exists – skipping")
            continue

        if dry_run:
            ok(f"[DRY RUN] Would create VLAN {vlan_id} – {name}")
            network_map[vlan_id] = {"_id": f"dry-run-{vlan_id}", "vlan": vlan_id, **payload}
            continue

        try:
            created = client.create_network(payload)
            network_map[vlan_id] = created
            ok(f"Created VLAN {vlan_id} – {name}  (id={created.get('_id', '?')})")
        except UniFiAPIError as exc:
            fail(f"Failed to create VLAN {vlan_id} ({name}): {exc}")

    return network_map


# ---------------------------------------------------------------------------
# Step 4 – Discover, adopt, configure devices
# ---------------------------------------------------------------------------

DEVICE_TYPE_LABELS = {
    "udm": "UniFi Dream Machine",
    "uxg": "UniFi Gateway (UXG)",
    "ugw": "UniFi Security Gateway",
    "usw": "UniFi Switch",
    "uap": "UniFi Access Point",
    "uvc": "UniFi Protect Camera",
}


def discover_and_adopt(client: UniFiClient, dry_run: bool) -> list[dict]:
    step_header("Step 4 · Discover & Adopt Devices")

    if dry_run:
        warn("[DRY RUN] Returning mock device list")
        return []

    all_devices = client.get_devices()

    if not all_devices:
        warn("No devices found. Are any UniFi devices connected?")
        return []

    # Resolve gateway's real product name for the udm device row
    gw_product_name = client.get_gateway_product_name()

    # Display what we found
    table = Table(title="Discovered Devices", show_header=True, header_style="bold cyan")
    table.add_column("Name / MAC")
    table.add_column("Type")
    table.add_column("IP")
    table.add_column("State")

    for dev in all_devices:
        state = dev.get("state", -1)
        state_str = {
            0: "[yellow]Disconnected[/yellow]",
            1: "[green]Connected[/green]",
            2: "[yellow]Pending Adoption[/yellow]",
            4: "[blue]Upgrading[/blue]",
            5: "[blue]Provisioning[/blue]",
            6: "[dim]Heartbeat Missed[/dim]",
            7: "[blue]Adopting[/blue]",
            10: "[red]Adopt Failed[/red]",
        }.get(state, f"[dim]{state}[/dim]")

        dtype = dev.get("type", "")
        type_label = gw_product_name if dtype == "udm" else DEVICE_TYPE_LABELS.get(dtype, dtype or "?")

        table.add_row(
            dev.get("name") or dev.get("mac", ""),
            type_label,
            dev.get("ip", ""),
            state_str,
        )
    console.print(table)

    # Adopt any pending devices (state 2 = pending adoption, adopted=False = not yet adopted)
    pending = [d for d in all_devices if not d.get("adopted", True)]
    if not pending:
        ok("No devices pending adoption")
        return all_devices

    console.print(f"\n  [bold]{len(pending)}[/bold] device(s) pending adoption:")
    for dev in pending:
        mac = dev.get("mac", "")
        name = dev.get("name") or mac
        if Confirm.ask(f"  Adopt [bold]{name}[/bold] ({mac})?", default=True):
            try:
                client.adopt_device(mac)
                ok(f"Adoption command sent to {name}")
            except UniFiAPIError as exc:
                fail(f"Could not adopt {name}: {exc}")

    # Wait for adopted devices to reach state=1 (Connected) before returning
    if pending:
        console.print("\n  Waiting for newly adopted devices to come fully online (up to 3 min)…")
        adopted_macs = {d.get("mac", "").lower() for d in pending}
        deadline = time.time() + 180
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), transient=True) as prog:
            task = prog.add_task("Waiting for devices…", total=180)
            while time.time() < deadline:
                elapsed = 180 - (deadline - time.time())
                prog.update(task, completed=elapsed)
                time.sleep(5)
                all_devices = client.get_devices()
                pending_still = [
                    d for d in all_devices
                    if d.get("mac", "").lower() in adopted_macs and d.get("state") != 1
                ]
                if not pending_still:
                    break
                states = {d.get("mac", ""): d.get("state") for d in pending_still}
                prog.update(task, description=f"Waiting… still provisioning: {states}")

        all_devices = client.get_devices()
        for dev in all_devices:
            if dev.get("mac", "").lower() in adopted_macs:
                state = dev.get("state")
                name = dev.get("name") or dev.get("mac", "")
                if state == 1:
                    ok(f"{name} is online (state=Connected)")
                else:
                    state_label = {10: "Adopt Failed", 5: "Provisioning", 6: "Heartbeat Missed"}.get(state, str(state))
                    warn(f"{name} is not yet fully online (state={state_label}) — port config will be skipped")

    return all_devices


# ---------------------------------------------------------------------------
# Step 5 – Apply VLAN port profiles
# ---------------------------------------------------------------------------

def _get_or_create_port_profile(client: UniFiClient, profile_def: dict,
                                  existing_profiles: list[dict],
                                  network_map: dict[int, dict]) -> str | None:
    """Find or create a port profile.  Returns the profile _id."""
    profile_name = profile_def["name"]

    # Check if it already exists
    for p in existing_profiles:
        if p.get("name") == profile_name:
            return p["_id"]

    native_vlan_id = profile_def.get("native_vlan_id")
    tagged_vlan_ids: list[int] = profile_def.get("tagged_vlan_ids", [])

    native_net = network_map.get(native_vlan_id)
    if not native_net:
        warn(f"Network for native VLAN {native_vlan_id} not found; skipping profile '{profile_name}'")
        return None

    tagged_ids = []
    for vid in tagged_vlan_ids:
        net = network_map.get(vid)
        if net:
            tagged_ids.append(net["_id"])
        else:
            warn(f"  Network for tagged VLAN {vid} not found; omitting from '{profile_name}'")

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
        fail(f"Could not create port profile '{profile_name}': {exc}")
        return None


def apply_vlan_to_ports(
    client: UniFiClient,
    devices: list[dict],
    network_map: dict[int, dict],
    device_profiles_cfg: dict,
    dry_run: bool,
) -> None:
    """
    Applies VLAN port profiles to switches and configures APs.

    Safe ordering to prevent adoption/management loops:
      1. Create all port profiles first (no impact on traffic yet)
      2. Configure switch ACCESS ports (safe – only affects endpoint traffic)
      3. Configure switch TRUNK/UPLINK ports LAST (touching these risks dropping mgmt)
      4. Configure APs (WLAN VLAN mapping, no port reconfiguration needed)
    """
    step_header("Step 5 · Apply VLAN Port Settings")

    if dry_run:
        warn("[DRY RUN] Skipping port configuration")
        return

    # -- Build port profiles --
    pp_cfgs: dict = device_profiles_cfg.get("port_profiles", {})
    existing_profiles = client.get_port_profiles()

    profile_id_map: dict[str, str] = {}  # profile_key -> _id
    console.print("  Creating/verifying port profiles…")
    for key, pp_def in pp_cfgs.items():
        pid = _get_or_create_port_profile(client, pp_def, existing_profiles, network_map)
        if pid:
            profile_id_map[key] = pid
            ok(f"Port profile ready: {pp_def['name']}  (id={pid})")

    trunk_profile_id = profile_id_map.get("trunk_all")
    corp_profile_id = profile_id_map.get("corporate_access")
    guest_profile_id = profile_id_map.get("guest_access")
    mgmt_profile_id = profile_id_map.get("management_access")

    switches = [d for d in devices if d.get("type") == "usw"]
    aps = [d for d in devices if d.get("type") == "uap"]

    # -- Configure switches --
    for switch in switches:
        device_id = switch.get("_id", "")
        name = switch.get("name") or switch.get("mac", "")
        state = switch.get("state")
        ports: list[dict] = switch.get("port_table", [])

        if state != 1:
            warn(f"  Skipping {name} – not fully online (state={state}). Run again once it's Connected.")
            continue

        if not ports:
            warn(f"  No port table available for {name} – cannot configure ports")
            continue

        console.print(f"\n  Configuring switch: [bold]{name}[/bold] ({len(ports)} ports)")

        # Identify uplink port: highest-index port or the one tagged as uplink
        uplink_idx = max((p.get("port_idx", 0) for p in ports), default=0)

        access_overrides: list[dict] = []
        trunk_overrides: list[dict] = []

        for port in ports:
            idx = port.get("port_idx", 0)
            if idx == uplink_idx:
                # Uplink/trunk – configure LAST
                if trunk_profile_id:
                    trunk_overrides.append({
                        "port_idx": idx,
                        "portconf_id": trunk_profile_id,
                        "name": f"Uplink-{idx}",
                    })
            else:
                # Default access ports -> Corporate VLAN
                if corp_profile_id:
                    access_overrides.append({
                        "port_idx": idx,
                        "portconf_id": corp_profile_id,
                        "name": f"Access-{idx}",
                    })

        # Step A: apply access port overrides first
        if access_overrides and Confirm.ask(
            f"  Apply access port profiles to {name}?", default=True
        ):
            try:
                client.update_device_port_overrides(device_id, access_overrides)
                ok(f"Access ports configured on {name}")
                time.sleep(3)  # brief pause before touching trunk
            except UniFiAPIError as exc:
                fail(f"Could not set access ports on {name}: {exc}")

        # Step B: apply trunk/uplink overrides last
        if trunk_overrides and Confirm.ask(
            f"  Apply trunk port profile to uplink port on {name}? "
            f"[yellow](connectivity may briefly drop)[/yellow]",
            default=True,
        ):
            try:
                client.update_device_port_overrides(device_id, access_overrides + trunk_overrides)
                ok(f"Trunk port configured on {name}")
            except UniFiAPIError as exc:
                fail(f"Could not set trunk port on {name}: {exc}")

    # -- Configure APs (WLAN VLAN mapping is handled by the WLAN group, not port overrides) --
    if aps:
        console.print()
        warn(
            "AP WLAN-to-VLAN mapping requires creating WLAN groups via the Network app UI "
            "or an additional API call to /rest/wlangroup and /rest/wlan.\n"
            "  The VLANs are now configured on the controller – assign SSIDs to VLAN IDs "
            "20 (Corporate) and 30 (Guest) in Settings → WiFi."
        )
        for ap in aps:
            ok(f"AP [bold]{ap.get('name') or ap.get('mac', '')}[/bold] – no port config needed (wireless device)")


# ---------------------------------------------------------------------------
# Step 6 – Generate documentation
# ---------------------------------------------------------------------------

def generate_docs(client: UniFiClient, output_dir: Path, dry_run: bool) -> None:
    step_header("Step 6 · Generate Inventory & Documentation")

    output_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        warn("[DRY RUN] Skipping doc generation")
        return

    inventory = inv.collect_inventory(client)

    json_path = inv.generate_json(inventory, output_dir)
    ok(f"JSON inventory:  {json_path}")

    csv_path = inv.generate_csv(inventory, output_dir)
    ok(f"CSV inventory:   {csv_path}")

    xlsx_path = inv.generate_xlsx(inventory, output_dir)
    if xlsx_path:
        ok(f"Excel workbook:  {xlsx_path}")
    else:
        warn("openpyxl not installed – Excel output skipped (pip install openpyxl)")

    # Print summary table
    console.print()
    table = Table(title="Device Summary", header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("MAC")
    table.add_column("IP")
    table.add_column("Firmware")
    for dev in inventory["devices"]:
        table.add_row(
            dev["name"], dev["type_label"], dev["mac"], dev["ip"], dev["firmware"]
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BC10 Deploy Tool – Automated UniFi Gateway deployment"
    )
    p.add_argument("--host", default=DEFAULT_HOST, help="Gateway IP (default: 192.168.1.1)")
    p.add_argument("--dry-run", action="store_true", help="Simulate without making changes")
    p.add_argument(
        "--output-dir",
        default="./output",
        help="Directory for generated inventory files (default: ./output)",
    )
    p.add_argument(
        "--setup",
        action="store_true",
        help="Run initial device setup before deploying (safe on already-configured devices)",
    )
    p.add_argument(
        "--setup-config",
        default="config/setup_config.yaml",
        help="Path to setup_config.yaml (default: config/setup_config.yaml)",
    )
    p.add_argument("--username", default=None, help="Gateway username (skips default credential attempt)")
    p.add_argument("--password", default=None, help="Gateway password")
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    console.print(
        Panel.fit(
            "[bold cyan]BC10 Deploy Tool[/bold cyan]\n"
            "UniFi Gateway Max / Ultra – Automated Deployment\n"
            + ("[bold yellow]DRY RUN MODE[/bold yellow]" if args.dry_run else ""),
            border_style="cyan",
        )
    )

    # Step 0: initial setup (factory-default devices)
    setup_username: str | None = None
    setup_password: str | None = None
    if args.setup:
        setup_cfg_path = Path(args.setup_config)
        if not setup_cfg_path.exists():
            fail(f"Setup config not found: {setup_cfg_path}")
            sys.exit(1)
        setup_cfg = load_yaml(setup_cfg_path)
        setup_username, setup_password = initial_setup(args.host, setup_cfg, args.dry_run)

    # Load config files
    vlan_cfg_path = Path("config/vlan_config.yaml")
    device_cfg_path = Path("config/device_profiles.yaml")

    if not vlan_cfg_path.exists():
        fail("config/vlan_config.yaml not found. Aborting.")
        sys.exit(1)

    vlan_config = load_yaml(vlan_cfg_path)
    vlan_list: list[dict] = vlan_config.get("vlans", [])

    device_profiles_cfg = load_yaml(device_cfg_path) if device_cfg_path.exists() else {}

    output_dir = Path(args.output_dir)

    # Step 1: Connect  (--username/--password take priority over --setup credentials)
    client = connect_to_gateway(
        args.host,
        args.dry_run,
        preferred_username=args.username or setup_username,
        preferred_password=args.password or setup_password,
    )

    # Step 1b: Adopt switches before pre-flight so downstream devices are visible
    adopt_switches(client, args.dry_run)

    # Step 2: Pre-flight device scan
    if not args.dry_run:
        preflight_check(client)

    # Step 3: VLANs
    network_map = configure_vlans(client, vlan_list, args.dry_run)

    # Step 4: Discover & adopt
    devices = discover_and_adopt(client, args.dry_run)

    # Step 5: Apply VLANs to ports
    apply_vlan_to_ports(client, devices, network_map, device_profiles_cfg, args.dry_run)

    # Step 6: Docs
    generate_docs(client, output_dir, args.dry_run)

    # Done
    if not args.dry_run:
        client.logout()

    console.print()
    console.print(Panel.fit("[bold green]Deployment complete![/bold green]", border_style="green"))


if __name__ == "__main__":
    main()
