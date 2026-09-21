"""PIM sign-in and routing never touch a real broker or Microsoft."""

import base64
import io
import json

import pytest

from owa_piggy import clients, oauth, pim_setup, scopes


@pytest.fixture
def profile(tmp_path, monkeypatch):
    from owa_piggy import config

    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    return "work"


def test_pim_is_scope_specific_and_never_falls_back(profile):
    assert scopes.resolve_audience("pim") == (scopes.PIM_SCOPE, "")
    assert clients.select_for_scope(profile, scopes.PIM_SCOPE) == (oauth.PIM_CLIENT_ID, {})
    assert clients.select_for_scope(profile, scopes.resolve_audience("graph")[0]) == (None, None)
    clients.save_client(profile, oauth.PIM_CLIENT_ID, refresh_token="fake-pim-rt")
    assert clients.select_for_scope(profile, scopes.PIM_SCOPE)[1]["refresh_token"] == "fake-pim-rt"
    assert clients.select_for_scope(profile, scopes.resolve_audience("graph")[0]) == (None, None)
    assert clients.capture_targets(profile) == []


def test_pim_default_and_override(monkeypatch):
    monkeypatch.setenv("OWA_DEFAULT_AUDIENCE", "pim")
    assert scopes.resolve_audience()[0] == scopes.PIM_SCOPE
    assert scopes.resolve_audience("graph")[0] != scopes.PIM_SCOPE
    assert scopes.resolve_audience("pim", "User.Read")[1]
    monkeypatch.delenv("OWA_DEFAULT_AUDIENCE")
    assert scopes.resolve_audience(profile_default="pim")[0] == scopes.PIM_SCOPE


def test_native_overlay_does_not_retain_spa_origin():
    original = {"OWA_ORIGIN": "https://outlook.cloud.microsoft", "OWA_REFRESH_TOKEN": "fake-owa"}
    overlay = clients.overlay_config(
        original, oauth.PIM_CLIENT_ID, {"refresh_token": "fake-native"}
    )
    assert "OWA_ORIGIN" not in overlay
    assert original["OWA_REFRESH_TOKEN"] == "fake-owa"


@pytest.mark.parametrize("cid,has_origin", [(oauth.PIM_CLIENT_ID, False), (oauth.CLIENT_ID, True)])
def test_refresh_header_is_client_specific(monkeypatch, cid, has_origin):
    def fake_open(req, **kwargs):
        assert req.has_header("Origin") is has_origin
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        return io.BytesIO(b'{"access_token":"fake-access"}')

    monkeypatch.setattr(oauth._OPENER, "open", fake_open)
    assert oauth.exchange_token("fake-rt", "fake-tenant", cid, scopes.PIM_SCOPE)


