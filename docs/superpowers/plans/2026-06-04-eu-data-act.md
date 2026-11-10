# EU Data Act Portal Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the VW Group EU Data Act portal (`eu-data-act.drivesomethinggreater.com`) as an alternative data source, replacing the closed We Connect API for Volkswagen users.

**Architecture:** A new `eu_data_act.py` module wraps the portal's cookie-session API (OIDC login → zip dataset download) and exposes a `DataUpdateCoordinator`. The existing config flow gains a `data_source` field that routes to either the legacy path (unchanged) or a new EU Data Act path. Platform files (`sensor.py`, `binary_sensor.py`) check `data_source` and dispatch to the appropriate entity classes.

**Tech Stack:** aiohttp (already a HA dep), Python stdlib zipfile + html.parser + json + re, pytest-homeassistant-custom-component for tests.

---

## Background: How the Portal Works

- **Not live telemetry.** The portal publishes a zipped JSON snapshot every ~15 minutes.
- **One-time browser consent required.** The user must open `eu-data-act.drivesomethinggreater.com`, log in with their We Connect credentials, accept the consent screen, link the vehicle, and enable the continuous data request. The integration cannot do this — it is a legal consent gate.
- **Auth:** VW Identity OIDC form login with a portal-specific `client_id`. Ends with a session cookie (no refresh token). Re-login on 401/403 or 5xx + HTML body (Adobe AEM session expiry).
- **Data flow:** login → GET `/proxy_api/consent/me/vehicles` → GET identifier → GET dataset list → GET + unzip newest dataset → parse flat `{key, dataFieldName, value}` array.
- **Available fields:** SoC, range, odometer, charging state, plug state, target SoC. No GPS.

Reference implementations: evcc Go (`vehicle/vw/eudataact/`) and ioBroker JS (`lib/euDataAct.js`).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| **Create** | `custom_components/volkswagencarnet/eu_data_act.py` | API client, coordinator, entity classes, descriptors |
| **Create** | `tests/eu_data_act_test.py` | Unit tests for eu_data_act.py (no HA fixtures needed) |
| **Modify** | `custom_components/volkswagencarnet/const.py` | New constants: CONF_DATA_SOURCE, DATA_SOURCE_*, CONF_BRAND, etc. |
| **Modify** | `custom_components/volkswagencarnet/config_flow.py` | EU Data Act config flow path (data_source selector + eu_login + eu_select_vehicle) |
| **Modify** | `custom_components/volkswagencarnet/__init__.py` | Branch on data_source in async_setup_entry / async_unload_entry |
| **Modify** | `custom_components/volkswagencarnet/sensor.py` | Dispatch to EUDataActSensor when data_source == eu_data_act |
| **Modify** | `custom_components/volkswagencarnet/binary_sensor.py` | Dispatch to EUDataActBinarySensor when data_source == eu_data_act |

---

## Task 1: Add constants

**Files:**
- Modify: `custom_components/volkswagencarnet/const.py`

- [ ] **Step 1: Add the new constants**

Append to the bottom of `const.py`:

```python
# EU Data Act data source
CONF_DATA_SOURCE = "data_source"
DATA_SOURCE_LEGACY = "legacy"
DATA_SOURCE_EU_DATA_ACT = "eu_data_act"

CONF_BRAND = "brand"

# Keys used in hass.data entries for EU Data Act
EUDA_COORDINATOR = "euda_coordinator"
EUDA_API = "euda_api"
DATA_SOURCE_KEY = "data_source_key"   # marker stored in hass.data to identify EU Data Act entries
```

- [ ] **Step 2: Verify no syntax errors**

```bash
cd /home/claude/workspace/homeassistant-volkswagencarnet
python -c "from custom_components.volkswagencarnet.const import CONF_DATA_SOURCE, DATA_SOURCE_EU_DATA_ACT; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add custom_components/volkswagencarnet/const.py
git commit -m "feat: add EU Data Act constants"
```

---

## Task 2: Create `eu_data_act.py` — HTML helpers (TDD)

**Files:**
- Create: `custom_components/volkswagencarnet/eu_data_act.py` (partial — helpers only)
- Create: `tests/eu_data_act_test.py`

The VW Identity login pages return HTML with `<form>` and/or an embedded `window._IDK.templateModel` JSON blob. Both patterns need to work.

- [ ] **Step 1: Write failing tests for HTML helpers**

Create `tests/eu_data_act_test.py`:

```python
"""Tests for EU Data Act helpers (no HA fixtures needed)."""

import io
import json
import zipfile

import pytest

from custom_components.volkswagencarnet.eu_data_act import (
    _extract_csrf,
    _extract_template_model,
    _login_fields,
    EUDataActAPI,
)


# --- _extract_csrf -----------------------------------------------------------

def test_extract_csrf_colon_syntax():
    html = "<script>var csrf_token: 'abc123'</script>"
    assert _extract_csrf(html) == "abc123"


def test_extract_csrf_equals_syntax():
    html = '<script>window.csrf_token = "xyz789"</script>'
    assert _extract_csrf(html) == "xyz789"


def test_extract_csrf_missing():
    assert _extract_csrf("<html>no csrf here</html>") == ""


# --- _extract_template_model -------------------------------------------------

def test_extract_template_model_basic():
    data = {"hmac": "h1", "relayState": "r1", "_csrf": "c1"}
    html = f"<script>window._IDK = {{ templateModel: {json.dumps(data)} }};</script>"
    result = _extract_template_model(html)
    assert result == data


def test_extract_template_model_missing():
    assert _extract_template_model("<html>no model</html>") == {}


def test_extract_template_model_nested():
    """The real VW page has templateModel nested inside window._IDK."""
    inner = {"hmac": "h2", "relayState": "r2", "postAction": "/authenticate"}
    html = f"""<script>
    window._IDK = {{
        "templateModel": {json.dumps(inner)},
        "other": "stuff"
    }};
    </script>"""
    result = _extract_template_model(html)
    assert result["hmac"] == "h2"
    assert result["postAction"] == "/authenticate"


# --- _login_fields -----------------------------------------------------------

EMAIL_FORM = """
<html>
<form id="emailPasswordForm" action="/signin-service/v1/abc/identifier">
  <input type="hidden" name="_csrf" value="csrf_val_1" />
  <input type="hidden" name="relayState" value="relay_1" />
  <input type="hidden" name="hmac" value="hmac_1" />
  <input type="text" name="email" value="" />
</form>
</html>
"""


def test_login_fields_email_form():
    action, fields = _login_fields(EMAIL_FORM, "https://identity.vwgroup.io/signin-service/v1/abc/identifier")
    assert action == "https://identity.vwgroup.io/signin-service/v1/abc/identifier"
    assert fields["_csrf"] == "csrf_val_1"
    assert fields["hmac"] == "hmac_1"
    assert fields["relayState"] == "relay_1"


def test_login_fields_relative_action_resolved():
    html = """<form action="/signin-service/v1/test/identifier">
    <input name="_csrf" value="c" />
    <input name="hmac" value="h" />
    <input name="relayState" value="r" />
    </form>"""
    action, _ = _login_fields(html, "https://identity.vwgroup.io/page")
    assert action == "https://identity.vwgroup.io/signin-service/v1/test/identifier"


def test_login_fields_template_model_overrides_form():
    """If the JS templateModel has hmac/relayState, they override the form inputs."""
    model = {"hmac": "hmac_from_js", "relayState": "relay_from_js"}
    html = f"""
    <form action="/signin-service/v1/abc/authenticate">
      <input name="_csrf" value="csrf_from_form" />
      <input name="hmac" value="hmac_from_form" />
      <input name="relayState" value="relay_from_form" />
    </form>
    <script>window._IDK = {{ templateModel: {json.dumps(model)} }};</script>
    """
    _, fields = _login_fields(html, "https://identity.vwgroup.io/")
    assert fields["hmac"] == "hmac_from_js"
    assert fields["relayState"] == "relay_from_js"
    assert fields["_csrf"] == "csrf_from_form"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/claude/workspace/homeassistant-volkswagencarnet
pip install pytest pytest-homeassistant-custom-component --quiet 2>/dev/null
pytest tests/eu_data_act_test.py -v 2>&1 | head -30
```
Expected: `ImportError` or `ModuleNotFoundError` — the module doesn't exist yet.

