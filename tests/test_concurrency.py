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


def test_orphan_edge_pids_picks_only_parentless_capture_browsers(tmp_path):
    """Reapable garbage is a parentless browser *we* launched: ppid==1 plus the
    --remote-debugging-port flag that only `launch_edge` sets. A user's own
    window is ppid==1 too (open_edge detaches and the CLI exits), so the debug
    port - not --headless - is what keeps us from killing a live sign-in."""
    from owa_piggy.capture import orphan_edge_pids

    edge_dir = tmp_path / "edge-profile"
    dd = f"--user-data-dir={edge_dir}"
    other = f"--user-data-dir={tmp_path / 'other-profile'}"
    dbg = "--remote-debugging-port=9"
    ps = "\n".join(
        [
            # reapable: abandoned headless capture browser
            f"  501     1 /Edge --headless=new {dbg} {dd}",
            # reapable: abandoned *windowless* offscreen capture browser. Not
            # headless, invisible, and the class that hangs a profile forever.
            f"  502     1 /Edge --no-startup-window {dbg} {dd}",
            # spared: the user's own interactive window. ppid==1 by construction
            # (start_new_session=True), so only the missing debug port saves it.
            f"  504     1 /Edge {dd}",
            # spared: helpers inherit both flags but are not the browser
            f"  505   501 /Edge Helper --type=renderer {dbg} {dd}",
            f"  506     1 /Edge Helper --type=gpu-process {dbg} {dd}",
            # spared: still owned by a live owa-piggy
            f"  507  9999 /Edge --headless=new {dbg} {dd}",
            # spared: someone else's profile dir
            f"  508     1 /Edge --headless=new {dbg} {other}",
            "garbage",
        ]
    )
    assert orphan_edge_pids(ps, edge_dir) == [501, 502]


def test_orphan_edge_pids_never_reaps_a_live_user_window(tmp_path):
    """The invariant that must survive any future refactor: no `owa-piggy` path,
    interactive or background, may kill the window the user is signed into.
    `owa-piggy token` runs on a cron loop, so a regression here would kill a
    live sign-in from a background job."""
    from owa_piggy.capture import orphan_edge_pids

    edge_dir = tmp_path / "edge-profile"
    # Exactly the argv `open_edge` builds, reparented to init as it always is.
    window = (
        f"  601     1 /Edge --no-first-run --no-default-browser-check "
        f"--user-data-dir={edge_dir} https://outlook.cloud.microsoft"
    )
    assert orphan_edge_pids(window, edge_dir) == []
