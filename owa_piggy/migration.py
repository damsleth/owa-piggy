"""One-shot folding of client-bound profiles into their identity's profile.

See fold_bound_clients_if_needed.
"""

from __future__ import annotations

import sys

from . import config as _config

FOLD_STAMP = "fold-bound-clients.done"


def fold_bound_clients_if_needed() -> list[tuple[str, str]]:
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
