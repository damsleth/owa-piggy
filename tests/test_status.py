"""Tests for status/debug behavior that should mirror token resolution."""
from types import SimpleNamespace

from owa_piggy import status as status_mod


def test_status_json_marks_disabled_without_probe(
    monkeypatch, tmp_config, clean_env
):
    from owa_piggy.config import profile_dir, save_config, save_profiles_conf, set_active_profile

    profile_dir('work').mkdir(parents=True)
    set_active_profile('work')
    save_config({
        'OWA_REFRESH_TOKEN': '1.AQ_fake',
        'OWA_TENANT_ID': 'tid',
    })
    save_profiles_conf({'OWA_DEFAULT_PROFILE': '', 'OWA_PROFILES': []})
    calls = []
    monkeypatch.setattr(
        'owa_piggy.token_flow.exchange_token',
        lambda *_args: calls.append(True) or {'access_token': 'unused'},
    )

    report = status_mod.status_report('work')

    assert report['state'] == 'disabled'
    assert report['hints'] == ['profile is disabled']
    assert calls == []


def test_status_json_keeps_legacy_fallback_when_registry_missing(
    monkeypatch, tmp_config, clean_env, make_jwt
):
    from owa_piggy.config import profile_dir, profiles_conf_path, save_config, set_active_profile

    profile_dir('work').mkdir(parents=True)
    set_active_profile('work')
    save_config({
        'OWA_REFRESH_TOKEN': '1.AQ_fake',
        'OWA_TENANT_ID': 'tid',
    })
    assert not profiles_conf_path().exists()

    calls = []

    def _exchange(*_args):
        calls.append(True)
        return {
            'access_token': make_jwt({
                'exp': 9_999_999_999,
                'aud': 'https://graph.microsoft.com',
                'scp': 'User.Read',
            }),
        }

    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', _exchange)

    report = status_mod.status_report('work')

    assert report['state'] == 'ok'
    assert calls == [True]


def test_status_honors_profile_default_audience(
    monkeypatch, tmp_config, clean_env, make_jwt, capsys
):
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile('work')
    save_config({
        'OWA_REFRESH_TOKEN': '1.AQ_fake',
        'OWA_TENANT_ID': 'tid',
        'OWA_DEFAULT_AUDIENCE': 'teams',
    })

    seen = {}

    def _exchange(_rt, _tid, _cid, scope):
        seen['scope'] = scope
        return {
            'access_token': make_jwt({
                'exp': 9_999_999_999,
                'aud': 'https://api.spaces.skype.com',
                'scp': 'User.Read',
            }),
        }

    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', _exchange)
    monkeypatch.setattr(status_mod, 'launchd_is_scheduled', lambda _alias: False)

    rc = status_mod.do_status('work', verbose=True)

    assert rc == 0
    assert seen['scope'].startswith('https://api.spaces.skype.com/.default ')
    assert 'audience:     teams' in capsys.readouterr().out


def test_status_google_profile_shows_no_hard_cap(
    monkeypatch, tmp_config, clean_env, capsys
):
    """A google-provider profile has no OWA_TENANT_ID and no 24h SPA
    hard-cap - status should render that as normal, not as a broken
    profile, and never suggest `reseed` (which doesn't apply there).

    Uses a realistic OPAQUE access token (Google's real shape, e.g.
    'ya29.a0...') rather than a JWT - a JWT-shaped mock here would hide
    the actual bug (status used to assume every access token is a JWT
    and call decode_jwt_segment on it, which throws on an opaque string
    and used to report the profile as broken even though the live
    exchange succeeded)."""
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile('work')
    save_config({
        'OWA_PROVIDER': 'google',
        'OWA_REFRESH_TOKEN': 'opaque-google-rt',
        'OWA_CLIENT_ID': 'gcid',
        'OWA_CLIENT_SECRET': 'gsecret',
    })
    monkeypatch.setattr(
        'owa_piggy.token_flow.google_exchange_token',
        lambda cid, secret, rt: {'access_token': 'ya29.a0-opaque-not-a-jwt',
                                  'expires_in': 3600},
    )
    monkeypatch.setattr(status_mod, 'launchd_is_scheduled', lambda _alias: False)

    rc = status_mod.do_status('work', verbose=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert 'does not expire (Google refresh tokens are long-lived)' in out
    assert 'run `owa-piggy reseed`' not in out
    assert 'no valid token' not in out


def test_debug_honors_profile_default_audience(
    monkeypatch, tmp_config, clean_env, make_jwt, capsys
):
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile('work')
    save_config({
        'OWA_REFRESH_TOKEN': '1.AQ_fake',
        'OWA_TENANT_ID': 'tid',
        'OWA_DEFAULT_AUDIENCE': 'outlook',
    })

    seen = {}

    def _exchange(_rt, _tid, _cid, scope):
        seen['scope'] = scope
        return {
            'access_token': make_jwt({
                'exp': 9_999_999_999,
                'iat': 9_999_990_000,
                'aud': 'https://outlook.office.com',
                'scp': 'Mail.Read',
            }),
        }

    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout='', stderr='not loaded')

    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', _exchange)
    monkeypatch.setattr(status_mod.subprocess, 'run', _fake_run)
    monkeypatch.setattr(status_mod, 'find_reseed_script', lambda: None)

    rc = status_mod.do_debug('work')

    assert rc == 0
    assert seen['scope'].startswith('https://outlook.office.com/.default ')
    assert 'access token aud: https://outlook.office.com' in capsys.readouterr().out


def test_debug_google_profile_with_opaque_token(
    monkeypatch, tmp_config, clean_env, capsys
):
    """`debug`'s refresh-token-shape display is a second, independent copy
    of the FOCI check in token_flow - it must also skip AAD-only checks for
    a google profile, and must not assume the access token is a JWT."""
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile('work')
    save_config({
        'OWA_PROVIDER': 'google',
        'OWA_REFRESH_TOKEN': '1//0opaque-google-rt',
        'OWA_CLIENT_ID': 'gcid',
        'OWA_CLIENT_SECRET': 'gsecret',
    })
    monkeypatch.setattr(
        'owa_piggy.token_flow.google_exchange_token',
        lambda cid, secret, rt: {'access_token': 'ya29.a0-opaque-not-a-jwt',
                                  'expires_in': 3600},
    )
    monkeypatch.setattr(status_mod.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr=''))
    monkeypatch.setattr(status_mod, 'find_reseed_script', lambda: None)

    rc = status_mod.do_debug('work')

    out = capsys.readouterr().out
    assert rc == 0
    assert 'NOT FOCI' not in out
    assert 'exchange succeeded' in out
    assert 'access token exp' in out
    assert 'n/a (google provider)' in out
