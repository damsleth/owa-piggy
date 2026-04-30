"""CLI smoke tests.

Exercises subcommand parsing and dispatch without hitting the network.
exchange_token is monkeypatched to return a synthetic JWT. No real
tokens, no config writes, no HTTP.
"""
import io
import sys

import pytest

from owa_piggy import cli as cli_mod
from owa_piggy.scopes import KNOWN_AUDIENCES


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', ['owa-piggy'] + list(argv))
    return cli_mod.main()


def test_help_exits_zero(monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['--help'])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert 'usage: owa-piggy' in out
    # Spot-check documented subcommands.
    for cmd in ('token', 'status', 'debug', 'setup', 'reseed', 'decode',
                'remaining', 'audiences', 'profiles'):
        assert cmd in out, f'{cmd} missing from --help'


def test_version_prints_version(monkeypatch, capsys):
    """--version prints `owa-piggy X.Y.Z` and exits 0, no subcommand needed."""
    from owa_piggy import __version__
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['--version'])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert f'owa-piggy {__version__}' in out


def test_audiences_lists_all_known(monkeypatch, capsys):
    rc = _run(monkeypatch, ['audiences'])
    assert rc == 0
    out = capsys.readouterr().out
    for name, (url, _desc) in KNOWN_AUDIENCES.items():
        assert name in out
        assert url in out


def test_short_help_alias(monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['-h'])
    assert excinfo.value.code == 0
    assert 'usage: owa-piggy' in capsys.readouterr().out


def test_missing_config_exits_nonzero(monkeypatch, capsys, tmp_config, clean_env):
    """No config file + no env = refuses to call AAD, exits non-zero with
    an actionable message."""
    rc = _run(monkeypatch, [])
    assert rc != 0
    err = capsys.readouterr().err
    assert 'OWA_REFRESH_TOKEN' in err
    assert 'owa-piggy setup' in err


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
    """Monkeypatch exchange_token to a canned response; `decode` should
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
    rc = _run(monkeypatch, ['decode'])
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
    rc = _run(monkeypatch, ['remaining'])
    assert rc == 0
    assert capsys.readouterr().out.strip() == '60min'


def test_scope_without_value_errors(monkeypatch, capsys, tmp_config,
                                     clean_env):
    """--scope with no value is an argparse error and exits non-zero."""
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['--scope'])
    assert excinfo.value.code != 0


def test_debug_unknown_audience_errors(monkeypatch, capsys, tmp_config,
                                        clean_env):
    """debug with an unknown --audience must exit non-zero (argparse's
    `choices=` check rejects it before we probe AAD)."""
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['debug', '--audience', 'nonesuch'])
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert 'nonesuch' in err


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
    rc = _run(monkeypatch, ['token', '--env'])
    assert rc == 0
    out = capsys.readouterr().out
    assert f'ACCESS_TOKEN={token}' in out
    assert 'EXPIRES_IN=3600' in out


def test_bare_token_env_works(monkeypatch, capsys, tmp_config, clean_env,
                              make_jwt):
    """--env works on the implicit token path too (no `token` prefix)."""
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': 9_999_999_999})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, ['--env'])
    assert rc == 0
    assert f'ACCESS_TOKEN={token}' in capsys.readouterr().out


def test_audiences_formatting(monkeypatch, capsys):
    """Every KNOWN_AUDIENCES entry appears in the `audiences` output with
    its URL."""
    _run(monkeypatch, ['audiences'])
    out = capsys.readouterr().out
    for name, (url, _desc) in KNOWN_AUDIENCES.items():
        assert name in out
        assert url in out


def test_cache_short_circuits_exchange(monkeypatch, capsys, tmp_config,
                                       clean_env, make_jwt):
    """A cached AT with exp > now + 60s means no call to AAD for the
    plain-token output path. exchange_token must not run."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_audience()
    token = make_jwt({'exp': int(_time.time()) + 3600})
    store_token('tid', CLIENT_ID, scope, token, int(_time.time()) + 3600)

    def _boom(*a, **k):
        raise AssertionError('exchange_token must not be called on cache hit')
    monkeypatch.setattr(cli_mod, 'exchange_token', _boom)

    rc = _run(monkeypatch, [])
    assert rc == 0
    assert capsys.readouterr().out.strip() == token


