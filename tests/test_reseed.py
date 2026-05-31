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


def test_reseed_scheduled_only_touches_scheduled_profiles(
    monkeypatch, tmp_config, clean_env
):
    """do_reseed_scheduled reseeds exactly OWA_SCHEDULED ∩ on-disk, not
    every enabled profile."""
    from owa_piggy.config import profile_dir, save_profiles_conf

    for alias in ('work', 'personal', 'side'):
        profile_dir(alias).mkdir(parents=True)
    save_profiles_conf({
        'OWA_DEFAULT_PROFILE': 'work',
        'OWA_PROFILES': ['work', 'personal', 'side'],
        'OWA_SCHEDULED': ['work', 'side'],
    })
    calls = []
    monkeypatch.setattr(
        reseed_mod, 'do_reseed', lambda alias: calls.append(alias) or 0,
    )

    rc = reseed_mod.do_reseed_scheduled()

    assert rc == 0
    assert calls == ['work', 'side']


def test_reseed_scheduled_empty_is_not_an_error(
    monkeypatch, tmp_config, clean_env, capsys
):
    """An empty schedule is a valid state; the hourly agent firing into it
    is a no-op, not a failure."""
    from owa_piggy.config import profile_dir, save_profiles_conf

    profile_dir('work').mkdir(parents=True)
    save_profiles_conf({
        'OWA_DEFAULT_PROFILE': 'work',
        'OWA_PROFILES': ['work'],
        'OWA_SCHEDULED': [],
    })
    calls = []
    monkeypatch.setattr(
        reseed_mod, 'do_reseed', lambda alias: calls.append(alias) or 0,
    )

    rc = reseed_mod.do_reseed_scheduled()

    assert rc == 0
    assert calls == []
    assert 'no scheduled profiles' in capsys.readouterr().err


def test_reseed_scheduled_skips_missing_profile_dir(
    monkeypatch, tmp_config, clean_env, capsys
):
    """A scheduled alias whose profile dir is gone is skipped with a
    warning, not a hard failure of the whole run."""
    from owa_piggy.config import profile_dir, save_profiles_conf

    profile_dir('work').mkdir(parents=True)
    save_profiles_conf({
        'OWA_DEFAULT_PROFILE': 'work',
        'OWA_PROFILES': ['work', 'ghost'],
        'OWA_SCHEDULED': ['work', 'ghost'],
    })
    # ghost has no dir on disk (never created); save dropped nothing because
    # ghost is in OWA_PROFILES, but list_profiles only sees 'work'.
    calls = []
    monkeypatch.setattr(
        reseed_mod, 'do_reseed', lambda alias: calls.append(alias) or 0,
    )

    rc = reseed_mod.do_reseed_scheduled()

    assert rc == 0
    assert calls == ['work']
    assert 'skipping scheduled profile with no config on disk: ghost' \
        in capsys.readouterr().err


def test_profile_cdp_port_is_stable_and_matches_shell_formula():
    """The Python port derivation must match scripts/setup-refresh.sh's
    `9222 + cksum % 10000` so a profile keeps its debug port across the
    scrape backend regardless of which code path computes it."""
    p1 = reseed_mod._profile_cdp_port('work')
    p2 = reseed_mod._profile_cdp_port('work')
    assert p1 == p2
    assert 9222 <= p1 < 9222 + 10000
    # Different aliases generally land on different ports.
    assert reseed_mod._profile_cdp_port('work') != \
        reseed_mod._profile_cdp_port('personal')


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
        lambda alias, **kwargs: ('ok', {
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