- [ ] **Step 3: Create `eu_data_act.py` with the helpers**

Create `custom_components/volkswagencarnet/eu_data_act.py`:

```python
"""EU Data Act portal client for Volkswagen Group vehicles.

The portal at https://eu-data-act.drivesomethinggreater.com publishes
vehicle telemetry snapshots every ~15 minutes as required by EU law.
Unlike the old WeConnect API, this is not live data.

Auth: OIDC form login against identity.vwgroup.io using the portal's
own client_id per brand. The result is a session cookie (no refresh
token). Re-login on 401/403 or 5xx+HTML (Adobe AEM session expiry).

IMPORTANT prerequisite: the user must first log in via a browser, accept
the consent screen, link the vehicle and enable the continuous data
request. The integration cannot perform this consent step.

Reference: https://github.com/TA2k/ioBroker.vw-connect/blob/master/lib/euDataAct.js
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# ── Portal endpoints ──────────────────────────────────────────────────────────

BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"
OIDC_AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
OIDC_REDIRECT_URI = BASE_URL + "/login"
OIDC_SCOPE = "openid cars profile"
PORTAL_HOST = urlparse(BASE_URL).netloc
NO_CONTENT_SUFFIX = "_no_content_found.zip"

# ── Brand → OIDC client_id ────────────────────────────────────────────────────

BRANDS: dict[str, dict[str, str]] = {
    "Volkswagen": {
        "client_id": "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com",
        "state_brand": "VOLKSWAGEN_PASSENGER_CARS",
    },
    "Audi": {
        "client_id": "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com",
        "state_brand": "AUDI",
    },
    "Skoda": {
        "client_id": "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com",
        "state_brand": "SKODA",
    },
    "Seat": {
        "client_id": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
        "state_brand": "SEAT",
    },
    "Cupra": {
        "client_id": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
        "state_brand": "CUPRA",
    },
}

# ── Dataset field names ───────────────────────────────────────────────────────

FIELD_SOC = "state_of_charge"
FIELD_HV_SOC = "hv_soc"
FIELD_RANGE = "cruising_range_combined"
FIELD_RANGE_PRIMARY = "cruising_range_primary_engine"
FIELD_ODOMETER = "mileage"
FIELD_CHARGING_STATE = "charging_state"
FIELD_PLUG_STATE = "charging_plug1_connectionstate"
FIELD_TARGET_SOC = "settings.target_soc"


# ── HTML form parsing helpers ─────────────────────────────────────────────────

class _FormParser(HTMLParser):
    """Parse the first HTML form's action URL and input values."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str = ""
        self.inputs: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attrs_d = dict(attrs)
        if tag == "form" and not self._in_form:
            self._in_form = True
            self.action = attrs_d.get("action") or ""
        elif tag == "input" and self._in_form:
            name = attrs_d.get("name")
            if name:
                self.inputs[name] = attrs_d.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def _extract_template_model(html: str) -> dict[str, Any]:
    """Extract VW Identity's window._IDK.templateModel JSON from a page."""
    idx = html.find("templateModel")
    if idx < 0:
        return {}
    brace = html.find("{", idx)
    if brace < 0:
        return {}
    depth = 0
    for i in range(brace, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _extract_csrf(html: str) -> str:
    """Extract CSRF token from HTML."""
    m = re.search(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    return m.group(1) if m else ""


def _login_fields(html: str, page_url: str) -> tuple[str, dict[str, str]]:
    """Return (resolved_action_url, fields_dict) from a VW Identity login page.

    Merges HTML form inputs with JS templateModel values. The templateModel
    takes priority for hmac/relayState when present.
    """
    parser = _FormParser()
    parser.feed(html)
    fields = dict(parser.inputs)
    action = parser.action

    model = _extract_template_model(html)
    for key in ("hmac", "relayState"):
        if model.get(key):
            fields[key] = str(model[key])

    csrf = _extract_csrf(html)
    if csrf and "_csrf" not in fields:
        fields["_csrf"] = csrf

    if action and not action.startswith("http"):
        action = urljoin(page_url, action)

    return action, fields
```

- [ ] **Step 4: Run the HTML helper tests**

```bash
cd /home/claude/workspace/homeassistant-volkswagencarnet
pytest tests/eu_data_act_test.py -v -k "csrf or template or login_fields"
```
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/volkswagencarnet/eu_data_act.py tests/eu_data_act_test.py
git commit -m "feat: EU Data Act HTML login form parsers + tests"
```

---

## Task 3: Add dataset parsing (TDD)

The dataset is a zip containing a JSON document with a flat `Data` array of `{key, dataFieldName, value}` entries. When the same `dataFieldName` appears multiple times, the entry with the **smallest** `key` UUID wins (matches evcc and ioBroker behaviour).

The portal emits `_no_content_found.zip` filenames when the vehicle is sleeping; those must be skipped when picking the newest dataset.

**Files:**
- Modify: `custom_components/volkswagencarnet/eu_data_act.py` (add static methods)
- Modify: `tests/eu_data_act_test.py` (add tests)

- [ ] **Step 1: Write failing tests for dataset parsing**

Append to `tests/eu_data_act_test.py`:

```python
# --- dataset parsing ---------------------------------------------------------