def test_cache_writes_on_exchange(monkeypatch, tmp_config, clean_env,
                                  make_jwt):
    """A successful exchange must populate the cache keyed by
    (tenant, client, scope)."""
    import time as _time
    from owa_piggy.cache import get_cached_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': int(_time.time()) + 3600})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, [])
    assert rc == 0
    scope, _ = resolve_audience()
    assert get_cached_token('tid', CLIENT_ID, scope) == token


def test_cache_does_not_cross_tenant_boundary(monkeypatch, capsys, tmp_config,
                                              clean_env, make_jwt):
    """Regression anchor for QA finding #1: a cached AT for tenant A must
    NOT be served when the active config has tenant B."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake',
                 'OWA_TENANT_ID': 'tenant-B'})
    scope, _ = resolve_audience()
    # Cache an AT under tenant A while the active config is tenant B.
    store_token('tenant-A', CLIENT_ID, scope, 'at-from-tenant-A',
                int(_time.time()) + 3600)

    fresh = make_jwt({'exp': int(_time.time()) + 3600, 'tid': 'tenant-B'})
    called = {'n': 0}

    def _exchange(*a, **k):
        called['n'] += 1
        return {'access_token': fresh, 'expires_in': 3600}
    monkeypatch.setattr(cli_mod, 'exchange_token', _exchange)

    rc = _run(monkeypatch, [])
    assert rc == 0
    assert called['n'] == 1
    out = capsys.readouterr().out.strip()
    assert out == fresh
    assert 'at-from-tenant-A' not in out


def test_setup_clears_cache(monkeypatch, tmp_config, clean_env, make_jwt):
    """Running `setup` wipes any pre-existing cache so entries from a
    previous identity can't leak past the re-setup."""
    import time as _time
    from owa_piggy.cache import load_cache, store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_audience()
    store_token('tid', CLIENT_ID, scope, 'stale',
                int(_time.time()) + 3600)
    assert load_cache() != {}

    # Patch interactive_setup to a no-op that succeeds so the CLI doesn't
    # try to read stdin. The cache must be gone by the time setup returns.
    seen_cache_during_setup = {}

    def _fake_setup(cfg, alias='default', *, email=None):
        seen_cache_during_setup['snapshot'] = load_cache()
        cfg['OWA_REFRESH_TOKEN'] = '1.AQ_fake'
        cfg['OWA_TENANT_ID'] = 'tid'
        return True

    monkeypatch.setattr(cli_mod, 'interactive_setup', _fake_setup)
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token':
                                         make_jwt({'exp': int(_time.time()) + 3600}),
                                         'expires_in': 3600})

    rc = _run(monkeypatch, ['setup'])
    assert rc == 0
    assert seen_cache_during_setup['snapshot'] == {}


def test_reseed_clears_cache(monkeypatch, tmp_config, clean_env):
    """`reseed` wipes the cache before shelling out so any AT minted for
    the pre-reseed RT can't be served afterwards."""
    import time as _time
    from owa_piggy.cache import load_cache, store_token
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    scope, _ = resolve_audience()
    store_token('tid', CLIENT_ID, scope, 'pre-reseed-at',
                int(_time.time()) + 3600)
    assert load_cache() != {}

    observed = {}

    def _fake_reseed(alias):
        observed['cache'] = load_cache()
        return 0

    monkeypatch.setattr(cli_mod, 'do_reseed', _fake_reseed)
    rc = _run(monkeypatch, ['reseed'])
    assert rc == 0
    assert observed['cache'] == {}


