"""Tests for reseed helpers."""

from owa_piggy import capture as capture_mod
from owa_piggy import reseed as reseed_mod


def test_reseed_all_skips_when_registry_present_but_empty(
    monkeypatch, tmp_config, clean_env, capsys
):
    """An empty OWA_PROFILES list means every on-disk profile is disabled,
    not a legacy install."""
    from owa_piggy.config import profile_dir, save_profiles_conf

    profile_dir('work').mkdir(parents=True)
    save_profiles_conf({'OWA_DEFAULT_PROFILE': '', 'OWA_PROFILES': []})
    calls = []
    monkeypatch.setattr(
        reseed_mod, 'do_reseed', lambda alias: calls.append(alias) or 0,
    )

    rc = reseed_mod.do_reseed_all()

    assert rc == 1
    assert calls == []
    err = capsys.readouterr().err
    assert 'skipping disabled profile: work' in err
    assert 'no active profiles to reseed' in err


def test_reseed_all_keeps_legacy_fallback_when_registry_missing(
    monkeypatch, tmp_config, clean_env
):
    """Missing profiles.conf is the backwards-compatible legacy case."""
    from owa_piggy.config import profile_dir, profiles_conf_path

    profile_dir('work').mkdir(parents=True)
    assert not profiles_conf_path().exists()
    calls = []
    monkeypatch.setattr(
        reseed_mod, 'do_reseed', lambda alias: calls.append(alias) or 0,
    )

    rc = reseed_mod.do_reseed_all()

    assert rc == 0
    assert calls == ['work']


def test_capture_reseed_clears_cache(monkeypatch, tmp_config, clean_env):
    """Capture-mode reseed must clear the per-profile AT cache even when
    called directly (the --all path bypasses cli._cmd_reseed's pre-clear)."""
    from owa_piggy.config import set_active_profile

    set_active_profile('work')
    cleared = []
    saved = {}

    monkeypatch.setattr(reseed_mod, 'clear_cache', lambda: cleared.append(True))
    monkeypatch.setattr(
        capture_mod,
        'capture_silent',
        lambda alias: ('ok', {
            'OWA_REFRESH_TOKEN': '1.AQ_fake-rotated',
            'OWA_TENANT_ID': 'tid-1',
        }),
    )
    monkeypatch.setattr(reseed_mod, 'iso_utc_now', lambda: '2026-04-30T12:00:00Z')
    monkeypatch.setattr(reseed_mod, 'save_config', lambda config: saved.update(config))

    rc = reseed_mod._do_reseed_capture('work', {'OWA_AUTH_MODE': 'capture'})
    assert rc == 0
    assert cleared == [True]
    assert saved['OWA_REFRESH_TOKEN'] == '1.AQ_fake-rotated'
    assert saved['OWA_TENANT_ID'] == 'tid-1'
    assert saved['OWA_RT_ISSUED_AT'] == '2026-04-30T12:00:00Z'
