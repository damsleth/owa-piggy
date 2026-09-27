"""Tests for the HaloITSM provider: exchange helper, dispatch, setup, cache (no network)."""

import io
import json
import urllib.error
import urllib.parse

from owa_piggy import oauth_halo, token_flow
from owa_piggy import setup as setup_mod
from owa_piggy.config import classify_profile_type


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_normalize_host_accepts_alias_host_or_url():
    assert oauth_halo.normalize_host("norconsult") == "norconsult.haloitsm.com"
    assert oauth_halo.normalize_host("norconsult.haloitsm.com") == "norconsult.haloitsm.com"
    assert (
        oauth_halo.normalize_host("https://norconsult.haloitsm.com/ticket?id=1")
        == "norconsult.haloitsm.com"
    )


def test_refresh_posts_public_client_grant_and_drops_id_token(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = urllib.parse.parse_qs(req.data.decode())
        return _FakeResp(
            {"access_token": "hat", "token_type": "Bearer", "expires_in": 3600, "id_token": "x.y.z"}
        )

    monkeypatch.setattr(oauth_halo.urllib.request, "urlopen", fake_urlopen)

    result = oauth_halo.refresh_access_token("norconsult", "fake-rt-for-tests")

    assert result == {"access_token": "hat", "token_type": "Bearer", "expires_in": 3600}
    assert seen["url"].startswith("https://norconsult.haloitsm.com/auth/token?Redirect_Uri=")
    assert seen["body"]["grant_type"] == ["refresh_token"]
    assert seen["body"]["client_id"] == [oauth_halo.HALO_CLIENT_ID]
    assert "client_secret" not in seen["body"]


def test_refresh_http_error_returns_none(monkeypatch, capsys):
    def fake_urlopen(req, timeout):
        body = io.BytesIO(b'{"error":"invalid_grant"}')
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, body)

    monkeypatch.setattr(oauth_halo.urllib.request, "urlopen", fake_urlopen)

    assert oauth_halo.refresh_access_token("norconsult", "fake-rt-for-tests") is None
    assert "invalid_grant" in capsys.readouterr().err


def test_exchange_fresh_halo_uses_host_from_tenant_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        token_flow,
        "halo_exchange_token",
        lambda host, rt: calls.append((host, rt)) or {"access_token": "HAT"},
    )
    config = {
        "OWA_PROVIDER": "halo",
        "OWA_TENANT_ID": "norconsult.haloitsm.com",
        "OWA_REFRESH_TOKEN": "opaque-halo-rt",
    }

    result, info = token_flow.exchange_fresh(config, "ignored", persist=False)

    assert result == {"access_token": "HAT"}
    assert calls == [("norconsult.haloitsm.com", "opaque-halo-rt")]
    assert info["rt_shape_ok"] is True
    assert info["aad_error"] is None


def test_exchange_fresh_halo_without_host_short_circuits(monkeypatch):
    monkeypatch.setattr(token_flow, "halo_exchange_token", lambda *a: 1 / 0)
    config = {"OWA_PROVIDER": "halo", "OWA_REFRESH_TOKEN": "opaque-halo-rt"}

    result, info = token_flow.exchange_fresh(config, "", persist=False)

    assert result is None
    assert info["tid_present"] is False


def test_classify_profile_type_halo():
    assert classify_profile_type({"OWA_PROVIDER": "halo"}) == "halo"


def test_halo_setup_verifies_then_persists(tmp_config, clean_env, monkeypatch):
    monkeypatch.setattr(setup_mod.sys, "stdin", io.StringIO("fake-rt-for-tests\n"))
    monkeypatch.setattr(oauth_halo, "refresh_access_token", lambda host, rt: {"access_token": "a"})
    config = {}

    assert setup_mod.interactive_setup(config, "nc-halo", halo="norconsult") is True
    assert config == {
        "OWA_PROVIDER": "halo",
        "OWA_TENANT_ID": "norconsult.haloitsm.com",
        "OWA_REFRESH_TOKEN": "fake-rt-for-tests",
    }
    assert tmp_config.exists()


def test_halo_setup_rejected_token_is_not_saved(tmp_config, clean_env, monkeypatch):
    monkeypatch.setattr(setup_mod.sys, "stdin", io.StringIO("dead-rt\n"))
    monkeypatch.setattr(oauth_halo, "refresh_access_token", lambda host, rt: None)

    assert setup_mod.interactive_setup({}, "nc-halo", halo="norconsult") is False
    assert not tmp_config.exists()


