"""Concurrency stress test for the atomic-write safety model.

The real-world failure mode this guards against: a user shells out to
`owa-piggy` many times in a tight loop (or several launchd reseeds fire
at once), and the resulting concurrent `save_config` + `store_token`
calls hammer the SAME profile config and cache file. Because every
refresh-token exchange rotates the token, a torn write here corrupts the
only live credential. `atomic_write` (temp file + fsync + rename) exists
precisely so that under contention the reader always sees either the old
or the new whole file, never a truncated mix.

This test reproduces that scenario with ~50 worker threads doing many
iterations of get-or-mint then save_config + store_token against one
config and one cache file, then asserts that afterward:
  - the config parses cleanly and OWA_REFRESH_TOKEN is one of the finite
    set of legitimately-rotated values (never garbage / torn),
  - the cache parses cleanly into a non-corrupt dict whose entries are
    well-formed (a corrupt cache would degrade to {} via load_cache),
  - no leftover `.*.tmp` shrapnel remains in the config dir.

Uses threads, not processes: the atomic rename is what serializes the
writers, and that mechanism is exercised within a single process. No
network, no real tokens, all writes under tmp_path.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from owa_piggy import cache as cache_mod
from owa_piggy.config import load_config, save_config

# Obvious fakes; a grep must never flag this file as shipping a real token.
TID = "00000000-0000-0000-0000-000000000000"
CID = "9199bf20-a13f-4107-85dc-02114787ef48"
SCOPE = "https://graph.microsoft.com/.default offline_access"

WORKERS = 50
ITERATIONS = 20

# The finite, known-good set of values a rotated refresh token can take.
# Any on-disk value outside this set means a write got torn into garbage.
LEGIT_RTS = frozenset(f"1.ROTATED-{i}" for i in range(WORKERS * ITERATIONS))


def _seed_config(tmp_config):
    """Lay down a starting config so the first load has a baseline RT."""
    save_config(
        {
            "OWA_REFRESH_TOKEN": "1.ROTATED-0",
            "OWA_TENANT_ID": TID,
            "OWA_CLIENT_ID": CID,
        }
    )


def _worker(worker_id):
    """One shell-loop invocation, repeated: load, rotate, persist, cache."""
    future = int(time.time()) + 3600
    for step in range(ITERATIONS):
        # get-or-mint: read current config (may be mid-rotation by a peer)
        cfg, _persist = load_config()
        # compute a rotated token from the known finite legitimate set
        chosen = f"1.ROTATED-{(worker_id * ITERATIONS + step) % len(LEGIT_RTS)}"
        cfg["OWA_REFRESH_TOKEN"] = chosen
        cfg.setdefault("OWA_TENANT_ID", TID)
        cfg.setdefault("OWA_CLIENT_ID", CID)
        save_config(cfg)
        cache_mod.store_token(TID, CID, SCOPE, f"at-{worker_id}-{step}", future)


def test_parallel_shell_loop_keeps_config_and_cache_intact(tmp_config, clean_env):
    _seed_config(tmp_config)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_worker, wid) for wid in range(WORKERS)]
        for f in futures:
            # Re-raise any worker exception (e.g. a write that blew up).
            f.result()

    # --- Config survived: parses cleanly and holds a legitimate RT --------
    cfg, persist = load_config()
    assert persist is True
    rt = cfg.get("OWA_REFRESH_TOKEN")
    assert rt in LEGIT_RTS, f"config holds a torn/garbage refresh token: {rt!r}"
    # Tenant/client lines must have survived the concurrent rewrites intact.
    assert cfg.get("OWA_TENANT_ID") == TID
    assert cfg.get("OWA_CLIENT_ID") == CID

    # --- Cache survived: valid JSON dict, not the {} corruption fallback ---
    # Read the raw bytes and parse directly so we can distinguish a genuinely
    # well-formed cache from load_cache()'s silent {}-on-corruption behavior.
    cache_path = tmp_config.parent / cache_mod.CACHE_FILENAME
    assert cache_path.exists()
    raw = json.loads(cache_path.read_text())  # raises if torn into garbage
    assert isinstance(raw, dict)
    assert raw, "cache is empty - a torn write likely tripped the {} fallback"
    cache = cache_mod.load_cache()
    assert cache == raw
    # Every entry is well-formed: the (tenant|client|scope) key with a
    # string access_token and an int exp.
    key = f"{TID}|{CID}|{SCOPE}"
    assert key in cache
    for entry in cache.values():
        assert isinstance(entry, dict)
        assert isinstance(entry["access_token"], str) and entry["access_token"]
        assert isinstance(entry["exp"], int)

    # --- No atomic-write shrapnel left behind -----------------------------
    leftovers = [p.name for p in tmp_config.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"stray temp files remain: {leftovers}"
