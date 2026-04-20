"""Tests for the access token cache.

Cache is keyed by (tenant_id, client_id, scope); these tests exercise
that key structure explicitly so a regression to scope-only keying
would be caught.
"""
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

# Canonical triple used across tests. The values are obvious fakes so a
# grep never flags this file as shipping a real token.
TID = '00000000-0000-0000-0000-000000000000'
CID = '9199bf20-a13f-4107-85dc-02114787ef48'
SCOPE = 'https://graph.microsoft.com/.default openid profile offline_access'


def test_empty_cache_returns_none(tmp_config, clean_env):
    assert get_cached_token(TID, CID, SCOPE) is None
    assert get_cached_exp(TID, CID, SCOPE) is None
    assert load_cache() == {}


def test_store_and_retrieve(tmp_config, clean_env):
    future = int(time.time()) + 3600
    store_token(TID, CID, SCOPE, 'fake-at-value', future)
    assert get_cached_token(TID, CID, SCOPE) == 'fake-at-value'
    assert get_cached_exp(TID, CID, SCOPE) == future


def test_expired_token_not_returned(tmp_config, clean_env):
    past = int(time.time()) - 60
    store_token(TID, CID, SCOPE, 'fake-at', past)
    assert get_cached_token(TID, CID, SCOPE) is None


def test_min_remaining_threshold(tmp_config, clean_env):
    """Token expiring in 30s is too close; default floor is 60s."""
    soon = int(time.time()) + 30
    store_token(TID, CID, SCOPE, 'fake-at', soon)
    assert get_cached_token(TID, CID, SCOPE) is None
    assert get_cached_token(TID, CID, SCOPE, min_remaining_seconds=0) == 'fake-at'


def test_different_scopes_cached_independently(tmp_config, clean_env):
    future = int(time.time()) + 3600
    graph_scope = 'https://graph.microsoft.com/.default'
    teams_scope = 'https://api.spaces.skype.com/.default'
    store_token(TID, CID, graph_scope, 'at-graph', future)
    store_token(TID, CID, teams_scope, 'at-teams', future + 100)
    assert get_cached_token(TID, CID, graph_scope) == 'at-graph'
    assert get_cached_token(TID, CID, teams_scope) == 'at-teams'


def test_different_tenants_cached_independently(tmp_config, clean_env):
    """Regression anchor for QA finding #1: same scope, different
    tenants must NEVER share cache entries."""
    tid_a = '11111111-1111-1111-1111-111111111111'
    tid_b = '22222222-2222-2222-2222-222222222222'
    future = int(time.time()) + 3600
    store_token(tid_a, CID, SCOPE, 'at-tenant-a', future)
    store_token(tid_b, CID, SCOPE, 'at-tenant-b', future)
    assert get_cached_token(tid_a, CID, SCOPE) == 'at-tenant-a'
    assert get_cached_token(tid_b, CID, SCOPE) == 'at-tenant-b'


def test_different_clients_cached_independently(tmp_config, clean_env):
    """Same tenant + scope with different OWA_CLIENT_ID overrides must
    not cross-contaminate."""
    cid_a = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    cid_b = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    future = int(time.time()) + 3600
    store_token(TID, cid_a, SCOPE, 'at-client-a', future)
    store_token(TID, cid_b, SCOPE, 'at-client-b', future)
    assert get_cached_token(TID, cid_a, SCOPE) == 'at-client-a'
    assert get_cached_token(TID, cid_b, SCOPE) == 'at-client-b'


def test_cache_file_permissions(tmp_config, clean_env):
    store_token(TID, CID, SCOPE, 'fake-at', int(time.time()) + 3600)
    cache_path = tmp_config.parent / 'cache.json'
    mode = stat.S_IMODE(cache_path.stat().st_mode)
    assert mode == 0o600


def test_corrupted_cache_returns_empty(tmp_config, clean_env):
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    cache_path = tmp_config.parent / 'cache.json'
    cache_path.write_text('{not valid json')
    assert load_cache() == {}
    assert get_cached_token(TID, CID, SCOPE) is None


def test_non_dict_cache_returns_empty(tmp_config, clean_env):
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    cache_path = tmp_config.parent / 'cache.json'
    cache_path.write_text('[1, 2, 3]')
    assert load_cache() == {}


def test_store_overwrites_previous(tmp_config, clean_env):
    future = int(time.time()) + 3600
    store_token(TID, CID, SCOPE, 'old-at', future)
    store_token(TID, CID, SCOPE, 'new-at', future + 100)
    assert get_cached_token(TID, CID, SCOPE) == 'new-at'


def test_clear_cache_removes_file(tmp_config, clean_env):
    store_token(TID, CID, SCOPE, 'fake-at', int(time.time()) + 3600)
    cache_path = tmp_config.parent / 'cache.json'
    assert cache_path.exists()
    clear_cache()
    assert not cache_path.exists()


def test_clear_cache_idempotent(tmp_config, clean_env):
    clear_cache()
    clear_cache()


def test_cache_file_is_valid_json(tmp_config, clean_env):
    future = int(time.time()) + 3600
    store_token(TID, CID, 'scope-a', 'at-a', future)
    store_token(TID, CID, 'scope-b', 'at-b', future)
    cache_path = tmp_config.parent / 'cache.json'
    parsed = json.loads(cache_path.read_text())
    # Two entries with composite keys of the form "tenant|client|scope".
    assert len(parsed) == 2
    for key, entry in parsed.items():
        assert key.startswith(f'{TID}|{CID}|')
        assert 'access_token' in entry
        assert 'exp' in entry
