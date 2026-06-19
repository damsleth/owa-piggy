"""CLI smoke tests.

Exercises subcommand parsing and dispatch without hitting the network.
exchange_token is monkeypatched to return a synthetic JWT. No real
tokens, no config writes, no HTTP.
"""
import io
import json
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
                'remaining', 'edge', 'tui', 'audiences', 'profiles'):
        assert cmd in out, f'{cmd} missing from --help'


def test_version_prints_version(monkeypatch, capsys):
    """--version prints `owa-piggy X.Y.Z` and exits 0, no subcommand needed."""
    from owa_piggy import __version__
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['--version'])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert f'owa-piggy {__version__}' in out


def test_short_version_flag(monkeypatch, capsys):
    """`-v` is a short alias for `--version` at the top level."""
    from owa_piggy import __version__
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ['-v'])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert f'owa-piggy {__version__}' in out


def test_version_json(monkeypatch, capsys):
    from owa_piggy import __version__
    rc = _run(monkeypatch, ['version', '--json'])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        'tool': 'owa-piggy',
        'version': __version__,
    }


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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    rc = _run(monkeypatch, [])
    assert rc == 0
    assert capsys.readouterr().out.strip() == token


def test_agent_token_adds_json_default(monkeypatch, capsys, tmp_config,
                                       clean_env, make_jwt):
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': 9_999_999_999})
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
                        lambda *a, **k: {'access_token': token,
                                         'expires_in': 3600})
    assert _run(monkeypatch, ['--agent', 'token']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['_owa']['command'] == 'token'
    assert payload['data']['access_token'] == token


def test_env_mode_emits_shell_vars(monkeypatch, capsys, tmp_config, clean_env,
                                   make_jwt):
    from owa_piggy.config import save_config
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    token = make_jwt({'exp': 9_999_999_999})
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', _boom)

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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', _exchange)

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

    def _fake_setup(cfg, alias='default', *, email=None, **kwargs):
        seen_cache_during_setup['snapshot'] = load_cache()
        cfg['OWA_REFRESH_TOKEN'] = '1.AQ_fake'
        cfg['OWA_TENANT_ID'] = 'tid'
        return True

    from owa_piggy import profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, 'interactive_setup', _fake_setup)
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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


def test_edge_launches_profile_sidecar(monkeypatch, capsys, tmp_config,
                                       clean_env):
    """`edge --profile work` resolves the profile, hands its sidecar dir to
    capture.open_edge, and prints the next-step hint. open_edge is stubbed
    so the test never actually spawns a browser."""
    import owa_piggy.capture as capture_mod
    from owa_piggy.config import (
        ensure_profile_registered,
        profile_dir,
        profile_edge_dir,
    )
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')

    seen = {}

    def _fake_open_edge(alias, *, url=None):
        seen['alias'] = alias
        return ('proc-sentinel', profile_edge_dir(alias))

    monkeypatch.setattr(capture_mod, 'open_edge', _fake_open_edge)
    rc = _run(monkeypatch, ['edge', '--profile', 'work'])
    assert rc == 0
    assert seen['alias'] == 'work'
    out = capsys.readouterr().out
    assert 'launched Edge' in out
    assert 'reseed --profile work' in out


def test_edge_reports_missing_edge_binary(monkeypatch, capsys, tmp_config,
                                          clean_env):
    """When open_edge can't find Edge it raises RuntimeError; the command
    surfaces it as a clean ERROR line and exits non-zero rather than letting
    the traceback escape."""
    import owa_piggy.capture as capture_mod
    from owa_piggy.config import ensure_profile_registered, profile_dir
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')

    def _boom(alias, *, url=None):
        raise RuntimeError('Microsoft Edge not found.')

    monkeypatch.setattr(capture_mod, 'open_edge', _boom)
    rc = _run(monkeypatch, ['edge', '--profile', 'work'])
    assert rc == 1
    assert 'Microsoft Edge not found' in capsys.readouterr().err


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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', _exchange)

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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', _exchange)

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

    def _fake_status_all(audience=None, scope=None, sharepoint_tenant=None, verbose=False):
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

    def _fake_debug(alias, audience=None, scope=None, sharepoint_tenant=None):
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

    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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

    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
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