def test_json_bypasses_cache(monkeypatch, capsys, tmp_config, clean_env,
                             make_jwt):
    """--json includes a fresh refresh_token we don't cache, so it must
    always hit AAD even when a valid AT is cached."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_audience()
    cached_token = make_jwt({'exp': int(_time.time()) + 3600})
    store_token('tid', CLIENT_ID, scope, cached_token,
                int(_time.time()) + 3600)

    fresh_token = make_jwt({'exp': int(_time.time()) + 3600,
                            'iss': 'fresh-from-aad'})
    called = {'n': 0}

    def _exchange(*a, **k):
        called['n'] += 1
        return {'access_token': fresh_token, 'refresh_token': '1.AQ_rotated',
                'expires_in': 3600}
    monkeypatch.setattr(cli_mod, 'exchange_token', _exchange)

    rc = _run(monkeypatch, ['token', '--json'])
    assert rc == 0
    assert called['n'] == 1
    out = capsys.readouterr().out
    assert fresh_token in out
    assert '1.AQ_rotated' in out


def test_expired_cache_falls_through_to_exchange(monkeypatch, capsys,
                                                 tmp_config, clean_env,
                                                 make_jwt):
    """Expired cache entry must not short-circuit - exchange_token runs
    and the fresh token is what lands on stdout, not the stale cached one."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_audience()
    store_token('tid', CLIENT_ID, scope, 'stale-at', int(_time.time()) - 60)

    fresh = make_jwt({'exp': int(_time.time()) + 3600})
    called = {'n': 0}

    def _exchange(*a, **k):
        called['n'] += 1
        return {'access_token': fresh, 'expires_in': 3600}
    monkeypatch.setattr(cli_mod, 'exchange_token', _exchange)

    rc = _run(monkeypatch, [])
    assert rc == 0
    assert called['n'] == 1
    out = capsys.readouterr().out
    assert out.strip() == fresh
    assert 'stale-at' not in out


# --- Cache bypass paths -------------------------------------------------
# status, debug, and reseed exist to probe AAD or shell out; they must
# NEVER serve from the cache even when a valid AT is present.


def test_status_bypasses_cache(monkeypatch, tmp_config, clean_env, make_jwt):
    """status prefers a live AAD probe over a cached AT. With no explicit
    --profile, dispatch lands in do_status_all rather than do_status."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    scope, _ = resolve_audience()
    store_token('tid', CLIENT_ID, scope,
                make_jwt({'exp': int(_time.time()) + 3600}),
                int(_time.time()) + 3600)

    called = {'n': 0}

    def _fake_status_all(audience=None, scope=None):
        called['n'] += 1
        return 0
    monkeypatch.setattr(cli_mod, 'do_status_all', _fake_status_all)

    rc = _run(monkeypatch, ['status'])
    assert rc == 0
    assert called['n'] == 1


def test_debug_bypasses_cache(monkeypatch, tmp_config, clean_env, make_jwt):
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    scope, _ = resolve_audience()
    store_token('tid', CLIENT_ID, scope,
                make_jwt({'exp': int(_time.time()) + 3600}),
                int(_time.time()) + 3600)

    called = {'n': 0}

    def _fake_debug(alias, audience=None, scope=None):
        called['n'] += 1
        return 0
    monkeypatch.setattr(cli_mod, 'do_debug', _fake_debug)

    rc = _run(monkeypatch, ['debug'])
    assert rc == 0
    assert called['n'] == 1


def test_reseed_bypasses_cache(monkeypatch, tmp_config, clean_env, make_jwt):
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    scope, _ = resolve_audience()
    store_token('tid', CLIENT_ID, scope,
                make_jwt({'exp': int(_time.time()) + 3600}),
                int(_time.time()) + 3600)

    called = {'n': 0}

    def _fake_reseed(alias):
        called['n'] += 1
        return 0
    monkeypatch.setattr(cli_mod, 'do_reseed', _fake_reseed)

    rc = _run(monkeypatch, ['reseed'])
    assert rc == 0
    assert called['n'] == 1


# --- Cache-hit output branches -----------------------------------------


def test_cache_hit_env_mode(monkeypatch, capsys, tmp_config, clean_env,
                            make_jwt):
    """--env on a cache hit computes EXPIRES_IN from (exp - now), since
    the original `expires_in` isn't stored in the cache."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_audience()
    future = int(_time.time()) + 1800
    token = make_jwt({'exp': future})
    store_token('tid', CLIENT_ID, scope, token, future)

    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: pytest.fail('cache should serve --env'))
    rc = _run(monkeypatch, ['token', '--env'])
    assert rc == 0
    out = capsys.readouterr().out
    assert f'ACCESS_TOKEN={token}' in out
    for line in out.splitlines():
        if line.startswith('EXPIRES_IN='):
            remaining = int(line.split('=', 1)[1])
            assert 1700 <= remaining <= 1800
            break
    else:
        pytest.fail('EXPIRES_IN line missing')


