"""Tests for multi-profile support: profile registry, resolution, migration.

The `tmp_config` fixture patches both ROOT_DIR and CONFIG_PATH into
tmp_path, so everything written here is fully sandboxed.
"""
import stat

from owa_piggy import config as config_mod
from owa_piggy import migration
from owa_piggy.config import (
    ensure_profile_registered,
    list_profiles,
    load_profiles_conf,
    profile_config_path,
    profile_dir,
    profile_edge_dir,
    profile_log_path,
    profiles_conf_path,
    profiles_dir,
    resolve_profile,
    save_config,
    save_profiles_conf,
    set_active_profile,
    unregister_profile,
    validate_alias,
)
import pytest


# --- Path helpers ------------------------------------------------------


def test_profile_paths_resolve_under_root(tmp_config, clean_env):
    root = tmp_config.parent
    assert profiles_dir() == root / 'profiles'
    assert profiles_conf_path() == root / 'profiles.conf'
    assert profile_dir('work') == root / 'profiles' / 'work'
    assert profile_config_path('work') == root / 'profiles' / 'work' / 'config'
    assert profile_edge_dir('work') == root / 'profiles' / 'work' / 'edge-profile'
    assert profile_log_path('work') == root / 'profiles' / 'work' / 'refresh.log'


def test_set_active_profile_rebinds_config_path(tmp_config, clean_env):
    before = config_mod.CONFIG_PATH
    returned = set_active_profile('work')
    assert config_mod.CONFIG_PATH == returned
    assert config_mod.CONFIG_PATH != before
    assert config_mod.CONFIG_PATH == profile_config_path('work')


def test_set_active_profile_redirects_cache(tmp_config, clean_env):
    """Regression anchor: cache.py reads _config.CONFIG_PATH at call time,
    so set_active_profile must automatically re-scope the cache without
    any separate plumbing."""
    import time
    from owa_piggy.cache import _cache_path, store_token

    set_active_profile('work')
    store_token('tid', 'cid', 'scope', 'at-work', int(time.time()) + 3600)
    assert _cache_path() == profile_dir('work') / 'cache.json'
    assert _cache_path().exists()

    set_active_profile('personal')
    # Same call hits a different file - no cross-profile leak.
    assert _cache_path() == profile_dir('personal') / 'cache.json'
    assert not _cache_path().exists()


# --- profiles.conf I/O -------------------------------------------------


def test_load_profiles_conf_missing_returns_defaults(tmp_config, clean_env):
    assert not profiles_conf_path().exists()
    out = load_profiles_conf()
    assert out == {'OWA_DEFAULT_PROFILE': '', 'OWA_PROFILES': []}


def test_save_and_load_profiles_conf_round_trip(tmp_config, clean_env):
    save_profiles_conf({
        'OWA_DEFAULT_PROFILE': 'work',
        'OWA_PROFILES': ['default', 'work', 'personal'],
    })
    out = load_profiles_conf()
    assert out['OWA_DEFAULT_PROFILE'] == 'work'
    assert out['OWA_PROFILES'] == ['default', 'work', 'personal']


def test_save_profiles_conf_permissions(tmp_config, clean_env):
    save_profiles_conf({'OWA_DEFAULT_PROFILE': 'x', 'OWA_PROFILES': ['x']})
    mode = stat.S_IMODE(profiles_conf_path().stat().st_mode)
    assert mode == 0o600


def test_save_profiles_conf_deduplicates(tmp_config, clean_env):
    """Re-registering an alias that's already in the list is a no-op."""
    save_profiles_conf({
        'OWA_DEFAULT_PROFILE': 'work',
        'OWA_PROFILES': ['work', 'work', 'default', 'work'],
    })
    out = load_profiles_conf()
    assert out['OWA_PROFILES'] == ['work', 'default']


def test_save_profiles_conf_atomic_no_stray_tmp(tmp_config, clean_env):
    save_profiles_conf({'OWA_DEFAULT_PROFILE': 'x', 'OWA_PROFILES': ['x']})
    siblings = [p.name for p in profiles_conf_path().parent.iterdir()]
    # Only the config file plus profiles.conf; no stray `.profiles.*.tmp`.
    assert all(not n.startswith('.profiles.') for n in siblings)


