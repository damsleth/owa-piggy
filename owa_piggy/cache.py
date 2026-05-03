"""Access token cache.

Lives alongside the main config at ~/.config/owa-piggy/cache.json, keyed
by (tenant_id, client_id, scope). Keeping tenant+client in the key
prevents one account's AT from being served after a config change to a
different tenant or a client-ID override.

~1KB per cached token; cuts AAD round-trips to zero for back-to-back
calls within a token's lifetime (~60-90 min), which matters when
callers shell out to `owa-piggy` many times in a loop and would
otherwise risk 429s.

Cache path is derived at call time from config.CONFIG_PATH so that
test fixtures which monkeypatch the config path get the cache
redirected into tmp_path automatically.
"""
import json
import time

from . import config as _config
from .config import atomic_write

CACHE_FILENAME = 'cache.json'


def _cache_path():
    return _config.CONFIG_PATH.parent / CACHE_FILENAME


def _key(tenant_id, client_id, scope):
    """Compose the cache key. Pipe-separated because tenant_id and
    client_id are UUIDs (no pipes) and scope strings only contain
    URL/space characters."""
    return f'{tenant_id}|{client_id}|{scope}'


def load_cache():
    """Return the whole cache dict, or {} on any kind of corruption.

    A malformed cache must never crash the tool - worst case we pay a
    single AAD round-trip, same as a cold cache."""
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def get_cached_token(tenant_id, client_id, scope, min_remaining_seconds=60):
    """Return the cached access token for the (tenant, client, scope)
    triple if it has at least `min_remaining_seconds` left before `exp`,
    else None.

    The 60s floor avoids handing out a token that will expire between
    the check and the caller's subsequent HTTP request."""
    entry = load_cache().get(_key(tenant_id, client_id, scope))
    if not entry:
        return None
    exp = entry.get('exp', 0)
    if exp <= time.time() + min_remaining_seconds:
        return None
    token = entry.get('access_token')
    return token if isinstance(token, str) and token else None


def get_cached_exp(tenant_id, client_id, scope):
    """Return the cached `exp` (unix seconds) for the triple, or None."""
    entry = load_cache().get(_key(tenant_id, client_id, scope))
    if not entry:
        return None
    exp = entry.get('exp')
    return exp if isinstance(exp, (int, float)) else None


def store_token(tenant_id, client_id, scope, access_token, exp):
    """Write an access token for the (tenant, client, scope) triple to the
    cache, atomically.

    Corrupting this file is harmless - the tool will just refetch. But
    write atomically anyway so concurrent `owa-piggy` invocations (which
    happen in shell loops) can't interleave and produce invalid JSON."""
    cache = load_cache()
    cache[_key(tenant_id, client_id, scope)] = {
        'access_token': access_token,
        'exp': int(exp),
    }
    atomic_write(_cache_path(), json.dumps(cache, indent=2) + '\n')


def clear_cache():
    """Remove the cache file if present. Called by `setup` and `reseed`
    so the cache never outlives an identity change."""
    path = _cache_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
