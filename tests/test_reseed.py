"""Tests for reseed helpers."""

from owa_piggy import capture as capture_mod
from owa_piggy import reseed as reseed_mod


def test_reseed_skips_google_profiles(tmp_config, clean_env, capsys):
    """Google refresh tokens don't need Edge-driven silent refresh - reseed
    should no-op successfully rather than trying to launch Edge for them."""
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile('gmail-personal')
    save_config({
        'OWA_PROVIDER': 'google',
        'OWA_REFRESH_TOKEN': 'grt',
        'OWA_CLIENT_ID': 'gcid',
        'OWA_CLIENT_SECRET': 'gsecret',
    })

    rc = reseed_mod.do_reseed('gmail-personal')

    assert rc == 0
    assert 'reseed not applicable' in capsys.readouterr().err


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


def _mock_capture_reseed(monkeypatch, results):
    """Monkeypatch the capture-reseed collaborators. `results` is a list of
    (status, config) tuples popped per capture_silent call; each call's
    headless kwarg is recorded. Returns (calls, saved)."""
    calls = []
    saved = {}

    def fake_silent(alias, **kwargs):
        calls.append(kwargs.get('headless'))
        return results.pop(0)

    monkeypatch.setattr(reseed_mod, 'clear_cache', lambda: None)
    monkeypatch.setattr(capture_mod, 'capture_silent', fake_silent)
    monkeypatch.setattr(reseed_mod, 'iso_utc_now', lambda: '2026-08-19T00:00:00Z')
    monkeypatch.setattr(reseed_mod, 'save_config', lambda config: saved.update(config))
    return calls, saved


