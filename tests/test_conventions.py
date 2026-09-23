"""Tests for the CLI contract helpers in owa_piggy/conventions.py."""

from __future__ import annotations

import io
import json

from owa_piggy.conventions import (
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_USER_ERROR,
    DoctorFinding,
    DoctorPayload,
    action_envelope,
    emit_action,
)


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
