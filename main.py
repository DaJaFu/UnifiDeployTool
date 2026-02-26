#!/usr/bin/env python3
"""
BC10 Deploy Tool
----------------
Automated deployment tool for UniFi Gateway Max / Ultra (factory-default state).

Usage:
    python main.py [--host 192.168.1.1] [--dry-run] [--output-dir ./output]

Steps performed:
    1. Connect to gateway (try defaults, prompt on failure)
    2. Configure 3 VLANs from vlan_config.yaml
    3. Upgrade gateway and device firmware
    4. Discover, adopt, and configure connected UniFi devices
    5. Apply VLAN port profiles (safe ordering to prevent adoption loops)
    6. Generate inventory documentation
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

# Known latest firmware download URLs (Ubiquiti CDN).
# For OFFLINE deployment: place .bin files in ./firmware/ directory and the
# tool will prefer them over downloading.
KNOWN_FIRMWARE = {
    "UXG-Max":   "https://dl.ui.com/unifi/firmware/UXG-Max/latest/UXG-Max.bin",
    "UXG-Ultra": "https://dl.ui.com/unifi/firmware/UXG-Ultra/latest/UXG-Ultra.bin",
}

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
# Step 1 – Connect
# ---------------------------------------------------------------------------

def connect_to_gateway(host: str, dry_run: bool) -> UniFiClient:
    step_header("Step 1 · Connect to Gateway")
    console.print(f"  Target: [bold]{host}[/bold]")

    if dry_run:
        warn("DRY RUN – skipping real connection")
        return UniFiClient(host)

    # Warn about self-signed cert
    warn("SSL certificate verification is disabled (self-signed cert expected on factory device).")

    client = UniFiClient(host, verify_ssl=False)

    # Try factory defaults first
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
# Step 2 – Configure VLANs
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
        "enabled": True,
        "igmp_snooping": vlan_cfg.get("igmp_snooping", True),
        "dhcpguard_enabled": vlan_cfg.get("dhcp_guard", True),
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
    step_header("Step 2 · Configure VLANs")

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
# Step 3 – Firmware upgrade
# ---------------------------------------------------------------------------

def _find_local_firmware(model: str) -> str | None:
    """Look for a local firmware file in ./firmware/ matching the model name."""
    fw_dir = Path("firmware")
    if not fw_dir.exists():
        return None
    for ext in ("*.bin", "*.tar.gz", "*.tar"):
        for f in fw_dir.glob(ext):
            if model.lower() in f.name.lower():
                return f.as_posix()
    return None


def upgrade_firmware(client: UniFiClient, devices: list[dict], dry_run: bool) -> None:
    step_header("Step 3 · Upgrade Firmware")

    if dry_run:
        warn("[DRY RUN] Skipping firmware upgrade")
        return

    # Gateway self-upgrade (UniFi OS level)
    try:
        sys_info = client.get_system_info()
        gw_model = sys_info.get("hardware", {}).get("shortname", "")
        gw_fw = sys_info.get("firmware", {}).get("installed", "unknown")
        console.print(f"  Gateway model: [bold]{gw_model}[/bold]  firmware: {gw_fw}")

        local_fw = _find_local_firmware(gw_model)
        if local_fw:
            ok(f"Found local firmware for {gw_model}: {local_fw}")
            warn("Local gateway firmware upgrade requires SSH or manual upload – skipping automatic trigger.")
        else:
            if Confirm.ask("  Trigger gateway firmware check/upgrade via UniFi OS API?", default=False):
                client.trigger_gateway_firmware_update()
                ok("Firmware upgrade triggered on gateway. It will reboot when complete.")
                warn("Waiting 60 s for gateway to start upgrade…")
                time.sleep(60)
    except UniFiAPIError as exc:
        warn(f"Could not query gateway firmware status: {exc}")

    # Upgrade adopted devices
    for dev in devices:
        mac = dev.get("mac", "")
        model = dev.get("model", "")
        name = dev.get("name") or mac
        dtype = dev.get("type", "")

        if dtype in ("uxg", "ugw"):
            continue  # handled above

        local_fw = _find_local_firmware(model)
        fw_url = local_fw or KNOWN_FIRMWARE.get(model)

        if Confirm.ask(f"  Upgrade firmware on [bold]{name}[/bold] ({model})?", default=True):
            try:
                client.upgrade_device_firmware(mac, fw_url)
                ok(f"Upgrade triggered: {name}")
            except UniFiAPIError as exc:
                fail(f"Upgrade failed for {name}: {exc}")


# ---------------------------------------------------------------------------
# Step 4 – Discover, adopt, configure devices
# ---------------------------------------------------------------------------

DEVICE_TYPE_LABELS = {
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

    # Display what we found
    table = Table(title="Discovered Devices", show_header=True, header_style="bold cyan")
    table.add_column("Name / MAC")
    table.add_column("Type")
    table.add_column("IP")
    table.add_column("State")

    for dev in all_devices:
        state = dev.get("state", -1)
        state_str = {0: "[yellow]Pending[/yellow]", 1: "[green]Connected[/green]"}.get(
            state, f"[dim]{state}[/dim]"
        )
        table.add_row(
            dev.get("name") or dev.get("mac", ""),
            DEVICE_TYPE_LABELS.get(dev.get("type", ""), dev.get("type", "?")),
            dev.get("ip", ""),
            state_str,
        )
    console.print(table)

    # Adopt any pending devices
    pending = [d for d in all_devices if d.get("state") == 0]
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

    # Wait for adopted devices to come online
    if pending:
        console.print("\n  Waiting for newly adopted devices to provision (up to 2 min)…")
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as prog:
            prog.add_task("Waiting…", total=None)
            time.sleep(30)

        all_devices = client.get_devices()

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
        ports: list[dict] = switch.get("port_table", [])

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

    # Load config files
    vlan_cfg_path = Path("vlan_config.yaml")
    device_cfg_path = Path("device_profiles.yaml")

    if not vlan_cfg_path.exists():
        fail("vlan_config.yaml not found in current directory. Aborting.")
        sys.exit(1)

    vlan_config = load_yaml(vlan_cfg_path)
    vlan_list: list[dict] = vlan_config.get("vlans", [])

    device_profiles_cfg = load_yaml(device_cfg_path) if device_cfg_path.exists() else {}

    output_dir = Path(args.output_dir)

    # Step 1: Connect
    client = connect_to_gateway(args.host, args.dry_run)

    # Step 2: VLANs
    network_map = configure_vlans(client, vlan_list, args.dry_run)

    # Step 3: Firmware
    devices: list[dict] = [] if args.dry_run else client.get_devices()
    upgrade_firmware(client, devices, args.dry_run)

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
