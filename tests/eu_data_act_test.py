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
