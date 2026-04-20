"""CLI smoke tests.

Exercises argument parsing and dispatch without hitting the network.
exchange_token is monkeypatched to return a synthetic JWT. No real
tokens, no config writes, no HTTP.
"""
import sys

from owa_piggy import cli as cli_mod
from owa_piggy.scopes import KNOWN_SCOPES


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', ['owa-piggy'] + list(argv))
    return cli_mod.main()


def test_help_exits_zero(monkeypatch, capsys):
    rc = _run(monkeypatch, ['--help'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'usage: owa-piggy' in out
    # Spot-check documented flags are all in --help output.
    for flag in ('--json', '--env', '--decode', '--remaining', '--graph',
                 '--teams', '--list-scopes', '--scope', '--save-config',
                 '--setup', '--reseed', '--status', '--debug'):
        assert flag in out, f'{flag} missing from --help'


def test_list_scopes_exits_zero(monkeypatch, capsys):
    rc = _run(monkeypatch, ['--list-scopes'])
    assert rc == 0
    out = capsys.readouterr().out
    for name in KNOWN_SCOPES:
        assert f'--{name}' in out
        assert KNOWN_SCOPES[name][0] in out


def test_short_help_alias(monkeypatch, capsys):
    rc = _run(monkeypatch, ['-h'])
    assert rc == 0
    assert 'usage: owa-piggy' in capsys.readouterr().out


def test_missing_config_exits_nonzero(monkeypatch, capsys, tmp_config, clean_env):
    """No config file + no env = refuses to call AAD, exits non-zero with
    an actionable message."""
    rc = _run(monkeypatch, [])
    assert rc != 0
    err = capsys.readouterr().err
    assert 'OWA_REFRESH_TOKEN' in err
    assert '--save-config' in err


def test_malformed_token_shape_rejected(monkeypatch, capsys, tmp_config,
                                        clean_env):
    """Refresh tokens must start with `1.` or `0.` (FOCI prefix). Anything
    else gets rejected before we hit the network, with a pointer at Edge."""
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': 'plain-chromium-session-token',
                 'OWA_TENANT_ID': '00000000-0000-0000-0000-000000000000'})
    rc = _run(monkeypatch, [])
    assert rc != 0
    err = capsys.readouterr().err
    assert 'FOCI' in err
    assert 'Microsoft Edge' in err


def test_decode_prints_synthetic_jwt(monkeypatch, capsys, tmp_config,
                                     clean_env, make_jwt):
    """Monkeypatch exchange_token to a canned response; --decode should
    emit valid JSON header + payload without touching the network."""
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake-rt-for-tests',
                 'OWA_TENANT_ID': '00000000-0000-0000-0000-000000000000'})

    token = make_jwt({'exp': 9_999_999_999,
                      'aud': 'https://graph.microsoft.com',
                      'scp': 'Mail.Read Mail.Send'})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda rt, tid, cid, scope: {'access_token': token,
                                                     'expires_in': 3600})
    rc = _run(monkeypatch, ['--decode'])
    assert rc == 0
    out = capsys.readouterr().out
    assert '=== Header ===' in out
    assert '=== Payload ===' in out
    assert 'Mail.Read Mail.Send' in out


def test_remaining_prints_minutes(monkeypatch, capsys, tmp_config, clean_env,
                                  make_jwt, frozen_time):
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake',
                 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': int(frozen_time) + 3600})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, ['--remaining'])
    assert rc == 0
    assert capsys.readouterr().out.strip() == '60min'


def test_env_rc_exits_nonzero(monkeypatch, capsys, tmp_config, clean_env):
    rc = _run(monkeypatch, ['--scope'])  # --scope with no value
    assert rc != 0
    assert '--scope requires a value' in capsys.readouterr().err


def test_raw_token_to_stdout(monkeypatch, capsys, tmp_config, clean_env,
                             make_jwt):
    """Default invocation: only the access token on stdout, nothing else."""
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': 9_999_999_999})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, [])
    assert rc == 0
    assert capsys.readouterr().out.strip() == token


def test_env_mode_emits_shell_vars(monkeypatch, capsys, tmp_config, clean_env,
                                   make_jwt):
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': 9_999_999_999})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, ['--env'])
    assert rc == 0
    out = capsys.readouterr().out
    assert f'ACCESS_TOKEN={token}' in out
    assert 'EXPIRES_IN=3600' in out


def test_list_scopes_formatting(monkeypatch, capsys):
    """Every KNOWN_SCOPES entry appears in --list-scopes with its URL."""
    _run(monkeypatch, ['--list-scopes'])
    out = capsys.readouterr().out
    # Match on the formatted flag column (trailing whitespace) to avoid
    # --outlook as a substring of --outlook365.
    for name, (url, _desc) in KNOWN_SCOPES.items():
        assert f'--{name} ' in out or f'--{name}\n' in out
        assert url in out


def test_cache_short_circuits_exchange(monkeypatch, capsys, tmp_config,
                                       clean_env, make_jwt):
    """A cached AT with exp > now + 60s means no call to AAD for the
    plain-token output path. exchange_token must not run."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.scopes import resolve_scope

    scope, _ = resolve_scope([])
    token = make_jwt({'exp': int(_time.time()) + 3600})
    store_token(scope, token, int(_time.time()) + 3600)

    def _boom(*a, **k):
        raise AssertionError('exchange_token must not be called on cache hit')
    monkeypatch.setattr(cli_mod, 'exchange_token', _boom)

    rc = _run(monkeypatch, [])
    assert rc == 0
    assert capsys.readouterr().out.strip() == token


def test_cache_writes_on_exchange(monkeypatch, tmp_config, clean_env,
                                  make_jwt):
    """A successful exchange must populate the cache keyed by scope."""
    import time as _time
    from owa_piggy.cache import get_cached_token
    from owa_piggy.config import save_config
    from owa_piggy.scopes import resolve_scope

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': int(_time.time()) + 3600})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, [])
    assert rc == 0
    scope, _ = resolve_scope([])
    assert get_cached_token(scope) == token


def test_json_bypasses_cache(monkeypatch, capsys, tmp_config, clean_env,
                             make_jwt):
    """--json includes a fresh refresh_token we don't cache, so it must
    always hit AAD even when a valid AT is cached."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.scopes import resolve_scope

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_scope([])
    cached_token = make_jwt({'exp': int(_time.time()) + 3600})
    store_token(scope, cached_token, int(_time.time()) + 3600)

    fresh_token = make_jwt({'exp': int(_time.time()) + 3600,
                            'iss': 'fresh-from-aad'})
    called = {'n': 0}

    def _exchange(*a, **k):
        called['n'] += 1
        return {'access_token': fresh_token, 'refresh_token': '1.AQ_rotated',
                'expires_in': 3600}
    monkeypatch.setattr(cli_mod, 'exchange_token', _exchange)

    rc = _run(monkeypatch, ['--json'])
    assert rc == 0
    assert called['n'] == 1
    out = capsys.readouterr().out
    assert fresh_token in out
    assert '1.AQ_rotated' in out


def test_expired_cache_falls_through_to_exchange(monkeypatch, tmp_config,
                                                 clean_env, make_jwt):
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.scopes import resolve_scope

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_scope([])
    # Cache an already-expired token.
    store_token(scope, 'stale-at', int(_time.time()) - 60)

    fresh = make_jwt({'exp': int(_time.time()) + 3600})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': fresh,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, [])
    assert rc == 0
