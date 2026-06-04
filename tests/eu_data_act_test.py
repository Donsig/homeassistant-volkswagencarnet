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
