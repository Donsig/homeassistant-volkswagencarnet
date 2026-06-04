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


# ── Custom exceptions ─────────────────────────────────────────────────────────

class LoginError(Exception):
    """Authentication against the VW Identity service failed."""


class ConsentRequired(LoginError):
    """The portal's one-time browser consent has not been completed."""


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
        self._brand_cfg = BRANDS.get(brand, BRANDS["Volkswagen"])
        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
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