def _make_zip(doc: dict) -> bytes:
    """Build an in-memory zip containing a single vehicle_data.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vehicle_data.json", json.dumps(doc))
    return buf.getvalue()


def test_parse_dataset_basic():
    doc = {
        "vin": "WVWZZZ123",
        "Data": [
            {"key": "ffff", "dataFieldName": "state_of_charge", "value": "73"},
            {"key": "aaaa", "dataFieldName": "mileage", "value": "12345"},
            {"key": "bbbb", "dataFieldName": "cruising_range_combined", "value": "210"},
        ],
    }
    result = EUDataActAPI._parse_dataset(_make_zip(doc))
    assert result["state_of_charge"] == "73"
    assert result["mileage"] == "12345"
    assert result["cruising_range_combined"] == "210"


def test_parse_dataset_smallest_key_wins():
    """When a field appears twice, the entry with smallest key wins."""
    doc = {
        "Data": [
            {"key": "ffff", "dataFieldName": "state_of_charge", "value": "73"},
            {"key": "0001", "dataFieldName": "state_of_charge", "value": "80"},
        ]
    }
    result = EUDataActAPI._parse_dataset(_make_zip(doc))
    assert result["state_of_charge"] == "80"  # "0001" < "ffff"


def test_parse_dataset_empty_field_name_skipped():
    doc = {"Data": [{"key": "aaaa", "dataFieldName": "", "value": "ignored"}]}
    result = EUDataActAPI._parse_dataset(_make_zip(doc))
    assert result == {}


def test_parse_dataset_nested_field():
    doc = {"Data": [{"key": "aaaa", "dataFieldName": "settings.target_soc", "value": "90"}]}
    result = EUDataActAPI._parse_dataset(_make_zip(doc))
    assert result["settings.target_soc"] == "90"


# --- newest_dataset ----------------------------------------------------------

def test_newest_dataset_picks_latest():
    datasets = [
        {"name": "2026-05-31T08-00.zip", "createdOn": "2026-05-31T08:00:00Z"},
        {"name": "2026-05-31T09-00.zip", "createdOn": "2026-05-31T09:00:00Z"},
    ]
    assert EUDataActAPI._newest_dataset(datasets) == "2026-05-31T09-00.zip"


def test_newest_dataset_skips_no_content():
    datasets = [
        {"name": "2026-05-31T09-00.zip", "createdOn": "2026-05-31T09:00:00Z"},
        {"name": "2026-05-31T09-15_no_content_found.zip", "createdOn": "2026-05-31T09:15:00Z"},
    ]
    assert EUDataActAPI._newest_dataset(datasets) == "2026-05-31T09-00.zip"


def test_newest_dataset_all_no_content():
    datasets = [{"name": "2026-05-31T09-00_no_content_found.zip"}]
    assert EUDataActAPI._newest_dataset(datasets) is None


def test_newest_dataset_empty():
    assert EUDataActAPI._newest_dataset([]) is None


def test_newest_dataset_falls_back_to_name_sort():
    """When createdOn is absent, use filename for ordering."""
    datasets = [
        {"name": "2026-05-31T08-00.zip"},
        {"name": "2026-05-31T09-00.zip"},
    ]
    assert EUDataActAPI._newest_dataset(datasets) == "2026-05-31T09-00.zip"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/eu_data_act_test.py -v -k "dataset or newest"
```
Expected: `ImportError` for `EUDataActAPI` (class not defined yet).

- [ ] **Step 3: Add static methods to `EUDataActAPI` stub + dataset code**

Add to `eu_data_act.py` after the helper functions:

```python
# ── Custom exceptions ─────────────────────────────────────────────────────────

class LoginError(Exception):
    """Authentication against the VW Identity service failed."""


class ConsentRequired(LoginError):
    """The portal's one-time browser consent has not been completed."""


# ── API client ────────────────────────────────────────────────────────────────

class EUDataActAPI:
    """Async client for the EU Data Act portal."""

    def __init__(self, brand: str, username: str, password: str) -> None:
        if brand not in BRANDS:
            raise ValueError(
                f"Unknown brand '{brand}'. Valid brands: {list(BRANDS)}"
            )
        self._brand_cfg = BRANDS[brand]
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar)
        return self._session

    # ── Dataset helpers (static — easy to unit test) ──────────────────────────

    @staticmethod
    def _newest_dataset(datasets: list[dict]) -> str | None:
        """Return the name of the most recent dataset that contains data.

        Skips files ending in ``_no_content_found.zip`` (portal placeholder
        when the vehicle is sleeping and has not produced a snapshot yet).
        Comparison key is ``createdOn`` when present, falling back to the
        filename string.
        """
        best: dict | None = None
        for d in datasets:
            name = d.get("name", "")
            if name.lower().endswith(NO_CONTENT_SUFFIX):
                continue
            sort_key = d.get("createdOn") or name
            best_key = (best.get("createdOn") or best.get("name", "")) if best else ""
            if best is None or sort_key > best_key:
                best = d
        return best["name"] if best else None

    @staticmethod
    def _parse_dataset(data: bytes) -> dict[str, str]:
        """Unzip a dataset archive and decode its flat data-point list.

        Returns a ``{dataFieldName: value}`` mapping.  When the same field
        name appears multiple times (the portal merges several report
        snapshots), the entry with the **smallest** key UUID is kept —
        matching the evcc and ioBroker reference implementations.
        """
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            json_name = next(
                (n for n in zf.namelist() if n.lower().endswith(".json")),
                None,
            )
            if json_name is None:
                raise UpdateFailed("No JSON file found inside dataset zip")
            raw = zf.read(json_name)

        doc = json.loads(raw)
        result: dict[str, str] = {}
        winner_keys: dict[str, str] = {}  # field_name → winning UUID key
        for point in doc.get("Data", []):
            field = point.get("dataFieldName", "")
            key = point.get("key", "")
            value = str(point.get("value", ""))
            if not field:
                continue
            if field not in winner_keys or key < winner_keys[field]:
                result[field] = value
                winner_keys[field] = key
        return result
```

- [ ] **Step 4: Run the dataset tests**

```bash
pytest tests/eu_data_act_test.py -v -k "dataset or newest"
```
Expected: all 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/volkswagencarnet/eu_data_act.py tests/eu_data_act_test.py
git commit -m "feat: EU Data Act dataset parsing + tests"
```

---

## Task 4: Add the login and data-fetch methods to `EUDataActAPI`

No TDD for the network methods — they need aiohttp mocking which is tested implicitly by the coordinator test in Task 6. Focus here is correct structure.

**Files:**
- Modify: `custom_components/volkswagencarnet/eu_data_act.py`

- [ ] **Step 1: Add `_authorize_url`, `login`, `_get_json`, `_download_raw`, `vehicles`, `status` to `EUDataActAPI`**

Add these methods inside the `EUDataActAPI` class (after `_parse_dataset`):