def test_cache_hit_decode_mode(monkeypatch, capsys, tmp_config, clean_env,
                               make_jwt):
    """decode on a cache hit decodes the cached AT, doesn't re-mint."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_audience()
    token = make_jwt({'exp': int(_time.time()) + 3600,
                      'aud': 'https://graph.microsoft.com',
                      'scp': 'Mail.Read'})
    store_token('tid', CLIENT_ID, scope, token, int(_time.time()) + 3600)

    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: pytest.fail('cache should serve decode'))
    rc = _run(monkeypatch, ['decode'])
    assert rc == 0
    out = capsys.readouterr().out
    assert '=== Header ===' in out
    assert '=== Payload ===' in out
    assert 'Mail.Read' in out


def test_unknown_subcommand_errors(monkeypatch, capsys, tmp_config, clean_env):
    """Unknown subcommand is rejected by argparse, exits non-zero."""
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['nonesuch'])
    assert excinfo.value.code != 0


def test_unknown_flag_is_rejected(monkeypatch, capsys, tmp_config, clean_env):
    """An unrecognised --flag must exit non-zero with argparse's standard
    error output."""
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['--somewrongparam'])
    assert excinfo.value.code != 0


def test_unknown_flag_mixed_with_known_is_rejected(monkeypatch, capsys,
                                                    tmp_config, clean_env):
    """Typo next to a valid flag must not be ignored just because the
    valid flag would succeed on its own."""
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['--audience', 'graph', '--typo'])
    assert excinfo.value.code != 0


def test_scope_value_not_treated_as_unknown_flag(monkeypatch, capsys,
                                                  tmp_config, clean_env,
                                                  make_jwt):
    """`--scope <url>` must not trigger unknown-flag rejection on the URL
    value."""
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': 9_999_999_999})
    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, ['--scope', 'https://graph.microsoft.com/.default'])
    assert rc == 0


def test_profiles_lists_registered(monkeypatch, capsys, tmp_config, clean_env):
    """`profiles` (bare) lists registered profiles. Non-TTY stdin falls
    through to the plain list so the test doesn't need terminal raw mode."""
    from owa_piggy.config import ensure_profile_registered, profile_dir
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    profile_dir('personal').mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    rc = _run(monkeypatch, ['profiles'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'work' in out
    assert 'personal' in out
    assert '*' in out  # default marker


def test_audiences_with_multiple_profiles_no_default(monkeypatch, capsys,
                                                      tmp_config, clean_env):
    """`audiences` is purely informational and must work on installs
    with multiple profile directories and no default set - it doesn't
    resolve a profile at all."""
    from owa_piggy.config import profile_dir
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    profile_dir('personal').mkdir(parents=True, exist_ok=True)
    # profiles.conf intentionally not written: no default pointer.
    rc = _run(monkeypatch, ['audiences'])
    assert rc == 0
    out = capsys.readouterr().out
    for name in KNOWN_AUDIENCES:
        assert name in out


def test_status_profile_label_on_stderr(monkeypatch, capsys, tmp_config,
                                        clean_env):
    """Single-profile status must keep its 'no valid token' stdout
    contract. The `profile: <alias>` header goes to stderr so scripts
    parsing stdout are not regressed."""
    rc = _run(monkeypatch, ['status', '--profile', 'default'])
    assert rc != 0
    cap = capsys.readouterr()
    assert cap.out.strip() == 'no valid token'
    assert 'profile:' in cap.err
    assert 'default' in cap.err


def test_status_without_profile_iterates_all(monkeypatch, capsys, tmp_config,
                                              clean_env):
    """`status` with no --profile prints a labeled block per configured
    profile. The `profile: <alias>` header moves to stdout so the
    output is self-describing when scanning several profiles."""
    from owa_piggy.config import ensure_profile_registered, profile_dir
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    profile_dir('personal').mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    rc = _run(monkeypatch, ['status'])
    assert rc != 0
    out = capsys.readouterr().out
    assert 'profile:      work' in out
    assert 'profile:      personal' in out


def test_status_no_profiles_configured(monkeypatch, capsys, tmp_config,
                                        clean_env):
    """`status` with no profiles configured anywhere must produce a
    helpful pointer, not a traceback."""
    rc = _run(monkeypatch, ['status'])
    assert rc != 0
    err = capsys.readouterr().err
    assert 'no profiles configured' in err


def test_cli_rejects_traversal_profile(monkeypatch, capsys, tmp_config,
                                        clean_env):
    """--profile ../../outside must be rejected before any path is derived."""
    rc = _run(monkeypatch, ['setup', '--profile', '../../outside'])
    assert rc != 0
    assert 'invalid profile alias' in capsys.readouterr().err


def test_cli_rejects_nested_profile(monkeypatch, capsys, tmp_config,
                                    clean_env):
    rc = _run(monkeypatch, ['setup', '--profile', 'work/sub'])
    assert rc != 0
    assert 'invalid profile alias' in capsys.readouterr().err


def test_profiles_set_default_rejects_bad_alias(monkeypatch, capsys,
                                                tmp_config, clean_env):
    rc = _run(monkeypatch, ['profiles', 'set-default', '../escape'])
    assert rc != 0
    assert 'invalid profile alias' in capsys.readouterr().err


def test_profiles_delete_rejects_bad_alias(monkeypatch, capsys, tmp_config,
                                           clean_env):
    rc = _run(monkeypatch, ['profiles', 'delete', '../escape'])
    assert rc != 0
    assert 'invalid profile alias' in capsys.readouterr().err


def test_interactive_profile_picker_ctrl_c_restores_terminal(monkeypatch):
    import termios
    import tty

    class FakeIn:
        def fileno(self):
            return 0

        def read(self, _n):
            return '\x03'

    restored = []
    monkeypatch.setattr(sys, 'stdin', FakeIn())
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    monkeypatch.setattr(termios, 'tcgetattr', lambda fd: ['old-state'])
    monkeypatch.setattr(tty, 'setraw', lambda fd: None)
    monkeypatch.setattr(
        termios,
        'tcsetattr',
        lambda fd, when, state: restored.append((fd, when, state)),
    )

    with pytest.raises(KeyboardInterrupt):
        cli_mod._interactive_profile_picker(['work', 'personal'], 'work')

    assert restored == [(0, termios.TCSADRAIN, ['old-state'])]


def test_profiles_delete_preserves_dir_if_registry_update_fails(
    monkeypatch, capsys, tmp_config, clean_env
):
    from owa_piggy.config import ensure_profile_registered, profile_dir

    target = profile_dir('work')
    target.mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')

    monkeypatch.setattr(
        cli_mod,
        'unregister_profile',
        lambda alias: (_ for _ in ()).throw(OSError('disk full')),
    )
    monkeypatch.setattr(
        cli_mod.shutil,
        'rmtree',
        lambda path: pytest.fail('rmtree must not run when registry update fails'),
    )

    rc = cli_mod._do_profiles_delete('work', force=True)
    assert rc == 1
    assert target.exists()
    assert 'failed to update profile registry' in capsys.readouterr().err


def test_profiles_delete_unregistered_dir_left_on_disk_if_rmtree_fails(
    monkeypatch, capsys, tmp_config, clean_env
):
    from owa_piggy.config import ensure_profile_registered, load_profiles_conf, profile_dir

    target = profile_dir('work')
    target.mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')

    monkeypatch.setattr(
        cli_mod.shutil,
        'rmtree',
        lambda path: (_ for _ in ()).throw(OSError('busy')),
    )

    rc = cli_mod._do_profiles_delete('work', force=True)
    assert rc == 1
    assert target.exists()
    assert 'work' not in load_profiles_conf()['OWA_PROFILES']
    assert 'was unregistered but failed to remove' in capsys.readouterr().err


def test_cache_hit_remaining_mode(monkeypatch, capsys, tmp_config, clean_env,
                                  make_jwt):
    """remaining on a cache hit reports minutes on the cached AT."""
    import time as _time
    from owa_piggy.cache import store_token
    from owa_piggy.config import save_config
    from owa_piggy.oauth import CLIENT_ID
    from owa_piggy.scopes import resolve_audience

    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    scope, _ = resolve_audience()
    future = int(_time.time()) + 3600
    token = make_jwt({'exp': future})
    store_token('tid', CLIENT_ID, scope, token, future)

    monkeypatch.setattr(cli_mod, 'exchange_token',
                        lambda *a, **k: pytest.fail('cache should serve remaining'))
    rc = _run(monkeypatch, ['remaining'])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith('min')
    minutes = int(out[:-3])
    assert 58 <= minutes <= 60