def test_profiles_json_lists_registered(monkeypatch, capsys, tmp_config, clean_env):
    from owa_piggy.config import ensure_profile_registered, profile_dir, save_config, set_active_profile
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    profile_dir('personal').mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    set_active_profile('work')
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})

    rc = _run(monkeypatch, ['profiles', '--json'])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['default'] == 'work'
    rows = {row['alias']: row for row in payload['profiles']}
    assert rows['work']['default'] is True
    assert rows['work']['has_config'] is True
    assert rows['personal']['has_config'] is False


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


def test_status_json_no_profiles(monkeypatch, capsys, tmp_config, clean_env):
    rc = _run(monkeypatch, ['status', '--json'])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {'profiles': [], 'summary': {'ok': 0, 'warn': 0, 'fail': 0}}


def test_status_json_single_profile_redacts_tokens(
    monkeypatch, capsys, tmp_config, clean_env, make_jwt
):
    from owa_piggy.config import save_config

    save_config({
        'OWA_REFRESH_TOKEN': '1.AQ_fake',
        'OWA_TENANT_ID': 'tid',
        'OWA_RT_ISSUED_AT': '2026-05-08T08:00:00Z',
    })
    token = make_jwt({
        'aud': 'https://graph.microsoft.com',
        'exp': 9_999_999_999,
        'scp': 'User.Read Mail.Read',
    })
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', lambda *a, **k: {
        'access_token': token,
        'expires_in': 3600,
        'refresh_token': '1.AQ_rotated',
    })
    monkeypatch.setattr('owa_piggy.token_flow.exchange_token', lambda *a, **k: {
        'access_token': token,
        'expires_in': 3600,
        'refresh_token': '1.AQ_rotated',
    })

    rc = _run(monkeypatch, ['status', '--profile', 'default', '--json'])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload['profile'] == 'default'
    assert payload['state'] == 'ok'
    assert payload['access_token']['present'] is True
    assert payload['refresh_token']['present'] is True
    assert '1.AQ' not in out
    assert token not in out


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


class _TTYStringIO(io.StringIO):
    """StringIO that claims to be a TTY, so run_dashboard's isatty guard
    doesn't divert to the plain-text fallback under test capture."""

    def isatty(self):
        return True


def _stub_dashboard_probe(monkeypatch, profiles):
    """Stub the network probe run_dashboard fires on entry/refresh so the
    dashboard's key loop can be exercised offline (all profiles 'fresh')."""
    from owa_piggy import status as status_mod
    monkeypatch.setattr(
        status_mod, 'status_all_report',
        lambda **kw: {
            'profiles': [
                {'profile': a, 'state': 'ok',
                 'access_token': {'present': True, 'minutes_remaining': 60},
                 'hints': []}
                for a in profiles
            ],
            'summary': {'ok': len(profiles), 'warn': 0, 'fail': 0},
        })


def test_interactive_profile_dashboard_ctrl_c_restores_terminal(monkeypatch):
    import termios
    import tty

    class FakeIn:
        def fileno(self):
            return 0

        def isatty(self):
            return True

        def read(self, _n):
            return '\x03'

    from owa_piggy import profile_tui

    restored = []
    monkeypatch.setattr(sys, 'stdin', FakeIn())
    monkeypatch.setattr(sys, 'stdout', _TTYStringIO())
    # The dashboard reads profile state from disk on each frame; stub the
    # lookups so the call doesn't depend on a real ~/.config tree.
    monkeypatch.setattr(profile_tui, 'list_profiles', lambda: ['work', 'personal'])
    monkeypatch.setattr(
        profile_tui,
        'load_profiles_conf',
        lambda: {'OWA_DEFAULT_PROFILE': 'work', 'OWA_PROFILES': ['work', 'personal']},
    )
    monkeypatch.setattr(profile_tui, 'launchd_is_scheduled', lambda alias: False)
    _stub_dashboard_probe(monkeypatch, ['work', 'personal'])
    monkeypatch.setattr(termios, 'tcgetattr', lambda fd: ['old-state'])
    monkeypatch.setattr(tty, 'setraw', lambda fd: None)
    monkeypatch.setattr(
        termios,
        'tcsetattr',
        lambda fd, when, state: restored.append((fd, when, state)),
    )

    with pytest.raises(KeyboardInterrupt):
        profile_tui.run_dashboard()

    assert restored == [(0, termios.TCSADRAIN, ['old-state'])]