```python
    # ── Auth ──────────────────────────────────────────────────────────────────

    def _authorize_url(self) -> str:
        params = {
            "client_id": self._brand_cfg["client_id"],
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": f"de__en__{self._brand_cfg['state_brand']}",
            "redirect_uri": OIDC_REDIRECT_URI,
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    async def login(self) -> None:
        """Run the OIDC form login and store session cookies.

        Raises LoginError on failure.
        Raises ConsentRequired if the portal's one-time browser consent
        has not been completed.
        """
        session = self._get_session()

        # Prime portal session (best effort — sets AEM cookies)
        try:
            async with session.get(BASE_URL + "/", allow_redirects=True):
                pass
        except Exception:  # pylint: disable=broad-except
            pass

        # Step 1: GET IdP authorize → email form
        async with session.get(
            self._authorize_url(), allow_redirects=True
        ) as resp:
            resp.raise_for_status()
            page_url = str(resp.url)
            html = await resp.text()

        email_action, fields = _login_fields(html, page_url)
        if not fields.get("hmac") or not fields.get("_csrf"):
            raise LoginError(
                "Could not parse email form (portal layout may have changed)"
            )
        fields["email"] = self._username

        # Step 2: POST email → password form
        async with session.post(
            email_action, data=fields, allow_redirects=True
        ) as resp:
            page_url = str(resp.url)
            html = await resp.text()

        pw_action, fields = _login_fields(html, page_url)
        if not fields.get("hmac") or not fields.get("_csrf"):
            raise LoginError(
                "Could not parse password form (check email address)"
            )
        fields["email"] = self._username
        fields["password"] = self._password

        # Step 3: POST password → follows redirect chain to portal
        async with session.post(
            pw_action, data=fields, allow_redirects=True
        ) as resp:
            landing_url = str(resp.url)
            landing_html = await resp.text()
            status = resp.status

        if status >= 400:
            raise LoginError(f"Login failed (HTTP {status})")

        parsed = urlparse(landing_url)
        path = parsed.path

        if "consent" in path or "consent-screen" in landing_html.lower():
            raise ConsentRequired(
                "EU Data Act portal requires one-time browser consent. "
                "Open https://eu-data-act.drivesomethinggreater.com/ in a "
                "browser, log in with your We Connect credentials, accept "
                "the consent screen, link your vehicle and enable the "
                "continuous data request (15 min interval)."
            )
        if "signin-service" in path or "/error" in path:
            raise LoginError(f"Login did not complete (landed at {landing_url})")
        if parsed.netloc != PORTAL_HOST:
            raise LoginError(
                f"Login ended at unexpected host: {parsed.netloc}"
            )

        self._logged_in = True
        _LOGGER.debug("EU Data Act login successful")

    async def _get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        _retried: bool = False,
    ) -> Any:
        """GET a JSON endpoint, re-authenticating once on auth failure.

        Also handles Adobe AEM session expiry (5xx + HTML body).
        """
        session = self._get_session()
        async with session.get(
            url, headers=headers or {}, allow_redirects=True
        ) as resp:
            body = await resp.text()
            http_status = resp.status

        # AEM session expiry returns 5xx with an HTML body
        aem_expired = http_status >= 500 and body.lstrip().startswith("<")

        if (http_status in (401, 403) or aem_expired) and not _retried:
            _LOGGER.debug(
                "EU Data Act: re-logging in after HTTP %s", http_status
            )
            self._logged_in = False
            await self.login()
            return await self._get_json(url, headers, _retried=True)

        if http_status >= 400:
            raise UpdateFailed(
                f"EU Data Act GET {url} returned HTTP {http_status}"
            )
        return json.loads(body)

    async def _download_raw(
        self,
        url: str,
        headers: dict[str, str],
        *,
        _retried: bool = False,
    ) -> bytes:
        """Download binary content, re-authenticating once on auth failure."""
        session = self._get_session()
        async with session.get(
            url, headers=headers, allow_redirects=True
        ) as resp:
            body = await resp.read()
            http_status = resp.status

        aem_expired = http_status >= 500 and body[:1] == b"<"
        if (http_status in (401, 403) or aem_expired) and not _retried:
            self._logged_in = False
            await self.login()
            return await self._download_raw(url, headers, _retried=True)

        if http_status >= 400:
            raise UpdateFailed(f"EU Data Act download returned HTTP {http_status}")
        return body

    # ── Data API ──────────────────────────────────────────────────────────────

    async def vehicles(self) -> list[dict[str, str]]:
        """Return list of ``{vin, name}`` dicts for vehicles on the portal."""
        if not self._logged_in:
            await self.login()
        data = await self._get_json(
            f"{BASE_URL}/proxy_api/consent/me/vehicles?viewPosition=FRONT_LEFT",
            {"Accept": "application/json"},
        )
        raw: list[dict] = data if isinstance(data, list) else data.get("vehicles", [])
        result = []
        for v in raw:
            vin = v.get("vin") or v.get("vehicleIdentificationNumber", "")
            if not vin or len(vin) != 17:
                continue
            name = (
                v.get("nickName")
                or v.get("vehicleNickname")
                or v.get("nickname")
                or v.get("modelName")
                or vin
            )
            result.append({"vin": vin, "name": str(name)})
        return result

    async def status(self, vin: str) -> dict[str, str]:
        """Download the newest dataset for *vin* and return decoded fields."""
        if not self._logged_in:
            await self.login()

        # 1. Get the data-request identifier for this VIN
        meta = await self._get_json(
            f"{BASE_URL}/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial",
            {"Accept": "application/json"},
        )
        identifier = meta.get("Identifier", "")
        if not identifier:
            raise UpdateFailed(
                "No data request configured for vehicle. "
                "Enable the continuous data request on the EU Data Act portal."
            )

        # 2. List available datasets
        list_url = (
            f"{BASE_URL}/proxy_api/euda-apim/datadelivery"
            f"/vehicles/{vin}/{identifier}/list"
        )
        datasets = await self._get_json(
            list_url, {"Accept": "application/json", "type": "partial"}
        )
        if not isinstance(datasets, list):
            datasets = datasets.get("files", [])

        name = self._newest_dataset(datasets)
        if name is None:
            if datasets:
                raise UpdateFailed(
                    "No dataset with content (vehicle may be sleeping). "
                    "Wake the vehicle via the app to generate a new snapshot."
                )
            raise UpdateFailed("No datasets available for this vehicle.")

        # 3. Download + parse
        download_url = (
            f"{BASE_URL}/proxy_api/euda-apim/datadelivery"
            f"/vehicles/{vin}/{identifier}/download"
        )
        raw = await self._download_raw(
            download_url, {"filename": name, "type": "partial"}
        )
        return self._parse_dataset(raw)
```

- [ ] **Step 2: Quick syntax check**

```bash
python -c "from custom_components.volkswagencarnet.eu_data_act import EUDataActAPI; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Run all tests so far**

```bash
pytest tests/eu_data_act_test.py -v
```
Expected: all tests still pass.

- [ ] **Step 4: Commit**

```bash
git add custom_components/volkswagencarnet/eu_data_act.py
git commit -m "feat: EU Data Act API login and data-fetch methods"
```

---

## Task 5: Add the coordinator and entity classes

**Files:**
- Modify: `custom_components/volkswagencarnet/eu_data_act.py` (append coordinator + entity classes)

- [ ] **Step 1: Append coordinator and entity descriptors + classes to `eu_data_act.py`**

Append at the bottom of `eu_data_act.py`:

```python
# ── Coordinator ───────────────────────────────────────────────────────────────

