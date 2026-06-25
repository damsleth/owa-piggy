"""owa-piggy machine surface: schema, --help --json, --agent, --err-json.

Mirrors the owa-tools consumer contract so an agent driving the
broker sees the same introspection surface. These
exercises need no auth: schema/help are static, --agent uses `version`,
and --err-json is triggered with an argparse usage error.
"""

from __future__ import annotations

import json
import subprocess
import sys


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "owa_piggy", *args],
        capture_output=True,
        text=True,
    )


def test_schema_shape():
    result = _run("schema")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["tool"] == "owa-piggy"
    assert payload["suite"] == "owa-piggy"
    assert payload["schema_version"] == 1
    assert payload["commands"]
    names = {c["name"] for c in payload["commands"]}
    assert {"token", "status", "setup", "profiles"} <= names


def test_schema_filters_one_command():
    result = _run("schema", "token")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert [c["name"] for c in payload["commands"]] == ["token"]


def test_schema_rejects_unknown_command():
    result = _run("schema", "bogus")
    assert result.returncode == 2
    assert "unknown schema command" in result.stderr


def test_help_json_emits_schema():
    result = _run("--help", "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["tool"] == "owa-piggy"


def test_destructive_profiles_delete_declares_metadata():
    payload = json.loads(_run("schema").stdout)
    by_name = {c["name"]: c for c in payload["commands"]}
    assert by_name["setup"]["mutates"] is True
    assert by_name["profiles"]["mutates"] is True


def test_agent_wraps_json_output():
    result = _run("--agent", "version", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["_owa"]["tool"] == "owa-piggy"
    assert payload["_owa"]["suite"] == "owa-piggy"
    assert payload["_owa"]["command"] == "version"
    assert "data" in payload


def test_agent_adds_json_default_for_machine_command():
    result = _run("--agent", "version")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["_owa"]["command"] == "version"
    assert payload["data"]["tool"] == "owa-piggy"


def test_err_json_emits_structured_error_on_stderr():
    result = _run("--err-json", "token", "--audience", "NOPE")
    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["tool"] == "owa-piggy"
    assert payload["error"]["command"] == "token"
    assert payload["error"]["exit_code"] == 2