class _ScriptedStdin:
    """Feed the picker a fixed key sequence then EOF (treated as quit).

    Reading past the end returns 'q' so the loop exits cleanly even if
    the test under-counts how many reads the picker will do (escape
    sequence parsing, redraws, etc.). Tests assert the side-effects
    they care about; loop exit is not under test.
    """

    def __init__(self, keys):
        self._keys = list(keys)

    def fileno(self):
        return 0

    def isatty(self):
        return True

    def read(self, _n):
        if self._keys:
            return self._keys.pop(0)
        return 'q'


def _stub_picker_environment(monkeypatch, *, profiles, default,
                             enabled=None, keys=()):
    """Wire up the termios/stdin/state stubs the dashboard needs to run
    detached from a real terminal and a real config tree, including a
    stubbed token probe so the key loop never touches the network."""
    import termios
    import tty
    from owa_piggy import profile_tui as tui

    enabled = list(enabled if enabled is not None else profiles)
    monkeypatch.setattr(sys, 'stdin', _ScriptedStdin(list(keys)))
    monkeypatch.setattr(sys, 'stdout', _TTYStringIO())
    monkeypatch.setattr(tui, 'list_profiles', lambda: list(profiles))
    monkeypatch.setattr(
        tui, 'load_profiles_conf',
        lambda: {'OWA_DEFAULT_PROFILE': default,
                 'OWA_PROFILES': list(enabled)},
    )
    monkeypatch.setattr(tui, 'launchd_is_scheduled', lambda alias: False)
    _stub_dashboard_probe(monkeypatch, profiles)
    monkeypatch.setattr(termios, 'tcgetattr', lambda fd: ['old-state'])
    monkeypatch.setattr(tty, 'setraw', lambda fd: None)
    monkeypatch.setattr(termios, 'tcsetattr', lambda *a, **kw: None)


def test_dashboard_space_toggles_profile(monkeypatch):
    """Pressing space on a highlighted profile calls disable_profile when
    it is currently enabled, then enable_profile when it is not - the
    registry mutation goes through the shared profiles.* helpers, never
    directly through the picker."""
    from owa_piggy import profile_tui as tui

    _stub_picker_environment(
        monkeypatch,
        profiles=['work', 'personal'],
        default='work',
        enabled=['work', 'personal'],
        keys=[' ', 'q'],
    )
    calls = []
    monkeypatch.setattr(tui, 'disable_profile',
                        lambda alias: calls.append(('disable', alias)) or (True, ''))
    monkeypatch.setattr(tui, 'enable_profile',
                        lambda alias: calls.append(('enable', alias)) or (True, ''))

    rc = tui.run_dashboard()
    assert rc == 0
    # Cursor starts on the default ('work'). Space disables it.
    assert calls == [('disable', 'work')]


def test_dashboard_enter_sets_default(monkeypatch):
    """Pressing enter on a non-default profile calls set_default_profile.
    Pressing enter on the already-default is a no-op (a status-line hint,
    not a registry write)."""
    from owa_piggy import profile_tui as tui

    _stub_picker_environment(
        monkeypatch,
        profiles=['work', 'personal'],
        default='work',
        keys=['j', '\r', 'q'],
    )
    calls = []
    monkeypatch.setattr(tui, 'set_default_profile',
                        lambda alias: calls.append(alias) or (True, ''))

    rc = tui.run_dashboard()
    assert rc == 0
    assert calls == ['personal']