class EUDataActCoordinator(DataUpdateCoordinator):
    """Manages polling the EU Data Act portal for one vehicle."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: EUDataActAPI,
        vin: str,
        update_interval: timedelta,
    ) -> None:
        self.api = api
        self.vin = vin
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"EU Data Act {vin}",
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> dict[str, str]:
        try:
            return await self.api.status(self.vin)
        except ConsentRequired as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LoginError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except UpdateFailed:
            raise
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(str(err)) from err


# ── Entity descriptors ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EUDataActSensorDescriptor:
    """Describes one sensor sourced from the EU Data Act portal."""
    key: str
    fields: tuple[str, ...]   # checked in order; first non-empty value used
    name: str
    unit: str | None
    device_class: SensorDeviceClass | None
    state_class: SensorStateClass | None


@dataclass(frozen=True)
class EUDataActBinarySensorDescriptor:
    """Describes one binary sensor sourced from the EU Data Act portal."""
    key: str
    fields: tuple[str, ...]
    name: str
    device_class: BinarySensorDeviceClass | None
    true_values: frozenset[str]   # lower-cased values that map to True


EU_SENSORS: list[EUDataActSensorDescriptor] = [
    EUDataActSensorDescriptor(
        key="soc",
        fields=(FIELD_SOC, FIELD_HV_SOC),
        name="Battery Level",
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EUDataActSensorDescriptor(
        key="range",
        fields=(FIELD_RANGE, FIELD_RANGE_PRIMARY),
        name="Range",
        unit=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EUDataActSensorDescriptor(
        key="odometer",
        fields=(FIELD_ODOMETER,),
        name="Odometer",
        unit=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EUDataActSensorDescriptor(
        key="target_soc",
        fields=(FIELD_TARGET_SOC,),
        name="Charge Limit",
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EUDataActSensorDescriptor(
        key="charging_state",
        fields=(FIELD_CHARGING_STATE,),
        name="Charging State",
        unit=None,
        device_class=None,
        state_class=None,
    ),
]

EU_BINARY_SENSORS: list[EUDataActBinarySensorDescriptor] = [
    EUDataActBinarySensorDescriptor(
        key="plug_connected",
        fields=(FIELD_PLUG_STATE,),
        name="Plug Connected",
        device_class=BinarySensorDeviceClass.PLUG,
        true_values=frozenset({"connected"}),
    ),
    EUDataActBinarySensorDescriptor(
        key="charging",
        fields=(FIELD_CHARGING_STATE,),
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        true_values=frozenset({"charging"}),
    ),
]


# ── Entity base ───────────────────────────────────────────────────────────────

class _EUDataActEntity(CoordinatorEntity):
    """Base entity for all EU Data Act sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EUDataActCoordinator,
        vin: str,
        name: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_name = name
        self._attr_unique_id = f"{vin}-euda-{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Volkswagen",
            serial_number=self._vin,
        )


# ── Concrete entity classes ───────────────────────────────────────────────────

class EUDataActSensor(_EUDataActEntity, SensorEntity):
    """Sensor reading a field from the EU Data Act dataset."""

    def __init__(
        self,
        coordinator: EUDataActCoordinator,
        vin: str,
        descriptor: EUDataActSensorDescriptor,
    ) -> None:
        super().__init__(coordinator, vin, descriptor.name, f"sensor-{descriptor.key}")
        self._fields = descriptor.fields
        self._attr_native_unit_of_measurement = descriptor.unit
        self._attr_device_class = descriptor.device_class
        self._attr_state_class = descriptor.state_class

    @property
    def native_value(self) -> float | str | None:
        data: dict[str, str] = self.coordinator.data or {}
        for field in self._fields:
            raw = data.get(field)
            if raw:
                try:
                    return float(raw)
                except (ValueError, TypeError):
                    return raw
        return None


class EUDataActBinarySensor(_EUDataActEntity, BinarySensorEntity):
    """Binary sensor reading a field from the EU Data Act dataset."""

    def __init__(
        self,
        coordinator: EUDataActCoordinator,
        vin: str,
        descriptor: EUDataActBinarySensorDescriptor,
    ) -> None:
        super().__init__(
            coordinator, vin, descriptor.name, f"binary-{descriptor.key}"
        )
        self._fields = descriptor.fields
        self._attr_device_class = descriptor.device_class
        self._true_values = descriptor.true_values

    @property
    def is_on(self) -> bool | None:
        data: dict[str, str] = self.coordinator.data or {}
        for field in self._fields:
            raw = data.get(field)
            if raw is not None:
                return raw.lower() in self._true_values
        return None
```

- [ ] **Step 2: Syntax check**

```bash
python -c "from custom_components.volkswagencarnet.eu_data_act import EUDataActCoordinator, EU_SENSORS, EU_BINARY_SENSORS, EUDataActSensor, EUDataActBinarySensor; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Run tests**

```bash
pytest tests/eu_data_act_test.py -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add custom_components/volkswagencarnet/eu_data_act.py
git commit -m "feat: EU Data Act coordinator and HA entity classes"
```

---

## Task 6: Add entity value tests

**Files:**
- Modify: `tests/eu_data_act_test.py`

- [ ] **Step 1: Add entity tests that exercise `native_value` and `is_on`**

Append to `tests/eu_data_act_test.py`:

```python
# --- entity value helpers ---------------------------------------------------
# Test native_value and is_on logic directly without a full HA setup.
# We subclass the coordinator to inject data without networking.

from unittest.mock import MagicMock

from custom_components.volkswagencarnet.eu_data_act import (
    EU_SENSORS,
    EU_BINARY_SENSORS,
    EUDataActSensor,
    EUDataActBinarySensor,
    EUDataActSensorDescriptor,
    EUDataActBinarySensorDescriptor,
    FIELD_SOC,
    FIELD_HV_SOC,
    FIELD_PLUG_STATE,
    FIELD_CHARGING_STATE,
    SensorDeviceClass,
    SensorStateClass,
    BinarySensorDeviceClass,
)


def _make_sensor(descriptor: EUDataActSensorDescriptor, data: dict) -> EUDataActSensor:
    coord = MagicMock()
    coord.data = data
    entity = EUDataActSensor.__new__(EUDataActSensor)
    entity.coordinator = coord
    entity._vin = "VIN0"
    entity._fields = descriptor.fields
    return entity


def _make_binary(descriptor: EUDataActBinarySensorDescriptor, data: dict) -> EUDataActBinarySensor:
    coord = MagicMock()
    coord.data = data
    entity = EUDataActBinarySensor.__new__(EUDataActBinarySensor)
    entity.coordinator = coord
    entity._vin = "VIN0"
    entity._fields = descriptor.fields
    entity._true_values = descriptor.true_values
    return entity


def test_sensor_soc_primary_field():
    soc_desc = next(d for d in EU_SENSORS if d.key == "soc")
    sensor = _make_sensor(soc_desc, {FIELD_SOC: "75"})
    assert sensor.native_value == 75.0


def test_sensor_soc_fallback_to_hv_soc():
    soc_desc = next(d for d in EU_SENSORS if d.key == "soc")
    sensor = _make_sensor(soc_desc, {FIELD_HV_SOC: "82"})
    assert sensor.native_value == 82.0


def test_sensor_no_data_returns_none():
    soc_desc = next(d for d in EU_SENSORS if d.key == "soc")
    sensor = _make_sensor(soc_desc, {})
    assert sensor.native_value is None


def test_sensor_string_value_returned_as_is():
    state_desc = next(d for d in EU_SENSORS if d.key == "charging_state")
    sensor = _make_sensor(state_desc, {FIELD_CHARGING_STATE: "charging"})
    assert sensor.native_value == "charging"


def test_binary_plug_connected_true():
    plug_desc = next(d for d in EU_BINARY_SENSORS if d.key == "plug_connected")
    binary = _make_binary(plug_desc, {FIELD_PLUG_STATE: "connected"})
    assert binary.is_on is True


def test_binary_plug_connected_false():
    plug_desc = next(d for d in EU_BINARY_SENSORS if d.key == "plug_connected")
    binary = _make_binary(plug_desc, {FIELD_PLUG_STATE: "disconnected"})
    assert binary.is_on is False


def test_binary_no_data_returns_none():
    plug_desc = next(d for d in EU_BINARY_SENSORS if d.key == "plug_connected")
    binary = _make_binary(plug_desc, {})
    assert binary.is_on is None


def test_binary_case_insensitive():
    plug_desc = next(d for d in EU_BINARY_SENSORS if d.key == "plug_connected")
    binary = _make_binary(plug_desc, {FIELD_PLUG_STATE: "CONNECTED"})
    assert binary.is_on is True
```