def _result(**claims):
    body = {
        "tid": "fake-tenant",
        "aud": oauth.PIM_CLIENT_ID,
        "preferred_username": "user@example.com",
        **claims,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return {
        "access_token": "fake-access",
        "refresh_token": "fake-native-rt",
        "id_token": f"fake.{encoded}.fake",
        "scope": scopes.PIM_PERMISSION,
    }


CONFIG = {"OWA_TENANT_ID": "fake-tenant", "OWA_EMAIL": "user@example.com"}
DEVICE = {"device_code": "fake-device", "user_code": "FAKE-CODE", "interval": 1, "expires_in": 20}


@pytest.mark.parametrize(
    "changes",
    [
        {"tid": "another-tenant"},
        {"aud": "wrong-client"},
        {"preferred_username": "another@example.com"},
    ],
)
def test_identity_mismatch_is_rejected(changes):
    with pytest.raises(ValueError, match="identity"):
        pim_setup._verify_identity(_result(**changes), CONFIG)


@pytest.mark.parametrize("field", ["id_token", "scope", "refresh_token", "access_token"])
def test_incomplete_signin_is_rejected(field):
    result = _result()
    del result[field]
    with pytest.raises(ValueError):
        pim_setup._verify_identity(result, CONFIG)


@pytest.mark.parametrize(
    "field,value", [("id_token", []), ("refresh_token", {}), ("access_token", 42)]
)
def test_malformed_credentials_are_rejected(field, value):
    result = _result()
    result[field] = value
    with pytest.raises(ValueError):
        pim_setup._verify_identity(result, CONFIG)


def test_native_diagnostic_rotation_is_isolated(profile):
    clients.save_client(profile, oauth.PIM_CLIENT_ID, refresh_token="fake-native")
    original = {**CONFIG, "OWA_REFRESH_TOKEN": "fake-owa", "OWA_RT_ISSUED_AT": "old-spa"}
    config, sink = clients.pim_exchange_config(profile, original, scopes.PIM_SCOPE)
    assert config["OWA_REFRESH_TOKEN"] == "fake-native"
    assert config["OWA_RT_ISSUED_AT"] != "old-spa"
    sink("fake-rotated")
    assert clients.load_clients(profile)[oauth.PIM_CLIENT_ID]["refresh_token"] == "fake-rotated"
    assert original["OWA_REFRESH_TOKEN"] == "fake-owa"


def test_missing_pim_status_does_not_report_owa_expiry(profile, monkeypatch):
    from owa_piggy import status

    monkeypatch.setattr(
        status,
        "load_config",
        lambda path: (
            {
                **CONFIG,
                "OWA_REFRESH_TOKEN": "fake-owa",
                "OWA_RT_ISSUED_AT": "2026-01-01T00:00:00Z",
            },
            True,
        ),
    )
    monkeypatch.setattr(status, "_profile_is_disabled", lambda alias: False)
    monkeypatch.setattr(status, "launchd_is_scheduled", lambda alias: False)
    monkeypatch.setattr(
        status, "exchange_fresh", lambda *a, **k: pytest.fail("unexpected exchange")
    )
    report = status._status_json(status._probe_profile(profile, audience="pim"))
    assert report["refresh_token"] == {
        "present": False,
        "expires_at": None,
        "minutes_remaining": None,
    }
    assert "clients add pim" in report["hints"][0]


def test_signin_waits_and_saves_only_verified_native_token(profile, monkeypatch, capsys):
    responses = iter(
        [DEVICE, {"error": "authorization_pending"}, {"error": "slow_down"}, _result()]
    )
    waits = []
    monkeypatch.setattr(pim_setup, "_post", lambda *a: next(responses))
    monkeypatch.setattr(pim_setup.time, "sleep", waits.append)
    clients.save_client(profile, clients.TEAMS_WEB_CLIENT_ID, refresh_token="fake-teams")
    assert pim_setup.sign_in(profile, CONFIG) == 0
    assert waits == [1, 1, 6]
    saved = clients.load_clients(profile)
    assert saved[oauth.PIM_CLIENT_ID]["refresh_token"] == "fake-native-rt"
    assert saved[clients.TEAMS_WEB_CLIENT_ID]["refresh_token"] == "fake-teams"
    out = capsys.readouterr()
    assert not out.out
    assert "FAKE-CODE" in out.err
    assert "fake-native-rt" not in out.err
    assert clients.clients_path(profile).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("result", [{"error": "access_denied"}, _result(tid="wrong"), {}])
def test_failed_setup_preserves_previous_credentials(profile, monkeypatch, result):
    clients.save_client(profile, oauth.PIM_CLIENT_ID, refresh_token="fake-existing")
    responses = iter([DEVICE, result])
    monkeypatch.setattr(pim_setup, "_post", lambda *a: next(responses))
    monkeypatch.setattr(pim_setup.time, "sleep", lambda _: None)
    assert pim_setup.sign_in(profile, CONFIG) == 1
    assert clients.load_clients(profile)[oauth.PIM_CLIENT_ID]["refresh_token"] == "fake-existing"


def test_token_cli_missing_native_credentials_never_exchanges(profile, monkeypatch, capsys):
    from argparse import Namespace

    from owa_piggy import cli

    monkeypatch.setattr(cli, "_resolve_and_activate", lambda a: (profile, 0))
    monkeypatch.setattr(cli, "load_config", lambda: (CONFIG, True))
    monkeypatch.setattr(cli, "exchange_fresh", lambda *a, **k: pytest.fail("unexpected exchange"))
    args = Namespace(audience="pim", scope=None, profile=profile)
    assert cli._mint_and_emit(args, mode="raw") == 1
    assert "clients add pim" in capsys.readouterr().err
