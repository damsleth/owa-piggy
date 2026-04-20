"""Tests for the access token cache."""
import json
import stat
import time

from owa_piggy.cache import (
    clear_cache,
    get_cached_exp,
    get_cached_token,
    load_cache,
    store_token,
)


def test_empty_cache_returns_none(tmp_config, clean_env):
    assert get_cached_token('any-scope') is None
    assert get_cached_exp('any-scope') is None
    assert load_cache() == {}


def test_store_and_retrieve(tmp_config, clean_env):
    future = int(time.time()) + 3600
    store_token('graph-scope', 'fake-at-value', future)
    assert get_cached_token('graph-scope') == 'fake-at-value'
    assert get_cached_exp('graph-scope') == future


def test_expired_token_not_returned(tmp_config, clean_env):
    past = int(time.time()) - 60
    store_token('scope', 'fake-at', past)
    assert get_cached_token('scope') is None


def test_min_remaining_threshold(tmp_config, clean_env):
    """Token expiring in 30s is considered too close; default floor is 60s."""
    soon = int(time.time()) + 30
    store_token('scope', 'fake-at', soon)
    assert get_cached_token('scope') is None
    assert get_cached_token('scope', min_remaining_seconds=0) == 'fake-at'


def test_different_scopes_cached_independently(tmp_config, clean_env):
    future = int(time.time()) + 3600
    store_token('graph-scope', 'at-graph', future)
    store_token('teams-scope', 'at-teams', future + 100)
    assert get_cached_token('graph-scope') == 'at-graph'
    assert get_cached_token('teams-scope') == 'at-teams'


def test_cache_file_permissions(tmp_config, clean_env):
    store_token('scope', 'fake-at', int(time.time()) + 3600)
    cache_path = tmp_config.parent / 'cache.json'
    mode = stat.S_IMODE(cache_path.stat().st_mode)
    assert mode == 0o600


def test_corrupted_cache_returns_empty(tmp_config, clean_env):
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    cache_path = tmp_config.parent / 'cache.json'
    cache_path.write_text('{not valid json')
    assert load_cache() == {}
    assert get_cached_token('any-scope') is None


def test_non_dict_cache_returns_empty(tmp_config, clean_env):
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    cache_path = tmp_config.parent / 'cache.json'
    cache_path.write_text('[1, 2, 3]')  # valid JSON, wrong shape
    assert load_cache() == {}


def test_store_overwrites_previous(tmp_config, clean_env):
    future = int(time.time()) + 3600
    store_token('scope', 'old-at', future)
    store_token('scope', 'new-at', future + 100)
    assert get_cached_token('scope') == 'new-at'


def test_clear_cache_removes_file(tmp_config, clean_env):
    store_token('scope', 'fake-at', int(time.time()) + 3600)
    cache_path = tmp_config.parent / 'cache.json'
    assert cache_path.exists()
    clear_cache()
    assert not cache_path.exists()


def test_clear_cache_idempotent(tmp_config, clean_env):
    """Clearing a cache that doesn't exist is a no-op, not an error."""
    clear_cache()
    clear_cache()


def test_cache_file_is_valid_json(tmp_config, clean_env):
    future = int(time.time()) + 3600
    store_token('scope-a', 'at-a', future)
    store_token('scope-b', 'at-b', future)
    cache_path = tmp_config.parent / 'cache.json'
    parsed = json.loads(cache_path.read_text())
    assert 'scope-a' in parsed
    assert 'scope-b' in parsed
    assert parsed['scope-a']['access_token'] == 'at-a'