def test_ensure_profile_registered_makes_first_default(tmp_config, clean_env):
    ensure_profile_registered('work')
    out = load_profiles_conf()
    assert out['OWA_DEFAULT_PROFILE'] == 'work'
    assert out['OWA_PROFILES'] == ['work']


def test_ensure_profile_registered_second_does_not_overwrite_default(
    tmp_config, clean_env
):
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    out = load_profiles_conf()
    assert out['OWA_DEFAULT_PROFILE'] == 'work'
    assert out['OWA_PROFILES'] == ['work', 'personal']


def test_ensure_profile_registered_idempotent(tmp_config, clean_env):
    ensure_profile_registered('work')
    ensure_profile_registered('work')
    ensure_profile_registered('work')
    out = load_profiles_conf()
    assert out['OWA_PROFILES'] == ['work']


def test_unregister_profile_clears_default(tmp_config, clean_env):
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    unregister_profile('work')
    out = load_profiles_conf()
    # work was default; default is now empty so the next --set-default or
    # setup call has to pick an explicit replacement.
    assert out['OWA_DEFAULT_PROFILE'] == ''
    assert out['OWA_PROFILES'] == ['personal']


# --- list_profiles ----------------------------------------------------


def test_list_profiles_empty_on_fresh(tmp_config, clean_env):
    assert list_profiles() == []


def test_list_profiles_only_counts_dirs(tmp_config, clean_env):
    pd = profiles_dir()
    pd.mkdir(parents=True)
    (pd / 'work').mkdir()
    (pd / 'personal').mkdir()
    (pd / 'notes.txt').write_text('stray file')
    assert list_profiles() == ['personal', 'work']


# --- resolve_profile --------------------------------------------------


def test_resolve_cli_flag_wins(tmp_config, clean_env):
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    # Even with a default set, --profile beats it.
    alias, err = resolve_profile('personal')
    assert err == ''
    assert alias == 'personal'


def test_resolve_missing_cli_profile_errors(tmp_config, clean_env):
    ensure_profile_registered('work')
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    alias, err = resolve_profile('typo')
    assert alias == ''
    assert 'typo' in err
    assert 'work' in err


def test_resolve_cli_profile_allow_missing_for_setup(tmp_config, clean_env):
    """--setup needs to accept a brand-new alias even though it doesn't
    exist on disk yet."""
    alias, err = resolve_profile('brand-new', allow_missing=True)
    assert err == ''
    assert alias == 'brand-new'


def test_resolve_env_overrides_default(tmp_config, clean_env, monkeypatch):
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    profile_dir('personal').mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('OWA_PROFILE', 'personal')
    alias, err = resolve_profile(None)
    assert err == ''
    assert alias == 'personal'


def test_resolve_default_when_no_flag_or_env(tmp_config, clean_env):
    ensure_profile_registered('work')
    ensure_profile_registered('personal')
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    profile_dir('personal').mkdir(parents=True, exist_ok=True)
    alias, err = resolve_profile(None)
    assert err == ''
    assert alias == 'work'  # first one registered becomes default


def test_resolve_single_profile_autoselects(tmp_config, clean_env):
    """If exactly one profile exists on disk, use it even without a
    registry default pointer."""
    profile_dir('only').mkdir(parents=True, exist_ok=True)
    alias, err = resolve_profile(None)
    assert err == ''
    assert alias == 'only'


def test_resolve_fresh_install_uses_default(tmp_config, clean_env):
    """No profiles anywhere + no flag = 'default' (so first --setup
    works without any ceremony)."""
    alias, err = resolve_profile(None)
    assert err == ''
    assert alias == 'default'


def test_resolve_ambiguity_errors(tmp_config, clean_env):
    """Multiple profiles exist, none marked default, no flag/env - should
    error rather than guess."""
    profile_dir('work').mkdir(parents=True, exist_ok=True)
    profile_dir('personal').mkdir(parents=True, exist_ok=True)
    # Deliberately no profiles.conf: emulates someone hand-creating dirs.
    alias, err = resolve_profile(None)
    assert alias == ''
    assert 'multiple' in err.lower() or 'ambig' in err.lower()
    assert 'work' in err
    assert 'personal' in err


