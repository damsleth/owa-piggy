"""Tests for status/debug behavior that should mirror token resolution."""
from types import SimpleNamespace

from owa_piggy import status as status_mod


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

    monkeypatch.setattr(status_mod, 'exchange_token', _exchange)
    monkeypatch.setattr(status_mod, 'launchd_is_installed', lambda _alias: False)

    rc = status_mod.do_status('work')

    assert rc == 0
    assert seen['scope'].startswith('https://api.spaces.skype.com/.default ')
    assert 'audience:     teams' in capsys.readouterr().out


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

    monkeypatch.setattr(status_mod, 'exchange_token', _exchange)
    monkeypatch.setattr(status_mod.subprocess, 'run', _fake_run)
    monkeypatch.setattr(status_mod, 'find_reseed_script', lambda: None)

    rc = status_mod.do_debug('work')

    assert rc == 0
    assert seen['scope'].startswith('https://outlook.office.com/.default ')
    assert 'access token aud: https://outlook.office.com' in capsys.readouterr().out
