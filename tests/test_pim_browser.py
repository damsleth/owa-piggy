"""Browser sign-in uses only a local callback and synthetic OAuth responses."""

import base64
import hashlib
import http.client
import threading
import urllib.parse

import pytest

from owa_piggy import clients, oauth, pim_browser

from .test_pim import CONFIG, _result


@pytest.fixture
def profile(tmp_path, monkeypatch):
    from owa_piggy import config

    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    return "work"


@pytest.mark.parametrize("bad_nonce", [False, True])
def test_browser_pkce_and_identity_before_save(profile, monkeypatch, capsys, bad_nonce):
    params = {}
    responses = []
    threads = []

    def browser_open(url):
        parsed = urllib.parse.urlsplit(url)
        assert parsed.hostname == "login.microsoftonline.com"
        params.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
        callback = urllib.parse.urlsplit(params["redirect_uri"])
        assert callback.hostname == "localhost"
        assert params["code_challenge_method"] == "S256"

        def visit():
            connection = http.client.HTTPConnection("127.0.0.1", callback.port, timeout=5)
            for state in ["wrong-state", params["state"]]:
                query = urllib.parse.urlencode({"code": "fake-auth-code", "state": state})
                connection.request("GET", "/?" + query)
                response = connection.getresponse()
                responses.append(response.status)
                response.read()
            connection.close()

        thread = threading.Thread(target=visit)
        thread.start()
        threads.append(thread)
        return True

    def post(tenant, endpoint, fields):
        assert tenant == CONFIG["OWA_TENANT_ID"]
        assert endpoint == "token"
        assert fields["grant_type"] == "authorization_code"
        assert fields["code"] == "fake-auth-code"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"].encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == params["code_challenge"]
        assert fields["redirect_uri"] == params["redirect_uri"]
        return _result(nonce="wrong" if bad_nonce else params["nonce"])

    monkeypatch.setattr(pim_browser.webbrowser, "open", browser_open)
    monkeypatch.setattr(pim_browser.pim_setup, "_post", post)
    try:
        assert pim_browser.sign_in(profile, CONFIG) == (1 if bad_nonce else 0)
    finally:
        for thread in threads:
            thread.join(timeout=6)
            assert not thread.is_alive()
    assert responses == [400, 200]
    saved = clients.load_clients(profile)
    assert (oauth.PIM_CLIENT_ID in saved) is not bad_nonce
    out = capsys.readouterr()
    assert not out.out
    assert "fake-auth-code" not in out.err
    assert "fake-native-rt" not in out.err


def test_browser_open_failure_preserves_credentials(profile, monkeypatch):
    monkeypatch.setattr(pim_browser.webbrowser, "open", lambda url: False)
    assert pim_browser.sign_in(profile, CONFIG) == 1
    assert clients.load_clients(profile) == {}
