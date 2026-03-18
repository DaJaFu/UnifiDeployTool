# BC10 Deploy Tool – Proposed Improvements

## Security

**1. Credential management**
The password is prompted via `getpass` but never stored anywhere secure. For repeated or automated deployments, integrate a secrets manager (e.g. `keyring`, HashiCorp Vault, or a local encrypted store) so passwords aren't re-entered or accidentally logged.

**2. SSL certificate pinning / validation**
`verify_ssl=False` is currently unconditional for factory devices. After first contact, the tool should fetch and pin the device's self-signed cert fingerprint, then enforce it on all subsequent connections. This prevents MITM attacks during the setup window when the device is most exposed.

**3. Immediate credential rotation**
After connecting with `ubnt`/`ubnt`, the tool should automatically create a new strong admin account and disable or change the factory default before doing anything else. Right now a failed deployment could leave factory creds active on a partially-configured device.

**4. Firewall rule provisioning**
VLANs are created with `inter_vlan_routing: false` in the config, but no firewall rules are actually pushed to enforce it. The UniFi API supports creating firewall rule groups — without them, the VLAN isolation is only as good as whatever default policy the gateway ships with.

**5. Audit log**
No record is kept of what changes were made, when, and by whom. A signed, append-only local log (or syslog forward) of every API call that mutates config would be essential for regulated environments and incident response.

**6. VLAN config schema validation**
`vlan_config.yaml` is loaded and used directly with no validation. A malformed or tampered config file could push garbage to the gateway. Add `pydantic` or `cerberus` schema validation before any API calls are made.

---

## Scalability

**7. Multi-site / multi-gateway support**
The tool is hardcoded for a single gateway at `192.168.1.1`. A `sites.yaml` manifest listing multiple gateways (host, site name, config file path) would let it deploy a fleet of locations sequentially or in parallel with `concurrent.futures`.

**8. Idempotent re-runs**
Currently, re-running the tool on a partially-configured gateway will skip VLANs that already exist but won't detect or correct configuration drift (e.g. wrong subnet, wrong DHCP range). Adding a reconcile mode that diffs the desired state against the live state and patches only what's wrong would make repeated deployments safe.

**9. Non-interactive / headless mode**
Steps 3–5 all use `Confirm.ask()` which blocks on stdin. A `--yes` / `--non-interactive` flag that auto-approves all steps (or a per-step `--skip-firmware`, `--skip-adoption` flag) is needed for scripted or CI/CD pipelines.

**10. AP WLAN provisioning is unfinished**
The code prints a warning and does nothing for access points. The `/rest/wlangroup` and `/rest/wlan` endpoints exist in the API — completing this step means APs could be fully configured without any manual UI work, which is the main scalability gap right now.

**11. Uplink detection is naive**
The uplink port is assumed to be the highest-index port (`max(port_idx)`). On multi-uplink switches or switches with SFP uplinks this will be wrong. The UniFi API includes `uplink` metadata on the port table that should be used instead.

**12. Firmware upgrade – NOT IMPLEMENTED (blocked)**
Firmware upgrade was attempted but removed. Three blockers were hit, all unresolved:

- **CDN is dead.** Ubiquiti's public firmware download URLs (`dl.ui.com/unifi/firmware/...`) return 404.
  Signed URLs are only available via authenticated API calls — there is no static public URL to fetch.
- **SSH is disabled.** The intended approach was SCP the `.bin` to `/tmp/fwupdate.bin` and run
  `ubnt-systool fwupdate /tmp/fwupdate.bin`. SSH is disabled by default on current UniFi OS builds
  and cannot be enabled without first logging into the UI.
- **REST API rejects file uploads.** `POST /api/firmware-update` returns 502 for multipart uploads;
  the endpoint only accepts JSON for triggering OTA updates, not local file uploads.

Before reimplementing: determine the correct upgrade path (UI-assisted, UNMS, or a newly discovered
API endpoint) and verify it works on a test device before adding it back to the tool.

**13. Deployment state file**
If the tool crashes partway through (e.g. gateway reboots during firmware upgrade), there's no way to resume. A simple JSON state file written after each completed step would let the tool pick up where it left off.

---

## Ease of Use

**14. Config file path as CLI argument**
`vlan_config.yaml` and `device_profiles.yaml` are always loaded from the current working directory. `--config` and `--profiles` flags would make the tool usable from any directory and allow multiple config sets to be maintained for different deployment types.

**15. Pre-flight check step** **[COMPLETE]**
Before touching anything, run a read-only check: verify gateway reachability, confirm VLAN IDs don't conflict with anything already on the network, validate the config YAML, and print a summary of what will be changed. Give the user a chance to abort before any writes happen.

**16. Post-deployment verification**
After all steps complete, re-query the gateway and cross-check: are all VLANs present with the right subnets? Are all adopted devices in a `connected` state? Are port profiles applied correctly? Right now the tool trusts that API calls succeeded, but doesn't verify the actual resulting state.

**17. Structured deployment report**
The inventory output captures a device snapshot, but doesn't record what the tool actually did (e.g. "VLAN 30 created", "USW-24 ports 1–22 set to Corporate"). A deployment report distinct from the inventory — showing before/after changes — would make handoff to a client much cleaner.

**18. Packaging**
Currently requires a Python environment and `pip install`. Packaging as a self-contained binary with `PyInstaller` or `Nuitka`, or as a Docker container, would make this usable by field technicians without any Python knowledge or internet access on the deployment laptop.

**19. Disable unused ports**
After applying port profiles, the tool should apply a "disabled" profile to any port that was not assigned a role (access, trunk, or uplink). This is a standard hardening step that prevents rogue devices from gaining network access by physically plugging in.

Exception: leave one spare port on the **gateway** open (enabled, on the Management VLAN) so a technician can plug in directly for post-deployment work. This port should be documented in the deployment report and closed manually once no longer needed.

**20. Generic device recovery script**
Born from `recover_switch.py` (written to recover a USW stuck in ADOPT_FAILED). If API-based factory reset proves reliable, generalize it into a standalone `recover_device.py` utility that accepts `--host`, `--mac`, and `--id` as arguments and tries the full reset sequence: `cmd: reset-default` → `cmd: factory-reset` → v2 DELETE. Useful as a field tool any time a device gets stuck without needing physical access to the reset button.