# --- Migration --------------------------------------------------------


def test_migration_no_op_on_fresh(tmp_config, clean_env):
    result = migration.migrate_if_needed()
    assert result is None


def test_migration_moves_legacy_config(tmp_config, clean_env):
    """Legacy layout -> profiles/default/ layout."""
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    legacy = tmp_config
    assert legacy.exists()

    result = migration.migrate_if_needed()
    assert result == 'default'
    # Legacy file is gone; default profile has it.
    assert not legacy.exists()
    assert profile_config_path('default').exists()
    # Registry is populated.
    out = load_profiles_conf()
    assert out['OWA_DEFAULT_PROFILE'] == 'default'
    assert 'default' in out['OWA_PROFILES']


def test_migration_moves_cache_and_edge_dir(tmp_config, clean_env):
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    root = tmp_config.parent
    (root / 'cache.json').write_text('{}')
    (root / 'edge-profile').mkdir()
    (root / 'edge-profile' / 'marker').write_text('test')

    migration.migrate_if_needed()

    assert (profile_dir('default') / 'cache.json').exists()
    assert (profile_dir('default') / 'edge-profile' / 'marker').exists()
    assert not (root / 'cache.json').exists()
    assert not (root / 'edge-profile').exists()


def test_migration_idempotent(tmp_config, clean_env):
    """Second call after migration is a no-op."""
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    migration.migrate_if_needed()
    # Second call must not error and must not re-touch anything.
    assert migration.migrate_if_needed() is None
    assert profile_config_path('default').exists()


# --- Alias validation -------------------------------------------------


@pytest.mark.parametrize('alias', [
    'work', 'personal', 'default', 'brand-new', 'work.2', 'a_b',
    'A', '0', 'Azure.Prod_01-eu',
])
def test_validate_alias_accepts_slug(alias):
    ok, err = validate_alias(alias)
    assert ok, err
    assert err == ''


@pytest.mark.parametrize('alias', [
    '', '.', '..', '../escape', '../../outside', 'work/sub',
    'has space', 'foo;rm', 'foo\x00bar', 'foo/../bar',
    'tab\there', '\n', './hidden',
])
def test_validate_alias_rejects_unsafe(alias):
    ok, err = validate_alias(alias)
    assert not ok
    assert err


def test_validate_alias_rejects_non_string():
    ok, err = validate_alias(None)
    assert not ok
    ok, err = validate_alias(42)
    assert not ok


def test_resolve_profile_rejects_traversal_cli_flag(tmp_config, clean_env):
    """--profile ../../outside must not resolve; it would let save_config()
    write outside the owa-piggy config tree."""
    alias, err = resolve_profile('../../outside', allow_missing=True)
    assert alias == ''
    assert err


def test_resolve_profile_rejects_nested_cli_flag(tmp_config, clean_env):
    """--profile work/sub creates a nested dir that list_profiles() can't
    round-trip back to the original alias."""
    alias, err = resolve_profile('work/sub', allow_missing=True)
    assert alias == ''
    assert err


def test_resolve_profile_rejects_bad_env(tmp_config, clean_env, monkeypatch):
    monkeypatch.setenv('OWA_PROFILE', '../../outside')
    alias, err = resolve_profile(None)
    assert alias == ''
    assert 'OWA_PROFILE' in err


def test_ensure_profile_registered_rejects_bad_alias(tmp_config, clean_env):
    with pytest.raises(ValueError):
        ensure_profile_registered('../escape')


def test_migration_skips_when_profiles_dir_present(tmp_config, clean_env):
    """If the user set up a profile-aware install first (no legacy
    config), migration must not kick in even if a legacy path somehow
    appears later."""
    profile_dir('work').mkdir(parents=True)
    save_config({'OWA_REFRESH_TOKEN': '1.AQ_fake', 'OWA_TENANT_ID': 'tid'})
    # Legacy path exists AND profiles/ exists. Migration is a no-op.
    assert migration.migrate_if_needed() is None
    assert tmp_config.exists()  # legacy untouched
