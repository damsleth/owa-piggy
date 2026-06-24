"""Tests for tenant-templated SharePoint audiences and auto-derivation.

No network, no real tokens: exchange_fresh and urlopen are monkeypatched.
"""

import io
import json

from owa_piggy import sharepoint as sp_mod
from owa_piggy.scopes import (
    KNOWN_AUDIENCE_TEMPLATES,
    resolve_audience,
    templated_audience_name,
)

# --- templated_audience_name (pure precedence logic) -----------------------


def test_templated_name_explicit_audience(monkeypatch):
    monkeypatch.delenv("OWA_DEFAULT_AUDIENCE", raising=False)
    assert templated_audience_name("sharepoint") == "sharepoint"
    assert templated_audience_name("sharepoint-admin") == "sharepoint-admin"
    assert templated_audience_name("graph") is None


def test_templated_name_scope_short_circuits(monkeypatch):
    monkeypatch.delenv("OWA_DEFAULT_AUDIENCE", raising=False)
    # An explicit --scope means resolve_audience never templates.
    assert templated_audience_name("sharepoint", scope="https://x/.default") is None


def test_templated_name_env_and_profile_default(monkeypatch):
    monkeypatch.setenv("OWA_DEFAULT_AUDIENCE", "sharepoint")
    assert templated_audience_name() == "sharepoint"
    # A non-templated env value wins over the profile default -> not templated.
    monkeypatch.setenv("OWA_DEFAULT_AUDIENCE", "graph")
    assert templated_audience_name(profile_default="sharepoint") is None
    monkeypatch.delenv("OWA_DEFAULT_AUDIENCE", raising=False)
    assert templated_audience_name(profile_default="sharepoint") == "sharepoint"


def test_templated_name_covers_all_templates(monkeypatch):
    monkeypatch.delenv("OWA_DEFAULT_AUDIENCE", raising=False)
    for name in KNOWN_AUDIENCE_TEMPLATES:
        assert templated_audience_name(name) == name


# --- resolve_audience templated substitution -------------------------------


def test_resolve_sharepoint_content(monkeypatch):
    monkeypatch.delenv("OWA_SHAREPOINT_TENANT", raising=False)
    scope, err = resolve_audience("sharepoint", sharepoint_tenant="contoso")
    assert err == ""
    assert scope.startswith("https://contoso.sharepoint.com/.default")


def test_resolve_sharepoint_admin(monkeypatch):
    monkeypatch.delenv("OWA_SHAREPOINT_TENANT", raising=False)
    scope, err = resolve_audience("sharepoint-admin", sharepoint_tenant="contoso")
    assert err == ""
    assert scope.startswith("https://contoso-admin.sharepoint.com/.default")


def test_resolve_sharepoint_missing_tenant_errors(monkeypatch):
    monkeypatch.delenv("OWA_SHAREPOINT_TENANT", raising=False)
    scope, err = resolve_audience("sharepoint")
    assert scope == ""
    assert "SharePoint tenant name" in err


# --- derive_sharepoint_tenant (Graph round-trip, mocked) -------------------


def _fake_exchange_ok(config, scope, *, persist, capture_stderr=False):
    return {"access_token": "at"}, {"aad_error": None}


def _sites_root_response(hostname):
    payload = json.dumps({"siteCollection": {"hostname": hostname}}).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    def _urlopen(req, timeout=None):
        return _Resp(payload)

    return _urlopen


def test_derive_persists_tenant(monkeypatch):
    monkeypatch.setattr(sp_mod, "exchange_fresh", _fake_exchange_ok)
    monkeypatch.setattr(
        sp_mod.urllib.request, "urlopen", _sites_root_response("contoso365.sharepoint.com")
    )
    saved = {}
    monkeypatch.setattr(sp_mod, "save_config", lambda cfg: saved.update(cfg))

    config = {"OWA_REFRESH_TOKEN": "1.x", "OWA_TENANT_ID": "t"}
    tenant, err = sp_mod.derive_sharepoint_tenant(config, persist=True)
    assert err == ""
    assert tenant == "contoso365"
    assert config["OWA_SHAREPOINT_TENANT"] == "contoso365"
    assert saved.get("OWA_SHAREPOINT_TENANT") == "contoso365"


def test_derive_no_persist_when_not_persist(monkeypatch):
    monkeypatch.setattr(sp_mod, "exchange_fresh", _fake_exchange_ok)
    monkeypatch.setattr(
        sp_mod.urllib.request, "urlopen", _sites_root_response("contoso365.sharepoint.com")
    )
    called = []
    monkeypatch.setattr(sp_mod, "save_config", lambda cfg: called.append(cfg))

    config = {"OWA_REFRESH_TOKEN": "1.x", "OWA_TENANT_ID": "t"}
    tenant, err = sp_mod.derive_sharepoint_tenant(config, persist=False)
    assert (tenant, err) == ("contoso365", "")
    assert called == []
    assert "OWA_SHAREPOINT_TENANT" not in config


def test_derive_graph_token_failure(monkeypatch):
    def _fail(config, scope, *, persist, capture_stderr=False):
        return None, {"aad_error": "AADSTS700084"}

    monkeypatch.setattr(sp_mod, "exchange_fresh", _fail)

    tenant, err = sp_mod.derive_sharepoint_tenant({}, persist=False)
    assert tenant == ""
    assert "AADSTS700084" in err


def test_derive_unexpected_hostname(monkeypatch):
    monkeypatch.setattr(sp_mod, "exchange_fresh", _fake_exchange_ok)
    monkeypatch.setattr(sp_mod.urllib.request, "urlopen", _sites_root_response("weird.example.com"))
    tenant, err = sp_mod.derive_sharepoint_tenant({}, persist=False)
    assert tenant == ""
    assert "unexpected SharePoint hostname" in err
