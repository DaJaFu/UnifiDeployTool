# Playwright Wizard Automation – Research Notes

## Why Playwright Was Considered

UniFi OS 3.x factory-default devices (UCG Ultra, UCG Max) enforce a first-boot setup
wizard before SSH or the management API becomes accessible. No public documentation or
community reverse-engineering of the wizard API existed at the time of investigation.
Playwright (headless browser automation) was chosen as a reliable fallback.

## What Was Built

- `setup_wizard.py` — 7-stage Playwright automation (wait → detect state → select local
  account → fill credentials → set identity → skip optional steps → finish)
- `setup_config.yaml` — Pre-deployment config (host, admin credentials, timezone, country,
  browser options)
- `requirements.txt` updated with `playwright>=1.40.0`
- `main.py` updated with `--wizard` / `--headed` / `--setup-config` flags and a Step 0
  orchestration function that invokes the wizard as a subprocess

## Why It Was Scrapped

During live testing against a factory-reset UCG Ultra (firmware 4.0.6, UniFi OS), direct
API probing revealed that `POST /api/setup` is a real, undocumented but accessible endpoint
that handles device initialisation without any browser interaction required. The direct API
approach is cleaner, faster, and more maintainable.

## Live Device Observations (UCG Ultra, FW 4.0.6)

### `/api/system` — Unauthenticated, pre-setup

`GET /api/system` returns full device info without credentials when `isSetup: false`.
Key fields observed:

```json
{
  "isSetup": false,
  "deviceState": "notSetup",
  "isSingleUser": false,
  "isSsoEnabled": false,
  "ssh": true,
  "hardware": {
    "name": "UniFi Cloud Gateway Ultra",
    "shortname": "UDRULT",
    "firmwareVersion": "4.0.6"
  },
  "hostname": "UCGULT",
  "ip": "192.168.1.1"
}
```

This endpoint can be used to detect whether a device still needs wizard completion before
attempting to connect.

### Wizard UI (rendered via Playwright)

The wizard is a compiled React SPA — raw HTML contains no form elements. After JS
renders, the first screen shows:

```
Plug in Internet Cable
Connect your modem to the UCG Ultra's internet port

[Other Configuration Options]   [Test Connection]
```

Two buttons are present:
- **"Other Configuration Options"** — proceeds without WAN
- **"Test Connection"** — requires WAN uplink

The wizard does not require a UI.com / cloud account; local-only setup is supported.

### `/api/setup` — The Direct Setup Endpoint

`POST /api/setup` returns HTTP 405 on GET, confirming it exists. On POST with an empty
body it returns HTTP 400 with Zod validation errors that reveal the required schema:

```json
{
  "validationErrors": [{
    "code": "invalid_union",
    "unionErrors": [
      { "issues": [
          { "path": ["name"],     "message": "Required" },
          { "path": ["password"], "message": "Required" }
      ]},
      { "issues": [
          { "path": ["hostname"], "message": "Required" },
          { "path": ["password"], "message": "Required" }
      ]}
    ]
  }]
}
```

**Schema (union of two forms):**

| Field      | Type   | Required | Notes                                    |
|------------|--------|----------|------------------------------------------|
| `name`     | string | Yes*     | Local admin username (*or use `hostname`)|
| `hostname` | string | Yes*     | Alternative to `name`                    |
| `password` | string | Yes      |                                          |
| `country`  | number | Yes      | ISO 3166-1 **numeric** code (US = 840)   |
| `timezone` | string | ?        | IANA string — presence unconfirmed       |

Country must be a **numeric** code, not an alpha-2 string ("US" caused a type error).

### SSH Access

`"ssh": true` appears in the pre-setup `/api/system` response, but SSH with
`ubnt`/`ubnt` (password auth) was denied — only public-key auth was offered by the
server. SSH was not pursued further once the API path was confirmed.

## Confirmed: `POST /api/setup` Full Schema

Tested live against UCG Ultra firmware 4.0.6. The endpoint requires **two separate
concerns** in one payload: a device identity field AND a user credential field.

### Working payload

```json
{
  "name":     "My-Gateway",
  "username": "admin",
  "password": "<your-password>",
  "country":  840
}
```

**HTTP 200** — returns the full created user object including JWT `deviceToken`,
roles (`Owner`), permissions, and group membership.

### Full schema (derived from Zod validation errors)

| Field      | Type   | Required | Notes                                             |
|------------|--------|----------|---------------------------------------------------|
| `name`     | string | Yes*     | Device/site name (*alternative: `hostname`)       |
| `hostname` | string | Yes*     | Alternative to `name` for device identity         |
| `username` | string | Yes†     | Local admin username (†alternative: `ssoUser`)    |
| `ssoUser`  | object | Yes†     | Alternative to `username` for UI.com SSO login    |
| `password` | string | Yes      | Admin password                                    |
| `country`  | number | Yes      | ISO 3166-1 **numeric** code — US=840, GB=826      |
| `timezone` | string | No       | IANA string (e.g. `"America/New_York"`) — optional|

Key pitfall: `country` **must be a number**, not an alpha-2 string. Sending `"US"`
produces a type validation error.

### Login after setup

```http
POST /api/auth/login
Content-Type: application/json

{"username": "admin", "password": "<your-password>"}
```

Returns HTTP 200 with the same user object plus `deviceToken` (JWT). The standard
`X-CSRF-Token` header flow used by `unifi_client.py` also works normally post-setup.

### Pre-setup state detection

`GET /api/system` is accessible **without authentication** when the device is in
factory-default / not-yet-setup state. Key fields to check:

```json
{ "isSetup": false, "deviceState": "notSetup" }
```

Use this to detect whether a device needs `POST /api/setup` before attempting login.

## Selector Notes (if Playwright is revisited)

If Playwright automation is needed again for a future firmware version that locks down
the API:

- The page is a React SPA — wait for `networkidle` + extra `sleep(5)` before querying
- Use `page.inner_text('body')` to dump visible text; do not rely on raw HTML
- First-screen buttons: `"Other Configuration Options"` and `"Test Connection"`
- All CSS class names are hashed (e.g. `button__Ad6q97AA`) — use text-based selectors
- Screenshots at each stage are essential for debugging; save to `./wizard_screenshots/`

## Steps Taken During Investigation

1. **Pinged 192.168.1.1** — device reachable, <1ms RTT
2. **Installed Playwright + Chromium** — confirmed JS SPA, no static form elements in HTML
3. **Probed `/api/system` (GET, unauthenticated)** — HTTP 200, revealed `isSetup: false`,
   `deviceState: notSetup`, firmware 4.0.6, model UDRULT (UCG Ultra), `ssh: true`
4. **Scanned candidate API endpoints** — only `/api/setup` returned 405 (exists, GET not
   allowed); all others 404
5. **POST `/api/setup` with `{}`** — HTTP 400 Zod error revealed `name`/`hostname` union
   and `password` requirement
6. **POST with `name`+`password`+`country:"US"`** — revealed `country` must be a number
7. **POST with `username`+`password`+`country:840`** — revealed separate `username`/
   `ssoUser` union; `name`/`hostname` still required alongside it
8. **POST with `name`+`username`+`password`+`country:840`** — **HTTP 200**, device
   configured, admin account created as Owner
9. **POST `/api/auth/login`** — **HTTP 200**, confirmed login works with new credentials,
   JWT token returned
10. **Scrapped Playwright** — direct API is cleaner; rolled back all Playwright changes
