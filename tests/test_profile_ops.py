"""Tests for the higher-level profile lifecycle operations.

Covers:
  - launchd helper paths/labels
  - set_default_profile policy
  - disable_profile + default-promotion
  - delete_profile end-to-end (registry, disk, default promotion)

Each test stubs `is_scheduled`/`unschedule` to keep launchd off the wire
- we never want a test to touch ~/Library/LaunchAgents.
"""
from owa_piggy import profiles
from owa_piggy.config import (
    ensure_profile_registered,
    list_profiles,
    load_profiles_conf,
    profile_dir,
)

# --- set_default_profile ----------------------------------------------


def _make_profile_dir(alias):
    """Create the on-disk profile dir so list_profiles() picks it up."""
    profile_dir(alias).mkdir(parents=True, exist_ok=True)


def test_set_default_profile_validates_alias(tmp_config, clean_env):
    ok, err = profiles.set_default_profile('../escape')
    assert ok is False
    assert 'invalid profile alias' in err


def test_set_default_profile_rejects_unknown_alias(tmp_config, clean_env):
    ok, err = profiles.set_default_profile('ghost')
    assert ok is False
    assert "profile 'ghost' not found" in err


def test_set_default_profile_succeeds_and_registers(tmp_config, clean_env):
    _make_profile_dir('work')
    ok, err = profiles.set_default_profile('work')
    assert ok is True
    assert err == ''
    reg = load_profiles_conf()
    assert reg['OWA_DEFAULT_PROFILE'] == 'work'
    assert 'work' in reg['OWA_PROFILES']


# --- disable_profile --------------------------------------------------


def test_disable_profile_promotes_replacement(tmp_config, clean_env):
    _make_profile_dir('work')
    _make_profile_dir('personal')
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    # work is default (registered first).
    profiles.disable_profile('work')
    reg = load_profiles_conf()
    assert reg['OWA_DEFAULT_PROFILE'] == 'personal'
    assert reg['OWA_PROFILES'] == ['personal']


def test_disable_profile_no_promote_clears_default(tmp_config, clean_env):
    _make_profile_dir('work')
    ensure_profile_registered('work')
    profiles.disable_profile('work', promote_replacement=False)
    reg = load_profiles_conf()
    assert reg['OWA_DEFAULT_PROFILE'] == ''
    assert reg['OWA_PROFILES'] == []


def test_disable_profile_idempotent_when_missing(tmp_config, clean_env):
    """Disabling a profile that isn't registered is a no-op, not an error."""
    ok, _ = profiles.disable_profile('ghost')
    assert ok is True


# --- delete_profile ---------------------------------------------------


def _stub_launchd(monkeypatch, *, installed=False, run_rc=0):
    """Replace launchd.is_scheduled / unschedule so tests stay away from
    the real launchd. Returns a list that captures every unschedule call
    for assertions. `installed` here means "this profile is scheduled"."""
    calls = []
    monkeypatch.setattr(profiles, 'launchd_is_scheduled',
                        lambda alias: installed)
    monkeypatch.setattr(profiles, 'launchd_unschedule',
                        lambda alias: (calls.append((alias, False)) or run_rc))
    return calls


def test_delete_profile_removes_dir_and_unregisters(monkeypatch, tmp_config,
                                                    clean_env):
    _make_profile_dir('work')
    ensure_profile_registered('work')
    _stub_launchd(monkeypatch, installed=False)

    ok, err = profiles.delete_profile('work')
    assert ok is True
    assert err == ''
    assert list_profiles() == []
    assert load_profiles_conf()['OWA_PROFILES'] == []


def test_delete_profile_uninstalls_launchd_when_present(monkeypatch, tmp_config,
                                                        clean_env):
    _make_profile_dir('work')
    ensure_profile_registered('work')
    calls = _stub_launchd(monkeypatch, installed=True)

    ok, _ = profiles.delete_profile('work')
    assert ok is True
    # launchd uninstall fired exactly once for the deleted profile.
    assert calls == [('work', False)]


def test_delete_profile_stops_when_launchd_unschedule_fails(monkeypatch,
                                                            tmp_config,
                                                            clean_env):
    _make_profile_dir('work')
    ensure_profile_registered('work')
    calls = _stub_launchd(monkeypatch, installed=True, run_rc=9)

    ok, err = profiles.delete_profile('work')

    assert ok is False
    assert 'failed to unschedule launchd' in err
    assert calls == [('work', False)]
    assert profile_dir('work').exists()
    assert list_profiles() == ['work']


def test_delete_profile_promotes_default(monkeypatch, tmp_config, clean_env):
    _make_profile_dir('work')
    _make_profile_dir('personal')
    ensure_profile_registered('work')
    ensure_profile_registered('personal')  # work stays default
    _stub_launchd(monkeypatch, installed=False)

    ok, _ = profiles.delete_profile('work')
    assert ok is True
    reg = load_profiles_conf()
    assert reg['OWA_DEFAULT_PROFILE'] == 'personal'


def test_delete_profile_skips_promotion_when_disabled(monkeypatch, tmp_config,
                                                     clean_env):
    _make_profile_dir('work')
    _make_profile_dir('personal')
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    _stub_launchd(monkeypatch, installed=False)

    ok, _ = profiles.delete_profile('work', promote_default=False)
    assert ok is True
    assert load_profiles_conf()['OWA_DEFAULT_PROFILE'] == ''


def test_delete_profile_returns_error_when_unregister_fails(
    monkeypatch, tmp_config, clean_env
):
    _make_profile_dir('work')
    ensure_profile_registered('work')
    _stub_launchd(monkeypatch, installed=False)
    monkeypatch.setattr(profiles, 'unregister_profile',
                        lambda alias: (_ for _ in ()).throw(OSError('disk full')))

    ok, err = profiles.delete_profile('work')
    assert ok is False
    assert 'disk full' in err
    # The on-disk directory must remain so a retry can recover.
    assert profile_dir('work').exists()
