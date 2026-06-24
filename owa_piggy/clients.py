"""Per-profile bound-client store: one identity, several minting clients.

A profile is a *user*, not a (user, client) pair. The FOCI refresh token
captured from OWA reaches most audiences, but some endpoints authorize on
the token's `appid` rather than its scopes, so they only answer the client
that owns the SPA:

- `teams.microsoft.com/api/authsvc/v1.0/authz` returns 410 ApiRestricted
  for every client except the Teams web app, which makes the Skype token
  (and with it chatsvc, the middle tier, and trouter) unreachable from an
  OWA-minted token no matter which scope it carries.
- The Azure DevOps app sits behind a preauth wall (AADSTS65002) the FOCI
  client cannot cross at all.

Both are the same problem: the audience is reachable, the *client* is not.
So a profile keeps its FOCI token in `config` (`OWA_REFRESH_TOKEN`) and any
additional client-bound refresh tokens in a sibling `clients.json`, all
captured through the one Edge sidecar session that profile already owns.
`select_for_scope` then routes each audience to the client that can serve
it, preferring a bound client when the profile has one.

Storage (mode 0600, atomically written, same as the config):

    profiles/<alias>/clients.json
    {
      "<client id>": {
        "refresh_token": "...",
        "origin": "https://teams.microsoft.com",
        "capture_url": "https://teams.microsoft.com/",
        "rt_issued_at": "2026-08-25T17:16:35Z"
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DEVOPS_CLIENT_ID, atomic_write, iso_utc_now, profile_dir

CLIENTS_FILENAME = "clients.json"


# Plain string maps rather than TypedDicts: every value is a string, the
# registry's only nuance is that `capture_url` may be absent, and entries are
# built by filtering empties - which a TypedDict rejects for no safety gained.
ClientMeta = dict[str, "str | None"]
ClientEntry = dict[str, str]

# Teams web app. The only client teams authsvc still answers, and a full
# FOCI-family member besides (verified: it mints graph, outlook, and every
# Teams audience), so routing an audience to it never costs reach.
TEAMS_WEB_CLIENT_ID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"


KNOWN_CLIENTS: dict[str, ClientMeta] = {
    TEAMS_WEB_CLIENT_ID: {
        "name": "teams",
        "origin": "https://teams.microsoft.com",
        "capture_url": "https://teams.microsoft.com/",
    },
    DEVOPS_CLIENT_ID: {
        "name": "devops",
        "origin": "https://dev.azure.com",
        "capture_url": None,
    },
}

# Clients `setup` / `reseed` capture without being asked. Only clients whose
# capture_url works for any tenant belong here.
DEFAULT_CLIENTS: tuple[str, ...] = (TEAMS_WEB_CLIENT_ID,)

# Audience URL -> the client that should serve it when the profile has that
# client's token. Everything absent here stays on the FOCI token.
AUDIENCE_CLIENT: dict[str, str] = {
    # The authsvc audience itself: this is the exchange that hands out the
    # Skype token, and the one that answers 410 ApiRestricted for anyone
    # but the Teams client. Callers that ask for it by scope rather than by
    # audience name (teaminal does) route here too.
    "https://teams.microsoft.com": TEAMS_WEB_CLIENT_ID,
    "https://api.spaces.skype.com": TEAMS_WEB_CLIENT_ID,
    "https://ic3.teams.office.com": TEAMS_WEB_CLIENT_ID,
    "https://chatsvcagg.teams.microsoft.com": TEAMS_WEB_CLIENT_ID,
    "https://presence.teams.microsoft.com": TEAMS_WEB_CLIENT_ID,
    "https://uis.teams.microsoft.com": TEAMS_WEB_CLIENT_ID,
    "https://app.vssps.visualstudio.com": DEVOPS_CLIENT_ID,
}


def client_id_for_name(name: str) -> str | None:
    """Resolve a short client name ('teams', 'devops') to its client id."""
    for cid, meta in KNOWN_CLIENTS.items():
        if meta["name"] == name:
            return cid
    return None


def client_name(client_id: str | None) -> str:
    """Short name for a client id, or the id itself when unknown."""
    meta = KNOWN_CLIENTS.get((client_id or "").strip())
    name = meta["name"] if meta else None
    return name or (client_id or "")


def clients_path(alias: str) -> Path:
    return profile_dir(alias) / CLIENTS_FILENAME


def load_clients(alias: str) -> dict[str, ClientEntry]:
    """Bound clients for a profile, or {} when there are none.

    A malformed store must never break token minting - the FOCI token in
    `config` still works, so we degrade to "no bound clients" rather than
    crashing every call until someone hand-edits the file.
    """
    path = clients_path(alias)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {cid: entry for cid, entry in data.items() if isinstance(entry, dict)}


def declare_client(
    alias: str,
    client_id: str,
    *,
    origin: str | None = None,
    capture_url: str | None = None,
) -> tuple[ClientEntry | None, str]:
    """Record that this profile uses `client_id`, before any token exists.

    The store doubles as the profile's site list: `reseed` walks it and
    opens each entry's `capture_url`, so declaring a client is how a
    profile says "I also sign in to Teams" or "...to this ADO org". A
    declaration with no refresh_token never routes (see
    `select_for_scope`) - it only tells reseed where to go.
    """
    meta = KNOWN_CLIENTS.get(client_id, {})
    clients = load_clients(alias)
    entry = dict(clients.get(client_id, {}))
    entry.setdefault("refresh_token", "")
    resolved_url = capture_url or entry.get("capture_url") or meta.get("capture_url")
    if not resolved_url:
        return None, (
            f"client {client_name(client_id)!r} needs an explicit capture URL "
            f"(its sign-in URL is org-specific). Pass "
            f"--with-client {client_name(client_id)}=<url>"
        )
    entry["capture_url"] = resolved_url
    entry["origin"] = origin or entry.get("origin") or meta.get("origin") or ""
    clients[client_id] = {k: v for k, v in entry.items() if v or k == "refresh_token"}
    atomic_write(clients_path(alias), json.dumps(clients, indent=2) + "\n")
    return clients[client_id], ""


def forget_client(alias: str, client_id: str) -> bool:
    """Drop a client from the profile's site list.

    Used when a default client turns out not to apply to this tenant: a
    declaration nobody can capture would otherwise cost an Edge launch and
    a capture timeout on every hourly reseed, forever.
    """
    clients = load_clients(alias)
    if client_id not in clients:
        return False
    del clients[client_id]
    atomic_write(clients_path(alias), json.dumps(clients, indent=2) + "\n")
    return True


def parse_spec(spec: str | None) -> tuple[str | None, str | None, str]:
    """Parse a `--with-client` value into `(client_id, capture_url, err)`.

    Accepts `teams`, `devops=https://dev.azure.com/org/proj/_workitems`, or
    a raw client id with an explicit URL for a client we don't know yet.
    """
    text = (spec or "").strip()
    if not text:
        return None, None, "empty --with-client value"
    name, _, url = text.partition("=")
    name, url = name.strip(), url.strip()
    client_id = client_id_for_name(name) or (name if len(name) == 36 else None)
    if not client_id:
        known = ", ".join(sorted(str(m["name"]) for m in KNOWN_CLIENTS.values()))
        return (
            None,
            None,
            (f"unknown client {name!r}; known names: {known} (or pass a client id with =<url>)"),
        )
    if client_id not in KNOWN_CLIENTS and not url:
        return None, None, f"client {name!r} needs an explicit =<url>"
    return client_id, url or None, ""


def capture_targets(alias: str) -> list[tuple[str, ClientEntry]]:
    """[(client_id, entry)] for every client this profile declares.

    The order is insertion order, so reseed rotates them in the order they
    were declared - deterministic, and the FOCI/OWA token is always done
    first by the caller.
    """
    return list(load_clients(alias).items())


def save_client(
    alias: str,
    client_id: str,
    *,
    refresh_token: str,
    origin: str | None = None,
    capture_url: str | None = None,
    rt_issued_at: str | None = None,
) -> ClientEntry:
    """Add or update one bound client, preserving the rest of the store.

    Read-modify-write rather than a whole-file rewrite: `reseed` rotates
    clients one at a time, and a crash between two of them must not drop
    the tokens already persisted.
    """
    clients = load_clients(alias)
    meta = KNOWN_CLIENTS.get(client_id, {})
    existing = clients.get(client_id, {})
    entry = {
        "refresh_token": refresh_token,
        "origin": origin or existing.get("origin") or meta.get("origin") or "",
        "capture_url": (
            capture_url or existing.get("capture_url") or meta.get("capture_url") or ""
        ),
        "rt_issued_at": rt_issued_at or iso_utc_now(),
    }
    clients[client_id] = {k: v for k, v in entry.items() if v}
    atomic_write(clients_path(alias), json.dumps(clients, indent=2) + "\n")
    return clients[client_id]


def audience_from_scope(scope: str | None) -> str:
    """Pull the audience URL out of a resolved scope string.

    `resolve_audience` returns '<audience>/.default openid profile
    offline_access'; an explicit --scope can be anything, in which case
    there is no audience to route on and we return ''.
    """
    if not scope:
        return ""
    first = scope.split()[0]
    if not first.endswith("/.default"):
        return ""
    return first[: -len("/.default")]


def select_for_scope(alias: str, scope: str) -> tuple[str | None, ClientEntry | None]:
    """Which bound client should mint `scope`, if any.

    Returns `(client_id, entry)` when the profile holds the client that
    owns this audience, else `(None, None)` so the caller keeps using the
    profile's FOCI token. Preferring the bound client is the point: an
    OWA-minted `api.spaces.skype.com` token is perfectly valid and still
    useless at authsvc, so falling back to it silently would reintroduce
    the exact failure this store exists to fix.
    """
    audience = audience_from_scope(scope)
    if not audience:
        return None, None
    client_id = AUDIENCE_CLIENT.get(audience)
    if not client_id:
        return None, None
    entry = load_clients(alias).get(client_id)
    if not entry or not entry.get("refresh_token"):
        return None, None
    return client_id, entry


def overlay_config(config: dict[str, str], client_id: str, entry: ClientEntry) -> dict[str, str]:
    """Config copy that mints as `client_id` instead of the FOCI client.

    A copy, not a mutation: the caller's `config` still describes the
    profile's FOCI token, and only the bound client's rotated token gets
    written back (via `save_client`, not `save_config`).
    """
    overlaid = dict(config)
    overlaid["OWA_CLIENT_ID"] = client_id
    overlaid["OWA_REFRESH_TOKEN"] = entry.get("refresh_token", "")
    origin = entry.get("origin") or KNOWN_CLIENTS.get(client_id, {}).get("origin")
    if origin:
        overlaid["OWA_ORIGIN"] = origin
    return overlaid


# --- folding a client-bound profile into its identity's profile --------


def _read_config_file(path: Path) -> dict[str, str]:
    from .config import _iter_kv

    try:
        return dict(_iter_kv(path.read_text()))
    except OSError:
        return {}


def fold_candidates() -> list[tuple[str, str, str]]:
    """[(bound_alias, parent_alias, client_id)] for profiles that are really
    one identity split across two profile dirs.

    A profile whose `OWA_CLIENT_ID` is a known bound client, sharing email
    and tenant with an ordinary FOCI profile, predates this store: the only
    reason it exists separately is that a profile used to hold exactly one
    client's token. It belongs in the FOCI profile's `clients.json`.
    """
    from .config import list_profiles, profile_config_path
    from .oauth import CLIENT_ID

    profiles = {}
    for alias in list_profiles():
        path = profile_config_path(alias)
        if not path.exists():
            continue
        # Raw file read, not load_config: env overrides (OWA_EMAIL,
        # OWA_TENANT_ID) would be applied to every profile alike and make
        # unrelated profiles look like the same identity.
        profiles[alias] = _read_config_file(path)

    def identity(cfg: dict[str, str]) -> tuple[str, str]:
        return (
            (cfg.get("OWA_EMAIL", "") or "").strip().lower(),
            (cfg.get("OWA_TENANT_ID", "") or "").strip().lower(),
        )

    out = []
    for alias, cfg in profiles.items():
        client_id = (cfg.get("OWA_CLIENT_ID", "") or "").strip()
        if not client_id or client_id == CLIENT_ID or client_id not in KNOWN_CLIENTS:
            continue
        if (cfg.get("OWA_FOLDED_INTO", "") or "").strip():
            continue
        if not cfg.get("OWA_REFRESH_TOKEN", "").strip():
            continue
        email, tenant = identity(cfg)
        if not email or not tenant:
            continue
        for parent, pcfg in profiles.items():
            if parent == alias:
                continue
            pclient = (pcfg.get("OWA_CLIENT_ID", "") or "").strip()
            if pclient and pclient != CLIENT_ID:
                continue
            if identity(pcfg) == (email, tenant):
                out.append((alias, parent, client_id))
                break
    return out


def fold_into_parent(bound_alias: str, parent_alias: str, client_id: str) -> bool:
    """Move `bound_alias`'s token into `parent_alias`'s client store and
    leave the old alias as a pointer at it.

    The token is moved, not copied: two profiles rotating the same refresh
    token independently is how one of them ends up holding a token AAD has
    already superseded. `OWA_FOLDED_INTO` makes the old alias keep working -
    every command that resolves it lands on the parent instead.
    """
    from .config import profile_config_path, save_config

    cfg = _read_config_file(profile_config_path(bound_alias))
    rt = (cfg.get("OWA_REFRESH_TOKEN", "") or "").strip()
    if not rt:
        return False
    save_client(
        parent_alias,
        client_id,
        refresh_token=rt,
        origin=(cfg.get("OWA_ORIGIN", "") or "").strip() or None,
        capture_url=(cfg.get("OWA_CAPTURE_URL", "") or "").strip() or None,
        rt_issued_at=(cfg.get("OWA_RT_ISSUED_AT", "") or "").strip() or None,
    )
    cfg["OWA_REFRESH_TOKEN"] = ""
    cfg["OWA_FOLDED_INTO"] = parent_alias
    save_config(cfg, profile_config_path(bound_alias))
    return True