def test_dashboard_delete_cancel_does_not_mutate(monkeypatch):
    """Answering 'n' at the delete confirmation must not call
    delete_profile. The picker prints the about-to-delete summary, the
    user backs out, and the registry is left alone."""
    from owa_piggy import profile_tui as tui

    _stub_picker_environment(
        monkeypatch,
        profiles=['work', 'personal'],
        default='work',
        keys=['d', 'q'],
    )
    monkeypatch.setattr(tui, 'profile_dir', lambda alias: f'/tmp/fake/{alias}')
    monkeypatch.setattr('builtins.input', lambda prompt='': 'n')
    monkeypatch.setattr(
        tui, 'delete_profile',
        lambda *a, **kw: pytest.fail('delete_profile must not run on confirm=no'),
    )

    rc = tui.run_dashboard()
    assert rc == 0


def test_dashboard_shift_r_reseeds_all(monkeypatch):
    """`R` triggers do_reseed_all - the same code path as
    `owa-piggy reseed --all` from the shell."""
    from owa_piggy import profile_tui as tui

    _stub_picker_environment(
        monkeypatch,
        profiles=['work'],
        default='work',
        keys=['R', 'q'],
    )
    calls = []
    monkeypatch.setattr(tui, 'do_reseed_all', lambda: calls.append('all') or 0)
    monkeypatch.setattr('builtins.input', lambda prompt='': '')

    rc = tui.run_dashboard()
    assert rc == 0
    assert calls == ['all']


def test_dashboard_e_opens_edge(monkeypatch):
    """`e` calls capture.open_edge for the highlighted profile and leaves
    the picker running (open_edge is detached - no cooked-mode drop)."""
    from owa_piggy import capture as capture_mod
    from owa_piggy import profile_tui as tui

    _stub_picker_environment(
        monkeypatch,
        profiles=['work', 'personal'],
        default='work',
        keys=['e', 'q'],
    )
    calls = []
    monkeypatch.setattr(
        capture_mod, 'open_edge',
        lambda alias, **kw: calls.append(alias) or ('proc', f'/tmp/{alias}'))

    rc = tui.run_dashboard()
    assert rc == 0
    # Cursor starts on the default ('work').
    assert calls == ['work']


def test_profiles_delete_preserves_dir_if_registry_update_fails(
    monkeypatch, capsys, tmp_config, clean_env
):
    from owa_piggy.config import ensure_profile_registered, profile_dir
    from owa_piggy import profiles as profiles_mod

    target = profile_dir('work')
    target.mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')

    monkeypatch.setattr(
        profiles_mod,
        'unregister_profile',
        lambda alias: (_ for _ in ()).throw(OSError('disk full')),
    )
    monkeypatch.setattr(
        profiles_mod.shutil,
        'rmtree',
        lambda path: pytest.fail('rmtree must not run when registry update fails'),
    )

    rc = cli_mod._do_profiles_delete('work', force=True, yes=True)
    assert rc == 1
    assert target.exists()
    assert 'profile registry update failed' in capsys.readouterr().err


def test_profiles_delete_unregistered_dir_left_on_disk_if_rmtree_fails(
    monkeypatch, capsys, tmp_config, clean_env
):
    from owa_piggy.config import ensure_profile_registered, load_profiles_conf, profile_dir
    from owa_piggy import profiles as profiles_mod

    target = profile_dir('work')
    target.mkdir(parents=True, exist_ok=True)
    ensure_profile_registered('work')

    monkeypatch.setattr(
        profiles_mod.shutil,
        'rmtree',
        lambda path: (_ for _ in ()).throw(OSError('busy')),
    )

    rc = cli_mod._do_profiles_delete('work', force=True, yes=True)
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

    monkeypatch.setattr('owa_piggy.token_flow.exchange_token',
                        lambda *a, **k: pytest.fail('cache should serve remaining'))
    rc = _run(monkeypatch, ['remaining'])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith('min')
    minutes = int(out[:-3])
    assert 58 <= minutes <= 60


