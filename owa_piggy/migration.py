"""One-shot migration from the legacy single-config layout to the
profile-per-directory layout.

Legacy installs have:
    ~/.config/owa-piggy/config
    ~/.config/owa-piggy/cache.json
    ~/.config/owa-piggy/edge-profile/

After migration:
    ~/.config/owa-piggy/profiles/default/config
    ~/.config/owa-piggy/profiles/default/cache.json
    ~/.config/owa-piggy/profiles/default/edge-profile/
    ~/.config/owa-piggy/profiles.conf   (OWA_DEFAULT_PROFILE="default")

Runs idempotently at the top of `main()` before any profile is resolved.
If `profiles/` already exists, nothing happens - this means users who
ran `owa-piggy setup --profile work` first (without ever having had a
legacy layout) never see the migration.

The move is done with `os.replace` for atomicity where possible, falling
back to shutil for the directory move. We only ever *move* existing
artifacts - no config rewrites, no re-reading of secrets - so the
refresh token never transits a new process or touches disk twice.
"""

from __future__ import annotations

import shutil
import sys

from . import config as _config


def migrate_if_needed():
    """Move legacy single-file config into profiles/default/ if needed.

    Returns the alias that got migrated (always 'default' today) or None
    if nothing was migrated. Prints a one-line notice to stderr on a
    successful migration so the user knows their layout changed.
    """
    root = _config.ROOT_DIR
    legacy_config = root / "config"
    legacy_cache = root / "cache.json"
    legacy_edge = root / "edge-profile"
    profiles_root = _config.profiles_dir()

    # Idempotent: if profiles/ exists, we've already migrated (or the
    # user's first-ever setup was profile-aware).
    if profiles_root.exists():
        return None

    # Nothing to migrate: no legacy config file on disk.
    if not legacy_config.is_file() or legacy_config.is_symlink():
        return None

    alias = "default"
    target_dir = _config.profile_dir(alias)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Move each legacy artifact into the default profile. os.replace is
    # atomic within a filesystem; the config file holds the live RT so
    # atomicity matters. Cache and edge-profile are best-effort.
    moved = []
    try:
        legacy_config.replace(target_dir / "config")
        moved.append("config")
    except OSError as e:
        print(f"ERROR: migration failed to move {legacy_config}: {e}", file=sys.stderr)
        return None

    if legacy_cache.is_file():
        try:
            legacy_cache.replace(target_dir / "cache.json")
            moved.append("cache.json")
        except OSError:
            # Cache is regenerable, don't fail the migration over it.
            pass

    if legacy_edge.is_dir() and not legacy_edge.is_symlink():
        dest_edge = target_dir / "edge-profile"
        try:
            # shutil.move handles cross-device gracefully but the common
            # case is same-filesystem (both under ~/.config).
            shutil.move(str(legacy_edge), str(dest_edge))
            moved.append("edge-profile")
        except OSError:
            # Edge profile can always be recreated by re-running one-time
            # setup; not worth failing the migration.
            pass

    # Register the 'default' profile so list/resolve pick it up.
    _config.ensure_profile_registered(alias, make_default_if_first=True)

    print(
        f"owa-piggy: migrated legacy config to profile {alias!r} ({', '.join(moved)})",
        file=sys.stderr,
    )
    return alias


FOLD_STAMP = "fold-bound-clients.done"


def fold_bound_clients_if_needed():
    """Fold client-bound profiles into their identity's profile, once.

    Before profiles could hold several clients, the only way to keep an
    Azure DevOps or Teams token was a profile of its own - same user, same
    Edge session, second directory. Those are now a `clients.json` entry on
    the real profile, so this moves them across and leaves the old alias
    pointing at the parent (`OWA_FOLDED_INTO`).

    Guarded by a stamp file: `owa-piggy` is shelled out to in tight loops
    by the owa-tools suite, and scanning every profile config on every
    invocation to discover there is nothing to do is pure overhead.
    """
    from . import clients

    stamp = _config.ROOT_DIR / FOLD_STAMP
    if stamp.exists():
        return []
    folded = []
    try:
        for bound_alias, parent_alias, client_id in clients.fold_candidates():
            if clients.fold_into_parent(bound_alias, parent_alias, client_id):
                folded.append((bound_alias, parent_alias))
                print(
                    f"NOTE: folded profile {bound_alias!r} into "
                    f"{parent_alias!r} as its "
                    f"{clients.client_name(client_id)} client; "
                    f"--profile {bound_alias} still works.",
                    file=sys.stderr,
                )
    except OSError:
        # A fold that cannot complete must not break token minting - the
        # bound profile still works standalone until the next attempt.
        return folded
    try:
        _config.ROOT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp.write_text("")
    except OSError:
        pass
    return folded
