"""Access token cache.

Lives alongside the main config at ~/.config/owa-piggy/cache.json, keyed
by the exact scope string we send to AAD. Keeps ~1KB per cached token
and cuts AAD round-trips to zero for back-to-back calls within a token's
lifetime (~60-90 min) - which matters when callers shell out to
`owa-piggy` many times in a loop and would otherwise risk 429s.

Cache path is derived at call time from config.CONFIG_PATH so that
test fixtures which monkeypatch the config path get the cache
redirected into tmp_path automatically.
"""
import json
import os
import tempfile
import time
from pathlib import Path

from . import config as _config

CACHE_FILENAME = 'cache.json'


def _cache_path():
    return _config.CONFIG_PATH.parent / CACHE_FILENAME


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


def get_cached_token(scope, min_remaining_seconds=60):
    """Return the cached access token for `scope` if it has at least
    `min_remaining_seconds` left before `exp`, else None.

    The 60s floor avoids handing out a token that will expire between
    the check and the caller's subsequent HTTP request."""
    entry = load_cache().get(scope)
    if not entry:
        return None
    exp = entry.get('exp', 0)
    if exp <= time.time() + min_remaining_seconds:
        return None
    token = entry.get('access_token')
    return token if isinstance(token, str) and token else None


def get_cached_exp(scope):
    """Return the cached `exp` (unix seconds) for `scope`, or None."""
    entry = load_cache().get(scope)
    if not entry:
        return None
    exp = entry.get('exp')
    return exp if isinstance(exp, (int, float)) else None


def store_token(scope, access_token, exp):
    """Write an access token for `scope` to the cache, atomically.

    Corrupting this file is harmless - the tool will just refetch. But
    write atomically anyway so concurrent `owa-piggy` invocations (which
    happen in shell loops) can't interleave and produce invalid JSON."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = load_cache()
    cache[scope] = {'access_token': access_token, 'exp': int(exp)}
    payload = json.dumps(cache, indent=2) + '\n'

    fd, tmp_path = tempfile.mkstemp(
        prefix='.cache.', suffix='.tmp', dir=str(path.parent)
    )
    tmp = Path(tmp_path)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def clear_cache():
    """Remove the cache file if present. Used by `--reseed` and any other
    path that invalidates existing tokens (scope changes, RT rotation
    that should flush stale audiences, etc.)."""
    path = _cache_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
