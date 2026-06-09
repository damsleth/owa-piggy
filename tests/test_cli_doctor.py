"""`owa-piggy --doctor` contract conformance."""
from __future__ import annotations

import json
import subprocess
import sys


def _run(*args):
  return subprocess.run(
    [sys.executable, "-m", "owa_piggy", *args],
    capture_output=True, text=True,
  )


def test_doctor_json_shape():
  result = _run("--doctor", "--json")
  payload = json.loads(result.stdout.strip())
  assert payload["tool"] == "owa-piggy"
  assert "version" in payload
  assert isinstance(payload["findings"], list)
  # Reserved-key contract: data class has no top-level `ok`.
  assert "ok" not in payload


def test_doctor_human_default():
  result = _run("--doctor")
  assert "owa-piggy doctor" in result.stdout


def test_doctor_redaction_sentinel():
  result = _run("--doctor", "--json")
  ids = [f["id"] for f in json.loads(result.stdout.strip())["findings"]]
  assert "redact_sentinel_leak" not in ids
  assert "redact_unavailable" not in ids


def test_doctor_includes_auth_summary():
  """Auth block must never carry token values - only counts and aliases."""
  result = _run("--doctor", "--json")
  payload = json.loads(result.stdout.strip())
  # Auth may be absent (config_unreadable path), but if present must
  # not contain anything token-shaped.
  auth = payload.get("auth")
  if auth is not None:
    serialised = json.dumps(auth)
    assert "access_token" not in serialised
    assert "refresh_token" not in serialised


def test_doctor_exit_code_well_defined():
  result = _run("--doctor", "--json")
  assert result.returncode in (0, 1)
