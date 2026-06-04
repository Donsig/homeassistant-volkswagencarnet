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


# ── API client ────────────────────────────────────────────────────────────────

class EUDataActAPI:
    """Async client for the EU Data Act portal."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        brand: str = "Volkswagen",
    ) -> None:
        self._hass = hass
        self._username = username
        self._password = password
        self._brand = brand
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def login(self) -> None:
        """Perform OIDC form login against identity.vwgroup.io."""
        session = await self._get_session()
        brand_cfg = BRANDS.get(self._brand, BRANDS["Volkswagen"])
        client_id = brand_cfg["client_id"]

        params = {
            "client_id": client_id,
            "redirect_uri": OIDC_REDIRECT_URI,
            "response_type": "code",
            "scope": OIDC_SCOPE,
        }
        auth_url = OIDC_AUTHORIZE_URL + "?" + urlencode(params)

        async with session.get(auth_url, allow_redirects=True) as resp:
            html = await resp.text()
            page_url = str(resp.url)

        action, fields = _login_fields(html, page_url)
        fields["email"] = self._username

        async with session.post(
            action,
            data={**fields},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=True,
        ) as resp:
            html = await resp.text()
            page_url = str(resp.url)

        if "password" in html.lower():
            action, fields = _login_fields(html, page_url)
            fields["password"] = self._password

            async with session.post(
                action,
                data={**fields},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True,
            ) as resp:
                if resp.status >= 400:
                    raise ConfigEntryAuthFailed(
                        f"EU Data Act login failed: HTTP {resp.status}"
                    )

        self._authenticated = True
        _LOGGER.debug("EU Data Act login succeeded")

    async def get_vehicle_data(self, vin: str) -> dict[str, Any]:
        """Fetch and unpack the latest data snapshot ZIP for a vehicle."""
        if not self._authenticated:
            await self.login()

        session = await self._get_session()
        url = f"{BASE_URL}/api/v1/vehicles/{vin}/data"

        async with session.get(url) as resp:
            if resp.status in (401, 403):
                self._authenticated = False
                raise ConfigEntryAuthFailed("EU Data Act session expired")
            if resp.status != 200:
                raise UpdateFailed(f"EU Data Act returned HTTP {resp.status}")

            content_type = resp.headers.get("Content-Type", "")
            if "zip" in content_type or "octet-stream" in content_type:
                raw = await resp.read()
                return _unpack_zip(raw)

            return await resp.json()


def _unpack_zip(data: bytes) -> dict[str, Any]:
    """Extract JSON records from a ZIP snapshot."""
    result: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    with zf.open(name) as f:
                        try:
                            result[name] = json.load(f)
                        except json.JSONDecodeError:
                            _LOGGER.warning("Could not parse %s in ZIP", name)
    except zipfile.BadZipFile:
        _LOGGER.error("EU Data Act returned invalid ZIP")
    return result
