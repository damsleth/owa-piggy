"""Tests for the Google consent branch of interactive_setup (no browser/network)."""

from owa_piggy import oauth_google
from owa_piggy import setup as setup_mod


def test_google_setup_persists_provider_and_tokens(tmp_config, clean_env, monkeypatch):
    monkeypatch.setattr(
        oauth_google,
        "run_local_consent_flow",
        lambda cid, secret, **kw: {"refresh_token": "grt", "access_token": "gat"},
    )
    config = {}

    ok = setup_mod.interactive_setup(
        config,
        "gmail-personal",
        google=True,
        google_client_id="cid",
        google_client_secret="secret",
    )

    assert ok is True
    assert config["OWA_PROVIDER"] == "google"
    assert config["OWA_CLIENT_ID"] == "cid"
    assert config["OWA_CLIENT_SECRET"] == "secret"
    assert config["OWA_REFRESH_TOKEN"] == "grt"
    # No hard-cap timestamp - Google refresh tokens don't expire on a
    # schedule the way AAD's SPA hard-cap does.
    assert "OWA_RT_ISSUED_AT" not in config
    assert tmp_config.exists()


def test_google_setup_requires_client_credentials(tmp_config, clean_env, capsys):
    ok = setup_mod.interactive_setup({}, "gmail-personal", google=True)

    assert ok is False
    assert "requires --google-client-id" in capsys.readouterr().err


def test_google_setup_no_refresh_token_fails(tmp_config, clean_env, monkeypatch, capsys):
    monkeypatch.setattr(
        oauth_google,
        "run_local_consent_flow",
        lambda cid, secret, **kw: {"access_token": "gat"},
    )

    ok = setup_mod.interactive_setup(
        {},
        "gmail-personal",
        google=True,
        google_client_id="cid",
        google_client_secret="secret",
    )

    assert ok is False
    assert "no refresh_token" in capsys.readouterr().err


def test_google_setup_consent_error_surfaces(tmp_config, clean_env, monkeypatch, capsys):
    def raise_consent_error(cid, secret, **kw):
        raise oauth_google.ConsentError("user denied access")

    monkeypatch.setattr(oauth_google, "run_local_consent_flow", raise_consent_error)

    ok = setup_mod.interactive_setup(
        {},
        "gmail-personal",
        google=True,
        google_client_id="cid",
        google_client_secret="secret",
    )

    assert ok is False
    assert "user denied access" in capsys.readouterr().err
