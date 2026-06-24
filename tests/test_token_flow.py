"""Tests for the shared live-exchange helper `exchange_fresh`.

These never touch the network: `exchange_token` is replaced with an
obvious fake that returns a canned dict (or None) and, where the AAD
error path is exercised, prints a synthetic AADSTS line to the captured
stderr sink. Refresh tokens here are obvious fakes so a grep never flags
this file as shipping a real token.
"""

from owa_piggy import token_flow
from owa_piggy.config import load_config

# FOCI-shaped fake refresh token (the `1.` prefix is what the shape check
# looks for) and an obviously fake tenant id.
RT = "1.fake-refresh-token"
TID = "00000000-0000-0000-0000-000000000000"
SCOPE = "https://graph.microsoft.com/.default"


def test_missing_rt_returns_none(clean_env):
    config = {"OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert result is None
    assert info["rt_present"] is False
    assert info["tid_present"] is True
    assert info["rt_shape_ok"] is False


def test_missing_tid_returns_none(clean_env):
    config = {"OWA_REFRESH_TOKEN": RT}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert result is None
    assert info["rt_present"] is True
    assert info["tid_present"] is False
    assert info["rt_shape_ok"] is True


def test_foci_shape_rejected_for_default_client(clean_env):
    """An opaque (non-`1.`/`0.`) RT for the DEFAULT client is rejected."""
    config = {"OWA_REFRESH_TOKEN": "opaque-rt-no-prefix", "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert result is None
    assert info["rt_present"] is True
    assert info["tid_present"] is True
    assert info["rt_shape_ok"] is False


def test_opaque_rt_allowed_for_non_default_client(clean_env, monkeypatch):
    """An opaque RT IS allowed when OWA_CLIENT_ID overrides the default
    client - the FOCI shape check only applies to the default client."""
    other_cid = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
    config = {
        "OWA_REFRESH_TOKEN": "opaque-rt-no-prefix",
        "OWA_TENANT_ID": TID,
        "OWA_CLIENT_ID": other_cid,
    }
    monkeypatch.setattr(token_flow, "exchange_token", lambda *a, **k: {"access_token": "at"})
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert info["rt_shape_ok"] is True
    assert info["cid"] == other_cid
    assert result == {"access_token": "at"}


def test_rotation_persisted_when_persist_true(tmp_config, clean_env, monkeypatch):
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **k: {"access_token": "at", "refresh_token": "1.rotated-rt"},
    )
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=True, config_path=tmp_config)
    assert result["access_token"] == "at"
    assert info["rotated"] is True
    # In-memory config is updated...
    assert config["OWA_REFRESH_TOKEN"] == "1.rotated-rt"
    # ...and the rotated token was written to disk.
    on_disk, _persist = load_config(tmp_config)
    assert on_disk["OWA_REFRESH_TOKEN"] == "1.rotated-rt"


def test_rotation_not_persisted_when_persist_false(tmp_config, clean_env, monkeypatch):
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **k: {"access_token": "at", "refresh_token": "1.rotated-rt"},
    )
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False, config_path=tmp_config)
    assert result["access_token"] == "at"
    assert info["rotated"] is True
    # In-memory config still updated, but nothing written to disk.
    assert config["OWA_REFRESH_TOKEN"] == "1.rotated-rt"
    assert not tmp_config.exists()


def test_no_rotation_when_same_rt(tmp_config, clean_env, monkeypatch):
    """exchange_token returns the SAME refresh token (no rotation): the
    rotation branch is skipped (covers 147->149) and nothing is persisted."""
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **k: {"access_token": "at", "refresh_token": RT},
    )
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=True, config_path=tmp_config)
    assert result["access_token"] == "at"
    assert info["rotated"] is False
    assert not tmp_config.exists()


def test_no_rotation_when_rt_absent(tmp_config, clean_env, monkeypatch):
    """Response carries no refresh_token at all: rotation branch skipped."""
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    monkeypatch.setattr(token_flow, "exchange_token", lambda *a, **k: {"access_token": "at"})
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=True, config_path=tmp_config)
    assert info["rotated"] is False
    assert not tmp_config.exists()


def test_aad_error_70043_detected(clean_env, monkeypatch):
    """A failed exchange that prints AADSTS70043 to the captured stderr
    sink surfaces info['aad_error'] == 'AADSTS70043'."""

    def printing_exchange(*a, **k):
        from owa_piggy import oauth

        print("ERROR: invalid_grant: AADSTS70043 expired", file=oauth._err_stream())
        return None

    monkeypatch.setattr(token_flow, "exchange_token", printing_exchange)
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False, capture_stderr=True)
    assert result is None
    assert info["aad_error"] == "AADSTS70043"
    assert "AADSTS70043" in info["stderr_text"]


def test_aad_error_700084_detected(clean_env, monkeypatch):
    def printing_exchange(*a, **k):
        from owa_piggy import oauth

        print("ERROR: invalid_grant: AADSTS700084", file=oauth._err_stream())
        return None

    monkeypatch.setattr(token_flow, "exchange_token", printing_exchange)
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False, capture_stderr=True)
    assert result is None
    assert info["aad_error"] == "AADSTS700084"


def test_aad_error_none_when_unrecognized(clean_env, monkeypatch):
    """A failed exchange whose stderr has no recoverable AAD code leaves
    aad_error None (covers the detection loop completing without a match)."""

    def printing_exchange(*a, **k):
        from owa_piggy import oauth

        print("ERROR: something else entirely", file=oauth._err_stream())
        return None

    monkeypatch.setattr(token_flow, "exchange_token", printing_exchange)
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False, capture_stderr=True)
    assert result is None
    assert info["aad_error"] is None
