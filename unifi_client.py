"""
unifi_client.py - UniFi OS API client for UXG-Max / UXG-Ultra

Targets the UniFi OS local management API (not the cloud key or hosted controller).
All UniFi OS devices expose:
  - OS API:      https://<ip>/api/...
  - Network app: https://<ip>/proxy/network/api/s/<site>/...
"""

import time
import logging
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

NETWORK_API = "/proxy/network/api/s/default"


class UniFiConnectionError(Exception):
    pass


class UniFiAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class UniFiClient:
    """Session-based client for the UniFi OS local API."""

    def __init__(self, host: str, verify_ssl: bool = False, timeout: int = 15):
        self.base_url = f"https://{host}"
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update({"Content-Type": "application/json"})
        self._csrf_token: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        """Authenticate against UniFi OS.  Stores session cookie + CSRF token."""
        url = f"{self.base_url}/api/auth/login"
        try:
            resp = self.session.post(
                url,
                json={"username": username, "password": password},
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise UniFiConnectionError(f"Cannot reach {self.base_url}: {exc}") from exc

        if resp.status_code == 401:
            raise UniFiConnectionError("Authentication failed: bad credentials")

        if resp.status_code not in (200, 201):
            raise UniFiConnectionError(
                f"Login failed with HTTP {resp.status_code}: {resp.text[:200]}"
            )

        # UniFi OS returns the CSRF token in the response header
        csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get("x-csrf-token")
        if csrf:
            self._csrf_token = csrf
            self.session.headers.update({"X-CSRF-Token": csrf})

        log.debug("Login successful as %s", username)

    def logout(self) -> None:
        try:
            self.session.post(f"{self.base_url}/api/auth/logout", timeout=self.timeout)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # System / OS level
    # ------------------------------------------------------------------

    def get_system_info(self) -> dict:
        """Return UniFi OS system information (firmware, model, etc.)."""
        return self._os_get("/api/system")

    def get_firmware_update_status(self) -> dict:
        """Check whether a firmware update is available for the gateway itself."""
        return self._os_get("/api/firmware-update")

    def trigger_gateway_firmware_update(self) -> dict:
        """Trigger an OS-level firmware upgrade on the gateway."""
        return self._os_post("/api/firmware-update", {})

    # ------------------------------------------------------------------
    # Network application - devices
    # ------------------------------------------------------------------

    def get_devices(self) -> list[dict]:
        """Return all devices visible to the Network app (adopted + pending)."""
        data = self._net_get("/stat/device-basic")
        return data if isinstance(data, list) else []

    def get_pending_devices(self) -> list[dict]:
        """Return devices that are visible but not yet adopted."""
        data = self._net_get("/stat/device")
        if not isinstance(data, list):
            return []
        return [d for d in data if d.get("state") == 0]  # 0 = pending adoption

    def adopt_device(self, mac: str) -> dict:
        """Send an adopt command to a pending device."""
        return self._net_cmd({"cmd": "adopt", "mac": mac.lower()})

    def upgrade_device_firmware(self, mac: str, firmware_url: str | None = None) -> dict:
        """Upgrade firmware on an adopted device.

        If firmware_url is omitted UniFi will use its CDN (requires internet access).
        For offline deployments, supply a URL reachable on the local network.
        """
        payload: dict = {"cmd": "upgrade", "mac": mac.lower()}
        if firmware_url:
            payload["url"] = firmware_url
        return self._net_cmd(payload)

    def wait_for_device(self, mac: str, timeout: int = 120, poll: int = 5) -> dict | None:
        """Poll until a device is connected (state == 1) or timeout expires."""
        deadline = time.time() + timeout
        mac = mac.lower()
        while time.time() < deadline:
            devices = self.get_devices()
            for dev in devices:
                if dev.get("mac", "").lower() == mac and dev.get("state") == 1:
                    return dev
            time.sleep(poll)
        return None

    # ------------------------------------------------------------------
    # Network application - VLANs / networks
    # ------------------------------------------------------------------

    def get_networks(self) -> list[dict]:
        """Return all configured networks/VLANs."""
        data = self._net_get("/rest/networkconf")
        return data if isinstance(data, list) else []

    def create_network(self, config: dict) -> dict:
        """Create a new network (VLAN).  Returns the created object."""
        data = self._net_post("/rest/networkconf", config)
        return data[0] if isinstance(data, list) and data else data

    def update_network(self, network_id: str, config: dict) -> dict:
        data = self._net_put(f"/rest/networkconf/{network_id}", config)
        return data[0] if isinstance(data, list) and data else data

    # ------------------------------------------------------------------
    # Network application - port profiles
    # ------------------------------------------------------------------

    def get_port_profiles(self) -> list[dict]:
        data = self._net_get("/rest/portconf")
        return data if isinstance(data, list) else []

    def create_port_profile(self, config: dict) -> dict:
        data = self._net_post("/rest/portconf", config)
        return data[0] if isinstance(data, list) and data else data

    def update_device_port_overrides(self, device_id: str, port_overrides: list[dict]) -> dict:
        """Apply port profile overrides to a specific switch."""
        data = self._net_put(f"/rest/device/{device_id}", {"port_overrides": port_overrides})
        return data[0] if isinstance(data, list) and data else data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _os_get(self, path: str) -> dict:
        resp = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
        self._raise_for_status(resp)
        return resp.json()

    def _os_post(self, path: str, payload: dict) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        self._raise_for_status(resp)
        return resp.json()

    def _net_get(self, path: str) -> list | dict:
        url = f"{self.base_url}{NETWORK_API}{path}"
        resp = self.session.get(url, timeout=self.timeout)
        self._raise_for_status(resp)
        body = resp.json()
        return body.get("data", body)

    def _net_post(self, path: str, payload: dict) -> list | dict:
        url = f"{self.base_url}{NETWORK_API}{path}"
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        self._raise_for_status(resp)
        body = resp.json()
        return body.get("data", body)

    def _net_put(self, path: str, payload: dict) -> list | dict:
        url = f"{self.base_url}{NETWORK_API}{path}"
        resp = self.session.put(url, json=payload, timeout=self.timeout)
        self._raise_for_status(resp)
        body = resp.json()
        return body.get("data", body)

    def _net_cmd(self, payload: dict) -> dict:
        url = f"{self.base_url}{NETWORK_API}/cmd/devmgr"
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        self._raise_for_status(resp)
        body = resp.json()
        return body.get("data", body)

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code in (200, 201):
            return
        try:
            body = resp.json()
            msg = body.get("meta", {}).get("msg", resp.text[:200])
        except Exception:
            msg = resp.text[:200]
        raise UniFiAPIError(f"HTTP {resp.status_code}: {msg}", status_code=resp.status_code)