def test_halo_token_json_is_cached_without_jwt(monkeypatch, capsys, tmp_config, clean_env):
    from owa_piggy import cli
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile("work")
    save_config(
        {
            "OWA_PROVIDER": "halo",
            "OWA_TENANT_ID": "norconsult.haloitsm.com",
            "OWA_REFRESH_TOKEN": "opaque-halo-rt",
        }
    )
    calls = []
    monkeypatch.setattr(
        token_flow,
        "halo_exchange_token",
        lambda host, rt: calls.append(host) or {"access_token": "hat-opaque", "expires_in": 3600},
    )

    monkeypatch.setattr(cli.sys, "argv", ["owa-piggy", "token", "--json"])
    assert cli.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["access_token"] == "hat-opaque"
    assert first["host"] == "norconsult.haloitsm.com"
    # Second call is served from the per-profile cache: no second exchange.
    monkeypatch.setattr(cli.sys, "argv", ["owa-piggy", "token", "--json"])
    assert cli.main() == 0
    second = json.loads(capsys.readouterr().out)
    assert (second["access_token"], second["host"]) == ("hat-opaque", "norconsult.haloitsm.com")
    assert calls == ["norconsult.haloitsm.com"]


# --- Halo as a service on an AAD profile ------------------------------------


def test_halo_service_token_mints_from_the_bound_entry(monkeypatch, capsys, tmp_config, clean_env):
    """`token --audience halo` on an AAD profile exchanges the clients.json
    Halo entry against its host, never the profile's FOCI token."""
    from owa_piggy import cli, clients
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile("work")
    save_config({"OWA_REFRESH_TOKEN": "1.fake-foci-rt", "OWA_TENANT_ID": "fake-tid"})
    clients.declare_client("work", clients.HALO_KEY, capture_url="https://norconsult.haloitsm.com")
    clients.save_client("work", clients.HALO_KEY, refresh_token="fake-halo-rt")
    calls = []
    monkeypatch.setattr(
        token_flow,
        "halo_exchange_token",
        lambda host, rt: calls.append((host, rt)) or {"access_token": "hat", "expires_in": 3600},
    )

    monkeypatch.setattr(cli.sys, "argv", ["owa-piggy", "token", "--audience", "halo", "--json"])
    assert cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["access_token"], out["host"]) == ("hat", "norconsult.haloitsm.com")
    assert calls == [("norconsult.haloitsm.com", "fake-halo-rt")]
    assert clients.profile_services("work", {}) == ["owa", "halo"]


def test_halo_service_without_a_sign_in_says_how(monkeypatch, capsys, tmp_config, clean_env):
    from owa_piggy import cli
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile("work")
    save_config({"OWA_REFRESH_TOKEN": "1.fake-foci-rt", "OWA_TENANT_ID": "fake-tid"})
    monkeypatch.setattr(cli.sys, "argv", ["owa-piggy", "token", "--audience", "halo"])
    assert cli.main() == 1
    assert "clients add halo=" in capsys.readouterr().err


def test_parse_spec_takes_halo_with_its_url():
    from owa_piggy import clients

    assert clients.parse_spec("halo=https://norconsult.haloitsm.com") == (
        clients.HALO_KEY,
        "https://norconsult.haloitsm.com",
        "",
    )


def test_bound_client_rotation_reads_halo_from_its_cookie(monkeypatch, tmp_config, clean_env):
    """Reseed walks clients.json; the halo entry goes to the cookie capture,
    not the AAD /token interception."""
    from owa_piggy import capture, clients
    from owa_piggy.config import set_active_profile

    set_active_profile("work")
    clients.declare_client("work", clients.HALO_KEY, capture_url="https://norconsult.haloitsm.com")
    seen = []
    monkeypatch.setattr(capture, "capture_silent", lambda *a, **kw: 1 / 0)
    monkeypatch.setattr(
        capture,
        "capture_halo",
        lambda alias, url, headless=None: seen.append(url) or ("ok", {"OWA_REFRESH_TOKEN": "new"}),
    )

    assert capture.capture_bound_clients("work") == (1, [])
    assert seen == ["https://norconsult.haloitsm.com"]
    assert clients.load_clients("work")[clients.HALO_KEY]["refresh_token"] == "new"