# --- reseed selectors (--all / --scheduled / --profile mutual exclusion) ---


def test_reseed_all_and_scheduled_mutually_exclusive(monkeypatch, capsys,
                                                     tmp_config, clean_env):
    rc = _run(monkeypatch, ['reseed', '--all', '--scheduled'])
    assert rc != 0
    assert '--all and --scheduled are mutually exclusive' in capsys.readouterr().err


def test_reseed_scheduled_and_profile_mutually_exclusive(monkeypatch, capsys,
                                                         tmp_config, clean_env):
    rc = _run(monkeypatch, ['reseed', '--scheduled', '--profile', 'work'])
    assert rc != 0
    assert '--scheduled and --profile are mutually exclusive' in capsys.readouterr().err


def test_reseed_scheduled_dispatches_to_do_reseed_scheduled(monkeypatch, capsys,
                                                            tmp_config, clean_env):
    called = []
    monkeypatch.setattr(cli_mod, 'do_reseed_scheduled',
                        lambda: called.append(True) or 0)
    rc = _run(monkeypatch, ['reseed', '--scheduled'])
    assert rc == 0
    assert called == [True]


def test_reseed_scheduled_json_envelope_scope(monkeypatch, capsys,
                                              tmp_config, clean_env):
    monkeypatch.setattr(cli_mod, 'do_reseed_scheduled', lambda: 0)
    rc = _run(monkeypatch, ['reseed', '--scheduled', '--json'])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env['ok'] is True
    assert env['stats']['scope'] == 'scheduled'


# --- profiles schedule / unschedule -----------------------------------


def test_profiles_schedule_unknown_profile_fails(monkeypatch, capsys,
                                                 tmp_config, clean_env):
    rc = _run(monkeypatch, ['profiles', 'schedule', 'ghost'])
    assert rc != 0
    assert 'not found' in capsys.readouterr().err


def test_profiles_schedule_adds_to_registry(monkeypatch, capsys,
                                            tmp_config, clean_env):
    """`profiles schedule` edits OWA_SCHEDULED; the one-time shared-agent
    install is stubbed so the test never touches launchctl."""
    from owa_piggy import launchd as launchd_mod
    from owa_piggy.config import (
        ensure_profile_registered, load_profiles_conf, profile_dir,
    )

    profile_dir('work').mkdir(parents=True)
    ensure_profile_registered('work')
    monkeypatch.setattr(launchd_mod, '_run_setup_refresh_script',
                        lambda *a: 0)
    monkeypatch.setattr(launchd_mod, 'shared_agent_installed', lambda: True)

    rc = _run(monkeypatch, ['profiles', 'schedule', 'work'])
    assert rc == 0
    assert load_profiles_conf()['OWA_SCHEDULED'] == ['work']


def test_profiles_unschedule_removes_from_registry(monkeypatch, capsys,
                                                   tmp_config, clean_env):
    from owa_piggy import launchd as launchd_mod
    from owa_piggy.config import (
        ensure_profile_registered, load_profiles_conf, profile_dir,
        schedule_profile,
    )

    profile_dir('work').mkdir(parents=True)
    ensure_profile_registered('work')
    schedule_profile('work')
    # Stub the uninstall so emptying the schedule doesn't shell out.
    monkeypatch.setattr(launchd_mod, '_run_setup_refresh_script', lambda *a: 0)
    monkeypatch.setattr(launchd_mod, 'shared_agent_installed', lambda: True)

    rc = _run(monkeypatch, ['profiles', 'unschedule', 'work'])
    assert rc == 0
    out = load_profiles_conf()
    assert out['OWA_SCHEDULED'] == []
    assert out['OWA_PROFILES'] == ['work']
