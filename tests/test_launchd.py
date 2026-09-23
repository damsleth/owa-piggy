"""Tests for shared launchd scheduling state."""

from owa_piggy import launchd
from owa_piggy.config import load_profiles_conf


def test_schedule_rolls_back_when_shared_install_fails(monkeypatch, tmp_config, clean_env):
    monkeypatch.setattr(launchd, "shared_agent_installed", lambda: False)
    monkeypatch.setattr(launchd, "_run_setup_refresh_script", lambda *a: 7)

    assert launchd.schedule("work") == 7

    reg = load_profiles_conf()
    assert reg["OWA_PROFILES"] == ["work"]
    assert reg["OWA_SCHEDULED"] == []


def test_schedule_refuses_a_disabled_profile(monkeypatch, tmp_config, clean_env):
    """Scheduling must not re-register a switched-off profile as a side
    effect - the hourly agent would start reseeding it again."""
    from owa_piggy.config import ensure_profile_registered

    monkeypatch.setattr(launchd, "shared_agent_installed", lambda: True)
    ensure_profile_registered("work")

    assert launchd.schedule("retired") == 1

    reg = load_profiles_conf()
    assert reg["OWA_PROFILES"] == ["work"]
    assert reg["OWA_SCHEDULED"] == []