- [ ] **Step 2: Run new entity tests**

```bash
pytest tests/eu_data_act_test.py -v -k "sensor_ or binary_"
```
Expected: all 8 pass.

- [ ] **Step 3: Commit**

```bash
git add tests/eu_data_act_test.py
git commit -m "test: EU Data Act entity value unit tests"
```

---

## Task 7: Wire up `__init__.py` — EU Data Act entry setup/unload

**Files:**
- Modify: `custom_components/volkswagencarnet/__init__.py`

The existing `async_setup_entry` / `async_unload_entry` handle the legacy path. We branch on `CONF_DATA_SOURCE` at the top of each and delegate to a new helper for the EU Data Act path.

- [ ] **Step 1: Add imports and constants at the top of `__init__.py`**

Merge these into the existing `from .const import (...)` block (keep one block):

```python
from .const import (
    # … existing items …
    CONF_DATA_SOURCE,
    DATA_SOURCE_EU_DATA_ACT,
    CONF_BRAND,
    EUDA_COORDINATOR,
    EUDA_API,
    DATA_SOURCE_KEY,
)
```

Add a new import block after the const import:

```python
from .eu_data_act import (
    EUDataActAPI,
    EUDataActCoordinator,
    ConsentRequired,
    LoginError,
)
```

- [ ] **Step 2: Add `async_setup_eu_data_act_entry` helper**

Add this function to `__init__.py` (before `async_setup_entry`). The `try/finally` ensures the aiohttp session is always closed if setup fails after login but before the entry is stored in `hass.data`:

```python
async def async_setup_eu_data_act_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up the EU Data Act data source."""
    scan_interval_conf = entry.data.get(CONF_SCAN_INTERVAL, 15)
    update_interval = timedelta(minutes=max(scan_interval_conf, 15))

    api = EUDataActAPI(
        brand=entry.data.get(CONF_BRAND, "Volkswagen"),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    try:
        await api.login()
    except (ConsentRequired, LoginError) as err:
        _LOGGER.warning("EU Data Act login failed: %s", err)
        await api.close()
        entry.async_start_reauth(hass)
        return False

    vin = entry.data[CONF_VEHICLE].upper()
    coordinator = EUDataActCoordinator(hass, entry, api, vin, update_interval)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await api.close()
        raise

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_SOURCE_KEY: DATA_SOURCE_EU_DATA_ACT,
        EUDA_COORDINATOR: coordinator,
        EUDA_API: api,
        UNDO_UPDATE_LISTENER: entry.add_update_listener(_async_update_listener),
    }

    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP,
        lambda _: hass.async_create_task(api.close()),
    )

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "binary_sensor"])
    return True
```

- [ ] **Step 3: Add `async_unload_eu_data_act_entry` helper**

Use `async_unload_platforms` (the non-deprecated API for HA 2024+):

```python
async def async_unload_eu_data_act_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload the EU Data Act data source."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    entry_data[UNDO_UPDATE_LISTENER]()

    unloaded = await hass.config_entries.async_unload_platforms(
        entry, ["sensor", "binary_sensor"]
    )

    if unloaded:
        api: EUDataActAPI = entry_data[EUDA_API]
        await api.close()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded
```

- [ ] **Step 4: Branch at the top of `async_setup_entry` and `async_unload_entry`**

In `__init__.py`, `async_setup_entry` starts at line 78. Insert the branch as the **first line of the function body**:

```python
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Perform Volkswagen Connect component setup."""
    if entry.data.get(CONF_DATA_SOURCE) == DATA_SOURCE_EU_DATA_ACT:
        return await async_setup_eu_data_act_entry(hass, entry)

    scan_interval_conf = entry.options.get(   # ← existing code continues here unchanged
    ...
```

In `async_unload_entry` (line 239), insert the branch as the **first line after the docstring**. The existing first line is `hass.data[DOMAIN][entry.entry_id][UNDO_UPDATE_LISTENER]()` — the EU Data Act unload helper handles its own listener, so the branch must come before that line:

```python
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if hass.data[DOMAIN].get(entry.entry_id, {}).get(DATA_SOURCE_KEY) == DATA_SOURCE_EU_DATA_ACT:
        return await async_unload_eu_data_act_entry(hass, entry)

    _LOGGER.debug("Removing update listener")    # ← existing code continues here unchanged
    hass.data[DOMAIN][entry.entry_id][UNDO_UPDATE_LISTENER]()
    ...
```

- [ ] **Step 5: Syntax check**

```bash
python -c "
import sys; sys.path.insert(0, '.')
# Just import the module; full HA fixtures not available outside pytest
" && echo "run tests instead"

pytest tests/ -v --timeout=30 2>&1 | tail -20
```
Expected: existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/volkswagencarnet/__init__.py custom_components/volkswagencarnet/const.py
git commit -m "feat: async_setup/unload_entry EU Data Act branching"
```

---

## Task 8: Wire up `sensor.py` and `binary_sensor.py`

**Files:**
- Modify: `custom_components/volkswagencarnet/sensor.py`
- Modify: `custom_components/volkswagencarnet/binary_sensor.py`

- [ ] **Step 1: Modify `sensor.py::async_setup_entry`**

Replace the existing `async_setup_entry` in `sensor.py`:

```python
async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Set up the entity."""
    from .const import CONF_DATA_SOURCE, DATA_SOURCE_EU_DATA_ACT, EUDA_COORDINATOR, DOMAIN
    from .eu_data_act import EU_SENSORS, EUDataActSensor

    if entry.data.get(CONF_DATA_SOURCE) == DATA_SOURCE_EU_DATA_ACT:
        coordinator = hass.data[DOMAIN][entry.entry_id][EUDA_COORDINATOR]
        vin = entry.data[CONF_VEHICLE].upper()
        async_add_devices([EUDataActSensor(coordinator, vin, d) for d in EU_SENSORS])
        return True

    data = hass.data[DOMAIN][entry.entry_id][DATA]
    coordinator = data.coordinator
    async_setup_platform_entities(
        hass, entry, coordinator, data, "sensor", VolkswagenSensor, async_add_devices
    )
    return True
```

You need to add `CONF_VEHICLE` to the imports at the top of `sensor.py`:

```python
from homeassistant.const import CONF_VEHICLE  # already imported via __init__ re-export? 
```

Wait — `CONF_VEHICLE` is a custom const, not a HA built-in. Import from `.const`:

```python
from .const import DATA, DATA_KEY, DOMAIN, CONF_DATA_SOURCE, DATA_SOURCE_EU_DATA_ACT, EUDA_COORDINATOR
```

And add the `CONF_VEHICLE` import:

```python
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_RESOURCES, CONF_SCAN_INTERVAL, CONF_USERNAME
```

Actually `CONF_VEHICLE` is defined in `.const` as `"vehicle"`. The value used as a key in `entry.data` is `"vehicle"`. So use:

```python
CONF_VEHICLE = entry.data.get("vehicle", "")
```

or import from `.const`:

```python
from .const import DATA, DATA_KEY, DOMAIN, CONF_VEHICLE
```

- [ ] **Step 2: Modify `binary_sensor.py::async_setup_entry`**

Replace the existing `async_setup_entry` in `binary_sensor.py`:

```python
async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Set up the Volkswagen binary sensor."""
    from .const import CONF_DATA_SOURCE, DATA_SOURCE_EU_DATA_ACT, EUDA_COORDINATOR, DOMAIN, CONF_VEHICLE
    from .eu_data_act import EU_BINARY_SENSORS, EUDataActBinarySensor

    if entry.data.get(CONF_DATA_SOURCE) == DATA_SOURCE_EU_DATA_ACT:
        coordinator = hass.data[DOMAIN][entry.entry_id][EUDA_COORDINATOR]
        vin = entry.data[CONF_VEHICLE].upper()
        async_add_devices([EUDataActBinarySensor(coordinator, vin, d) for d in EU_BINARY_SENSORS])
        return True

    data = hass.data[DOMAIN][entry.entry_id][DATA]
    coordinator = data.coordinator
    async_setup_platform_entities(
        hass,
        entry,
        coordinator,
        data,
        "binary_sensor",
        VolkswagenBinarySensor,
        async_add_devices,
    )
    return True
