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
    # First-boot setup (factory-default devices only)
    # ------------------------------------------------------------------

    def check_setup_status(self) -> dict:
        """
        Call GET /api/system without authentication.
        Available on factory-default devices before any account exists.
        Returns the full system info dict.
        Key fields: isSetup (bool), deviceState (str), hardware.firmwareVersion (str).
        Raises UniFiConnectionError if the device is unreachable.
        """
        try:
            resp = self.session.get(
                f"{self.base_url}/api/system", timeout=self.timeout
            )
        except requests.exceptions.ConnectionError as exc:
            raise UniFiConnectionError(f"Cannot reach {self.base_url}: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise UniFiConnectionError(
                f"GET /api/system returned HTTP {resp.status_code}"
            )
        return resp.json()

    def needs_initial_setup(self) -> bool:
        """Return True if the device is in factory-default / not-yet-configured state."""
        info = self.check_setup_status()
        return not info.get("isSetup", True)

    def initial_setup(
        self,
        device_name: str,
        username: str,
        password: str,
        country: int = 840,
        timezone: str | None = None,
    ) -> dict:
        """
        Perform first-boot device setup via POST /api/setup.
        Only valid when the device has not been configured yet (isSetup=false).

        Args:
            device_name: Friendly name for the device/site (shown in UniFi dashboard).
            username:    Local admin username to create.
            password:    Admin password.
            country:     ISO 3166-1 numeric country code (default 840 = United States).
            timezone:    IANA timezone string e.g. "America/New_York" (optional).

        Returns the created user object from the API.
        Raises UniFiAPIError on failure.
        """
        payload: dict = {
            "name":     device_name,
            "username": username,
            "password": password,
            "country":  country,
        }
        if timezone:
            payload["timezone"] = timezone

        try:
            resp = self.session.post(
                f"{self.base_url}/api/setup",
                json=payload,
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise UniFiConnectionError(f"Cannot reach {self.base_url}: {exc}") from exc

        if resp.status_code not in (200, 201):
            try:
                body = resp.json()
                msg = body.get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise UniFiAPIError(
                f"Initial setup failed (HTTP {resp.status_code}): {msg}",
                status_code=resp.status_code,
            )

        log.debug("Initial setup complete, admin user '%s' created", username)
        return resp.json()

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

        if resp.status_code in (401, 403):
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

    def get_gateway_product_name(self) -> str:
        """Return the human-readable product name from /api/system hardware info.

        e.g. 'UniFi Cloud Gateway Ultra', 'UniFi Dream Machine Pro', etc.
        Falls back to the shortname model code if the field is unavailable.
        """
        try:
            info = self.get_system_info()
            hw = info.get("hardware", {})
            return hw.get("name") or hw.get("shortname") or "UniFi Gateway"
        except Exception:
            return "UniFi Gateway"


    # ------------------------------------------------------------------
    # Network application - devices
    # ------------------------------------------------------------------

    def get_devices(self) -> list[dict]:
        """Return all devices visible to the Network app (adopted + pending), with full data."""
        data = self._net_get("/stat/device")
        return data if isinstance(data, list) else []

    def get_pending_devices(self) -> list[dict]:
        """Return devices that are visible but not yet adopted."""
        data = self._net_get("/stat/device")
        if not isinstance(data, list):
            return []
        return [d for d in data if not d.get("adopted", True)]

    def adopt_device(self, mac: str) -> dict:
        """Send an adopt command to a pending device."""
        return self._net_cmd({"cmd": "adopt", "mac": mac.lower()})

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
        log.debug("GET %s response %s: %s", path, resp.status_code, resp.text[:2000])
        self._raise_for_status(resp)
        body = resp.json()
        return body.get("data", body)

    def _net_post(self, path: str, payload: dict) -> list | dict:
        url = f"{self.base_url}{NETWORK_API}{path}"
        log.debug("POST %s payload: %s", path, payload)
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        log.debug("POST %s response %s: %s", path, resp.status_code, resp.text[:500])
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