def test_capture_reseed_falls_back_to_non_headless_on_error(
    monkeypatch, tmp_config, clean_env, capsys
):
    """Two headless timeouts ('error') must trigger the offscreen
    non-headless fallback, and success there persists
    OWA_CAPTURE_HEADLESS=0 so future reseeds skip the doomed headless
    attempts."""
    calls, saved = _mock_capture_reseed(monkeypatch, [
        ('error', None),
        ('error', None),
        ('ok', {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'}),
    ])

    rc = reseed_mod._do_reseed_capture('brkh', {'OWA_AUTH_MODE': 'capture'})

    assert rc == 0
    assert calls == [True, True, False]
    assert saved['OWA_CAPTURE_HEADLESS'] == '0'
    assert 'falling back to non-headless' in capsys.readouterr().err


def test_capture_reseed_honors_persisted_headless_zero(
    monkeypatch, tmp_config, clean_env
):
    """A profile that persisted a recent OWA_CAPTURE_HEADLESS=0 goes
    straight to non-headless - no wasted headless attempts."""
    calls, saved = _mock_capture_reseed(monkeypatch, [
        ('ok', {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'}),
    ])

    rc = reseed_mod._do_reseed_capture(
        'brkh', {'OWA_AUTH_MODE': 'capture', 'OWA_CAPTURE_HEADLESS': '0',
                 'OWA_CAPTURE_HEADLESS_AT': _stamp_hours_ago(1)})

    assert rc == 0
    assert calls == [False]


def test_capture_reseed_env_overrides_persisted_headless(
    monkeypatch, tmp_config, clean_env
):
    """OWA_CAPTURE_HEADLESS=1 in the environment beats a persisted '0'
    (ad-hoc experimentation after a tenant policy change)."""
    monkeypatch.setenv('OWA_CAPTURE_HEADLESS', '1')
    calls, saved = _mock_capture_reseed(monkeypatch, [
        ('ok', {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'}),
    ])

    rc = reseed_mod._do_reseed_capture(
        'brkh', {'OWA_AUTH_MODE': 'capture', 'OWA_CAPTURE_HEADLESS': '0'})

    assert rc == 0
    assert calls == [True]


def test_capture_reseed_no_fallback_when_already_non_headless(
    monkeypatch, tmp_config, clean_env
):
    """Persistent 'error' in non-headless mode fails without a redundant
    third attempt (there is no more-capable mode to fall back to)."""
    calls, saved = _mock_capture_reseed(monkeypatch, [
        ('error', None),
        ('error', None),
    ])

    rc = reseed_mod._do_reseed_capture(
        'brkh', {'OWA_AUTH_MODE': 'capture', 'OWA_CAPTURE_HEADLESS': '0',
                 'OWA_CAPTURE_HEADLESS_AT': _stamp_hours_ago(1)})

    assert rc == 1
    assert calls == [False, False]
    assert 'OWA_CAPTURE_HEADLESS' not in saved


def test_legacy_reseed_script_binds_debugging_to_loopback():
    from pathlib import Path

    script = Path('scripts/reseed-from-edge.sh').read_text()
    assert '--remote-debugging-address=127.0.0.1' in script


def _pref(cfg, env=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.delenv('OWA_CAPTURE_HEADLESS', raising=False)
        if env is not None:
            monkeypatch.setenv('OWA_CAPTURE_HEADLESS', env)
    return reseed_mod._headless_pref(cfg)


def test_headless_pref_default_and_env(monkeypatch):
    assert _pref({}, monkeypatch=monkeypatch) is True
    assert _pref({}, env='0', monkeypatch=monkeypatch) is False
    assert _pref({'OWA_CAPTURE_HEADLESS': '0'}, env='1',
                 monkeypatch=monkeypatch) is True


def test_headless_pref_expires_after_a_day(monkeypatch):
    """A fallback that succeeded once must not put a browser window onscreen
    forever: the auto-written preference is retried after 24h."""
    fresh = {'OWA_CAPTURE_HEADLESS': '0',
             'OWA_CAPTURE_HEADLESS_AT': _stamp_hours_ago(1)}
    stale = {'OWA_CAPTURE_HEADLESS': '0',
             'OWA_CAPTURE_HEADLESS_AT': _stamp_hours_ago(25)}
    unstamped = {'OWA_CAPTURE_HEADLESS': '0'}
    assert _pref(fresh, monkeypatch=monkeypatch) is False
    assert _pref(stale, monkeypatch=monkeypatch) is True
    # Written before the stamp existed - treat it as expired, not eternal.
    assert _pref(unstamped, monkeypatch=monkeypatch) is True


def _stamp_hours_ago(hours):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_expired_headless_zero_is_retried_and_cleared(
    monkeypatch, tmp_config, clean_env
):
    """The bug this exists for: one fallback used to pin a profile to a
    visible browser window forever. A day later headless gets another go,
    and a success clears the preference."""
    calls, saved = _mock_capture_reseed(monkeypatch, [
        ('ok', {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'}),
    ])

    rc = reseed_mod._do_reseed_capture(
        'brkh', {'OWA_AUTH_MODE': 'capture', 'OWA_CAPTURE_HEADLESS': '0',
                 'OWA_CAPTURE_HEADLESS_AT': _stamp_hours_ago(30)})

    assert rc == 0
    assert calls == [True]
    assert saved['OWA_CAPTURE_HEADLESS'] == '1'


def test_fallback_stamps_the_headless_preference(
    monkeypatch, tmp_config, clean_env
):
    calls, saved = _mock_capture_reseed(monkeypatch, [
        ('headless_blocked', None),
        ('ok', {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'}),
    ])
    monkeypatch.setattr(reseed_mod.sys.stdin, 'isatty', lambda: False,
                        raising=False)

    rc = reseed_mod._do_reseed_capture('brkh', {'OWA_AUTH_MODE': 'capture'})

    assert rc == 0
    assert saved['OWA_CAPTURE_HEADLESS'] == '0'
    assert saved['OWA_CAPTURE_HEADLESS_AT']


def test_interactive_rescue_does_not_credit_headless(
    monkeypatch, tmp_config, clean_env
):
    """A TTY run where headless failed and the *user* signed in must not
    record 'headless works again' - that mis-attribution made the profile
    flap between headless and a visible window every 24h."""
    calls, saved = _mock_capture_reseed(monkeypatch, [('headless_blocked', None)])
    monkeypatch.setattr(reseed_mod.sys.stdin, 'isatty', lambda: True,
                        raising=False)
    monkeypatch.setattr(
        capture_mod, 'capture_signin',
        lambda *a, **kw: {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'})

    rc = reseed_mod._do_reseed_capture(
        'brkh', {'OWA_AUTH_MODE': 'capture', 'OWA_EMAIL': 'a@b.no',
                 'OWA_CAPTURE_HEADLESS': '0',
                 'OWA_CAPTURE_HEADLESS_AT': _stamp_hours_ago(30)})

    assert rc == 0
    assert calls == [True]
    assert saved['OWA_CAPTURE_HEADLESS'] == '0'


def test_env_headless_override_leaves_persisted_preference_alone(
    monkeypatch, tmp_config, clean_env
):
    """An ad-hoc OWA_CAPTURE_HEADLESS=1 experiment must not rewrite the state
    the next launchd run reads."""
    calls, saved = _mock_capture_reseed(monkeypatch, [
        ('ok', {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'}),
    ])
    monkeypatch.setenv('OWA_CAPTURE_HEADLESS', '1')

    rc = reseed_mod._do_reseed_capture(
        'brkh', {'OWA_AUTH_MODE': 'capture', 'OWA_CAPTURE_HEADLESS': '0',
                 'OWA_CAPTURE_HEADLESS_AT': _stamp_hours_ago(1)})

    assert rc == 0
    assert calls == [True]
    assert saved['OWA_CAPTURE_HEADLESS'] == '0'