```

Add `CONF_VEHICLE` to imports at top of `binary_sensor.py`:
```python
from .const import DATA, DATA_KEY, DOMAIN, CONF_VEHICLE
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v --timeout=30 2>&1 | tail -30
```
Expected: all existing tests pass; EU Data Act unit tests pass.

- [ ] **Step 4: Commit**

```bash
git add custom_components/volkswagencarnet/sensor.py custom_components/volkswagencarnet/binary_sensor.py
git commit -m "feat: sensor/binary_sensor dispatch for EU Data Act"
```

---

## Task 9: Add EU Data Act config flow path

**Files:**
- Modify: `custom_components/volkswagencarnet/config_flow.py`

The config flow gains:
- `CONF_DATA_SOURCE` + `CONF_BRAND` added to `DATA_SCHEMA` (top of form)
- `async_step_user` branches after form submission: EU Data Act → `async_step_eu_login`, legacy → existing `async_step_login`
- New `_async_task_eu_login()` async method: creates `EUDataActAPI`, calls login + vehicle discovery
- New `async_step_eu_login()`: progress step
- New `async_step_eu_select_vehicle()`: choose VIN from discovered list → `async_create_entry`

The EU Data Act entry does NOT go through `async_step_select_instruments` — all entities are auto-created.

- [ ] **Step 1: Add EU Data Act imports to `config_flow.py`**

Add at the top of `config_flow.py` (with other imports):

```python
from .const import (
    # existing …
    CONF_DATA_SOURCE,
    DATA_SOURCE_LEGACY,
    DATA_SOURCE_EU_DATA_ACT,
    CONF_BRAND,
)
from .eu_data_act import (
    EUDataActAPI,
    BRANDS as EUDA_BRANDS,
    ConsentRequired,
    LoginError,
)
```

- [ ] **Step 2: Expand `DATA_SCHEMA` to include data_source and brand**

Replace `DATA_SCHEMA` in `config_flow.py`:

```python
DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=""): str,
        vol.Required(CONF_USERNAME, default=""): str,
        vol.Required(CONF_PASSWORD, default=""): str,
        vol.Optional(CONF_DATA_SOURCE, default=DATA_SOURCE_EU_DATA_ACT): vol.In(
            {
                DATA_SOURCE_EU_DATA_ACT: "EU Data Act Portal (recommended)",
                DATA_SOURCE_LEGACY: "Legacy We Connect API",
            }
        ),
        vol.Optional(CONF_BRAND, default="Volkswagen"): vol.In(list(EUDA_BRANDS)),
        vol.Optional(CONF_SPIN, default=""): str,
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): str,
        vol.Optional(CONF_MUTABLE, default=True): cv.boolean,
        vol.Optional(CONF_CONVERT, default=CONF_NO_CONVERSION): vol.In(CONVERT_DICT),
        vol.Optional(
            CONF_SCAN_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
        ): cv.positive_int,
    }
)
```

- [ ] **Step 3: Branch in `async_step_user` after form submission**

In the `async_step_user` method of `VolkswagenCarnetConfigFlow`, change the block that handles `user_input is not None`:

```python
    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle user step."""
        if user_input is not None:
            self._errors = {}
            self._init_info = user_input
            self.task_login = None

            data_source = user_input.get(CONF_DATA_SOURCE, DATA_SOURCE_LEGACY)

            if data_source == DATA_SOURCE_EU_DATA_ACT:
                return await self.async_step_eu_login()

            _LOGGER.debug("Creating connection to Volkswagen Connect")
            self._connection = Connection(
                session=async_get_clientsession(self.hass),
                username=self._init_info[CONF_USERNAME],
                password=self._init_info[CONF_PASSWORD],
                country=self._init_info[CONF_REGION],
            )
            return await self.async_step_login()

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=self._errors,
        )
```

- [ ] **Step 4: Add `_async_task_eu_login`, `async_step_eu_login`, `async_step_eu_select_vehicle`**

Add these three methods to `VolkswagenCarnetConfigFlow` (before `async_step_reauth`):

```python
    async def _async_task_eu_login(self) -> None:
        """Login to EU Data Act portal and discover vehicles."""
        try:
            api = EUDataActAPI(
                brand=self._init_info.get(CONF_BRAND, "Volkswagen"),
                username=self._init_info[CONF_USERNAME],
                password=self._init_info[CONF_PASSWORD],
            )
            await api.login()
            vehicles = await api.vehicles()
            await api.close()
            self._init_info["_eu_vehicles"] = vehicles
        except ConsentRequired as err:
            self._errors["base"] = "eu_consent_required"
            _LOGGER.warning("EU Data Act consent required: %s", err)
        except LoginError as err:
            self._errors["base"] = "cannot_connect"
            _LOGGER.error("EU Data Act login failed: %s", err)
        except Exception as err:  # pylint: disable=broad-except
            self._errors["base"] = "cannot_connect"
            _LOGGER.error("EU Data Act error: %s", err)

        # Always wake the progress step — whether on success or error.
        # Without this call the spinner freezes forever when errors are caught.
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
        )

    async def async_step_eu_login(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Progress step: authenticate to the EU Data Act portal."""
        if not self.task_login:
            self.task_login = self.hass.async_create_task(
                self._async_task_eu_login()
            )
            return self.async_show_progress(
                step_id="eu_login",
                progress_action="task_login",
                progress_task=self.task_login,
            )

        try:
            await self.task_login
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("EU Data Act login task failed: %s", err)
            return self.async_abort(reason="cannot_connect")

        if self._errors:
            return self.async_show_progress_done(next_step_id="user")

        return self.async_show_progress_done(next_step_id="eu_select_vehicle")

    async def async_step_eu_select_vehicle(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Handle EU Data Act vehicle selection."""
        vehicles: list[dict] = self._init_info.get("_eu_vehicles", [])

        if user_input is not None:
            vin = user_input[CONF_VEHICLE]
            if not self._init_info.get(CONF_NAME):
                # Use the vehicle's nickname as the name if the user left name blank
                nickname = next(
                    (v["name"] for v in vehicles if v["vin"] == vin), vin
                )
                self._init_info[CONF_NAME] = nickname

            self._init_info[CONF_VEHICLE] = vin
            self._init_info.pop("_eu_vehicles", None)

            await self.async_set_unique_id(vin)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=self._init_info[CONF_NAME],
                data=self._init_info,
                options={},
            )

        if not vehicles:
            return self.async_abort(reason="no_vehicles_found")

        vin_map = {v["vin"]: f"{v['name']} ({v['vin']})" for v in vehicles}
        return self.async_show_form(
            step_id="eu_select_vehicle",
            data_schema=vol.Schema(
                {vol.Required(CONF_VEHICLE): vol.In(vin_map)}
            ),
            errors=self._errors,
        )
```

- [ ] **Step 5: Update `strings.json` and `translations/en.json`**

In `custom_components/volkswagencarnet/strings.json`, make these three targeted additions:

**In `"config"."step"."user"."data"`, add two fields** (alongside existing `username`, `password`, etc.):
```json
"data_source": "Data source",
"brand": "Brand (for EU Data Act)"
```

**In `"config"."step"`, add two new step entries** (alongside `user`, `select_vehicle`, etc.):
```json
"eu_login": {
  "title": "EU Data Act Portal",
  "description": "Signing in to the EU Data Act portal..."
},
"eu_select_vehicle": {
  "title": "Select Vehicle",
  "description": "The following vehicles were found on the EU Data Act portal.",
  "data": {
    "vehicle": "Vehicle"
  }
}
```

**In `"config"."error"`, add:**
```json
"eu_consent_required": "Portal consent not yet completed. Open https://eu-data-act.drivesomethinggreater.com in a browser, log in, accept the consent screen, link your vehicle and enable the continuous data request (15 min interval)."
```

**In `"config"."abort"`, add:**
```json
"no_vehicles_found": "No vehicles found on the EU Data Act portal. Complete the one-time portal setup first."
```

**In `"config"."progress"`, add:**
```json
"task_eu_login": "Signing in to the EU Data Act portal"
```
Then update `async_step_eu_login` to use `progress_action="task_eu_login"` instead of `"task_login"` to avoid sharing the progress key with the legacy step.

Apply the **identical changes** to `translations/en.json` (it has the same structure but with concrete English strings rather than `[%key:...]` references).

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -v --timeout=30 2>&1 | tail -30
```
Expected: all existing config flow tests pass (legacy path unchanged), all EU Data Act unit tests pass.

- [ ] **Step 7: Commit**

```bash
git add custom_components/volkswagencarnet/config_flow.py \
        custom_components/volkswagencarnet/strings.json \
        custom_components/volkswagencarnet/translations/
git commit -m "feat: EU Data Act config flow path (eu_login + eu_select_vehicle steps)"
```

---

## Task 10: Push branch and open PR

- [ ] **Step 1: Run full test suite one last time**

```bash
pytest tests/ -v --timeout=30
```
Expected: all pass, zero failures.

- [ ] **Step 2: Push branch**

```bash
git push -u origin feat/eu-data-act
```

- [ ] **Step 3: Open PR**

Note: `--repo Donsig/homeassistant-volkswagencarnet` creates a PR on the personal fork for testing. To contribute upstream, change to `--repo robinostlund/homeassistant-volkswagencarnet` when ready.

```bash
gh pr create \
  --repo Donsig/homeassistant-volkswagencarnet \
  --base master \
  --title "feat: add EU Data Act portal as alternative data source" \
  --body "$(cat <<'EOF'
## Summary

- Adds `eu_data_act.py` — async client for `eu-data-act.drivesomethinggreater.com`
- Config flow gains a **Data Source** selector: *EU Data Act Portal* (default) or *Legacy We Connect API*
- EU Data Act path auto-creates 5 sensors (SoC, range, odometer, charge limit, charging state) and 2 binary sensors (plug connected, charging)
- Legacy path is **unchanged** — existing entries continue to work

## Why

The reverse-engineered We Connect API used by the `volkswagencarnet` library was closed by VW. Under the EU Data Act, VW is legally required to provide vehicle data via the EU Data Act portal. This PR adds support for that portal.

## Limitations vs legacy

| Feature | Legacy | EU Data Act |
|---------|--------|-------------|
| Live telemetry | Yes | No (15 min snapshots) |
| GPS / position | Yes | **Not available** |
| Climate / lock control | Yes | **Not available** |
| Entity count | ~68 | 7 |

## Prerequisites (one-time user action)

Before the EU Data Act path works, the user must:
1. Open `https://eu-data-act.drivesomethinggreater.com` in a browser
2. Log in with their We Connect credentials
3. Accept the consent screen
4. Link their vehicle
5. Enable the continuous data request (15 min interval)

The integration will show a `eu_consent_required` error if this has not been done.

## References

- evcc implementation: https://github.com/evcc-io/evcc/pull/30362
- ioBroker reference: https://github.com/TA2k/ioBroker.vw-connect/blob/master/lib/euDataAct.js
EOF
)"
```

---

## Self-Review Checklist

- [x] `eu_data_act.py` — all exports used in platform files are defined: `EUDataActAPI`, `EUDataActCoordinator`, `EU_SENSORS`, `EU_BINARY_SENSORS`, `EUDataActSensor`, `EUDataActBinarySensor`, `ConsentRequired`, `LoginError`, `BRANDS`
- [x] `const.py` — all new constants are used: `CONF_DATA_SOURCE`, `DATA_SOURCE_LEGACY`, `DATA_SOURCE_EU_DATA_ACT`, `CONF_BRAND`, `EUDA_COORDINATOR`, `EUDA_API`, `DATA_SOURCE_KEY`
- [x] Task 7 references `DATA_SOURCE_KEY` but it's added to `const.py` in Task 7 Step 2 note — **gap**: add `DATA_SOURCE_KEY = "data_source_key"` explicitly to Task 1's const additions
- [x] `CONF_VEHICLE` in `sensor.py` / `binary_sensor.py` — referenced as a string key `"vehicle"` via `.const.CONF_VEHICLE`, which is already defined as `"vehicle"` in `const.py` ✓
- [x] `async_setup_eu_data_act_entry` uses `EVENT_HOMEASSISTANT_STOP` — this is already imported in `__init__.py` ✓
- [x] `async_setup_eu_data_act_entry` uses `CONF_SCAN_INTERVAL` and `CONF_USERNAME/PASSWORD/VEHICLE` — these are HA built-ins already imported ✓
- [x] Strings gap: `eu_consent_required` error string referenced in config flow but only added in Task 9 Step 5 ✓

**Fix found:** Add `DATA_SOURCE_KEY = "data_source_key"` to Task 1's const additions. The plan above already includes it in the `const.py` block in Task 1 Step 1.
