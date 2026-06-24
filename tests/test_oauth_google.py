"""Tests for the Google OAuth exchange helper (no CDP/Edge involved)."""

import json
import urllib.error

import pytest

from owa_piggy import oauth_google


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_consent_url_has_offline_and_consent_prompt():
    url = oauth_google._consent_url(
        "cid", "http://127.0.0.1:1234", oauth_google.DEFAULT_SCOPES, "st"
    )
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=st" in url


def test_refresh_access_token_success(monkeypatch):
    monkeypatch.setattr(
        oauth_google.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp({"access_token": "AT", "expires_in": 3600}),
    )
    result = oauth_google.refresh_access_token("cid", "secret", "RT")
    assert result == {"access_token": "AT", "expires_in": 3600}


def test_refresh_access_token_http_error_returns_none(monkeypatch, capsys):
    import io

    def raise_http_error(req, timeout=None):
        body = json.dumps({"error": "invalid_grant", "error_description": "bad rt"}).encode()
        raise urllib.error.HTTPError(
            oauth_google.TOKEN_URL,
            400,
            "Bad Request",
            {},
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(oauth_google.urllib.request, "urlopen", raise_http_error)

    result = oauth_google.refresh_access_token("cid", "secret", "RT")

    assert result is None
    assert "invalid_grant" in capsys.readouterr().err


def test_run_local_consent_flow_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(oauth_google.webbrowser, "open", lambda url: True)
    with pytest.raises(oauth_google.ConsentError, match="no response"):
        oauth_google.run_local_consent_flow("cid", "secret", timeout=0.2)
