"""Capture-URL plumbing — the persisted per-profile OWA_CAPTURE_URL must
flow into the reseed path so a non-FOCI profile (e.g. the Azure DevOps
broker) navigates the headless reseed back to the SPA that minted its RT,
not OWA. Env override wins; absent both, capture falls back to OWA."""

from owa_piggy import capture
from owa_piggy import reseed as reseed_mod


def _mock_reseed(monkeypatch):
    monkeypatch.setattr(reseed_mod, "clear_cache", lambda: None)
    monkeypatch.setattr(reseed_mod, "iso_utc_now", lambda: "2026-06-12T00:00:00Z")
    monkeypatch.setattr(reseed_mod, "save_config", lambda cfg: None)
    seen = {}

    def _fake_silent(alias, *, timeout=None, headless=None, user_agent=None, capture_url=None):
        seen["capture_url"] = capture_url
        return "ok", {"OWA_REFRESH_TOKEN": "1.AQ_x", "OWA_TENANT_ID": "tid-x"}

    monkeypatch.setattr(capture, "capture_silent", _fake_silent)
    return seen


def test_reseed_passes_persisted_capture_url(monkeypatch, tmp_config):
    from owa_piggy.config import set_active_profile

    set_active_profile("cap")
    seen = _mock_reseed(monkeypatch)
    cfg = {
        "OWA_AUTH_MODE": "capture",
        "OWA_CAPTURE_URL": "https://dev.azure.com/Norconsult-Group/NOCOS/_workitems",
    }
    assert reseed_mod._do_reseed_capture("cap", cfg) == 0
    assert seen["capture_url"] == ("https://dev.azure.com/Norconsult-Group/NOCOS/_workitems")


def test_reseed_env_overrides_persisted_capture_url(monkeypatch, tmp_config):
    from owa_piggy.config import set_active_profile

    set_active_profile("cap2")
    monkeypatch.setenv("OWA_CAPTURE_URL", "https://dev.azure.com/Env-Org/Proj/_git")
    seen = _mock_reseed(monkeypatch)
    cfg = {
        "OWA_AUTH_MODE": "capture",
        "OWA_CAPTURE_URL": "https://dev.azure.com/Norconsult-Group/NOCOS/_workitems",
    }
    assert reseed_mod._do_reseed_capture("cap2", cfg) == 0
    assert seen["capture_url"] == "https://dev.azure.com/Env-Org/Proj/_git"


def test_reseed_no_capture_url_passes_none(monkeypatch, tmp_config):
    """An ordinary FOCI profile has no OWA_CAPTURE_URL; reseed passes None
    and capture_silent falls back to the OWA default."""
    from owa_piggy.config import set_active_profile

    set_active_profile("cap3")
    seen = _mock_reseed(monkeypatch)
    assert reseed_mod._do_reseed_capture("cap3", {"OWA_AUTH_MODE": "capture"}) == 0
    assert seen["capture_url"] is None


def test_capture_silent_capture_url_defaults_to_env(monkeypatch, tmp_config):
    """capture_silent(capture_url=None) resolves OWA_CAPTURE_URL from env via
    _capture_url(); the OWA default applies when env is also unset."""
    monkeypatch.setenv("OWA_CAPTURE_URL", "https://dev.azure.com/X/Y/_workitems")
    assert capture._capture_url() == "https://dev.azure.com/X/Y/_workitems"
    monkeypatch.delenv("OWA_CAPTURE_URL", raising=False)
    assert capture._capture_url() == capture.OWA_URL
