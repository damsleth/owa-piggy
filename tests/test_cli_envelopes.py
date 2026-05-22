"""Action envelope coverage on owa-piggy command surface.

Tests the hugr CONVENTIONS.md contract on the state-changing
commands: reseed, profiles set-default, profiles delete, plus the
interactive setup rejection.
"""
from __future__ import annotations

import json
import subprocess
import sys


def _run(*args):
  return subprocess.run(
    [sys.executable, "-m", "owa_piggy", *args],
    capture_output=True, text=True,
  )


def _last_json(out: str) -> dict:
  for line in reversed(out.strip().splitlines()):
    try:
      return json.loads(line)
    except json.JSONDecodeError:
      continue
  raise AssertionError(f"No JSON in: {out!r}")


def test_setup_rejects_json():
  result = _run("setup", "--profile", "anything", "--json")
  assert result.returncode == 1
  assert "interactive" in result.stderr.lower()


def test_profiles_delete_refuses_without_yes_in_non_tty():
  """Destructive gating: machine invocation without --yes must refuse."""
  result = _run("profiles", "delete", "any-alias", "--json")
  payload = _last_json(result.stdout)
  assert payload["ok"] is False
  # Either confirmation_required (gate fired) or an earlier validation
  # error (invalid alias / not found). Both are acceptable - what we
  # care about is that no profile was deleted without --yes.
  assert result.returncode != 0


def test_profiles_delete_with_yes_but_missing_profile_emits_envelope():
  """With --yes set, gate is bypassed but profile_not_found still triggers."""
  result = _run("profiles", "delete", "definitely-not-a-real-alias", "--yes", "--json")
  payload = _last_json(result.stdout)
  assert payload["ok"] is False
  # Either profile_not_found or invalid_alias depending on the alias
  # string. Both are valid failure codes.
  assert payload["error"]["code"] in ("profile_not_found", "invalid_alias")


def test_profiles_set_default_emits_envelope_on_missing_profile():
  """set-default surfaces an action envelope when the profile doesn't exist."""
  result = _run("profiles", "set-default", "nonexistent-profile-xyz", "--json")
  payload = _last_json(result.stdout)
  assert payload["tool"] == "owa-piggy"
  assert payload["command"] == "profiles set-default"
  assert payload["ok"] is False
  assert payload["error"]["code"] == "set_default_failed"


def test_reseed_json_emits_envelope_on_usage_error():
  """--all and --profile are mutually exclusive."""
  result = _run("reseed", "--all", "--profile", "x", "--json")
  payload = _last_json(result.stdout)
  assert payload["command"] == "reseed"
  assert payload["ok"] is False
  assert payload["error"]["code"] == "usage"


def _profiles_list_keys(payload: dict) -> None:
  """Shape check for `profiles --json` / `profiles list --json`."""
  assert "default" in payload
  assert isinstance(payload.get("profiles"), list)
  for entry in payload["profiles"]:
    assert "alias" in entry
    assert "default" in entry
    assert "registered" in entry
    assert "has_config" in entry


def test_profiles_list_json_returns_registry_doc():
  """`profiles list --json` returns the same shape as bare `profiles --json`."""
  result = _run("profiles", "list", "--json")
  assert result.returncode == 0
  payload = json.loads(result.stdout)
  _profiles_list_keys(payload)


def test_profiles_json_list_returns_registry_doc():
  """Parent `--json` placement also yields JSON (regression: argparse
  subparser default once clobbered the parent flag, producing plain text)."""
  result = _run("profiles", "--json", "list")
  assert result.returncode == 0
  payload = json.loads(result.stdout)
  _profiles_list_keys(payload)


def test_profiles_list_plain_does_not_open_picker():
  """`profiles list` (no --json) emits plain text even on a TTY context;
  it must never block on an interactive picker - that's the whole point
  of having `list` as a non-interactive alias for scripts."""
  result = _run("profiles", "list")
  # Either we have profiles (plain list, one per line, possibly with markers)
  # or the empty-state hint. Both are non-interactive.
  assert result.returncode == 0
  assert "owa-piggy setup" in result.stdout or result.stdout.strip()
