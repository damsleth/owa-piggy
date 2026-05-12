"""Tests for the mnem CLI contract helpers in owa_piggy/conventions.py."""
from __future__ import annotations

import io
import json

from owa_piggy.conventions import (
  DoctorFinding,
  DoctorPayload,
  EXIT_OK,
  EXIT_PARTIAL,
  EXIT_USER_ERROR,
  action_envelope,
  data_error,
  emit_action,
  emit_data_error,
  redact,
)


def test_redact_jwt_like():
  jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
  assert jwt not in redact(f"token={jwt}")


def test_redact_bearer():
  out = redact("Authorization: Bearer abc123def456")
  assert "abc123def456" not in out


def test_redact_token_fields():
  out = redact('{"access_token":"xyz","refresh_token":"abc"}')
  assert "xyz" not in out and "abc" not in out


def test_redaction_sentinel_does_not_leak():
  jwt = "eyJfake." + "CANARY_SECRET_xxxx" + "." + "padding1234"
  out = redact(f"Authorization: Bearer {jwt}")
  assert "CANARY_SECRET_xxxx" not in out


def test_action_envelope_shape():
  env = action_envelope(command="reseed", ok=True, stats={"profiles_reseeded": 2})
  assert env["tool"] == "owa-piggy"
  assert env["command"] == "reseed"
  assert env["ok"] is True
  assert env["stats"]["profiles_reseeded"] == 2


def test_emit_action_one_line():
  buf = io.StringIO()
  emit_action(action_envelope(command="x", ok=True), stream=buf)
  payload = json.loads(buf.getvalue())
  assert payload["command"] == "x"


def test_data_error_shape():
  err = data_error(command="token", code="auth_expired", message="m", hint="run setup")
  assert err["ok"] is False
  assert err["error"]["hint"] == "run setup"


def test_emit_data_error_one_line():
  buf = io.StringIO()
  emit_data_error(data_error(command="x", code="c", message="m"), stream=buf)
  assert json.loads(buf.getvalue())["ok"] is False


def test_doctor_payload_to_dict():
  d = DoctorPayload(
    config_path="/etc/owa-piggy",
    auth={"profile_count": 0},
    findings=[DoctorFinding(id="x", severity="warning", message="m")],
  ).to_dict()
  assert d["tool"] == "owa-piggy"
  assert d["auth"]["profile_count"] == 0
  assert d["findings"][0]["severity"] == "warning"


def test_doctor_exit_codes():
  assert DoctorPayload().exit_code() == EXIT_OK
  d = DoctorPayload(findings=[DoctorFinding(id="x", severity="error", message="m")])
  assert d.exit_code() == EXIT_USER_ERROR


def test_exit_constants():
  assert EXIT_OK == 0
  assert EXIT_PARTIAL == 5
