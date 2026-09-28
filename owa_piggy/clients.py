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
So a profile keeps its FOCI token in `settings.OWA_REFRESH_TOKEN` and any
additional client-bound refresh tokens in the `clients` array of the same
`config.json` (see config.py), all captured through the one Edge sidecar
session that profile already owns. `select_for_scope` then routes each
audience to the client that can serve it, preferring a bound client when
the profile has one.

Each record:

    {"name": "teams", "client_id": "5e3ce6c0-...", "enabled": true,
     "refresh_token": "...", "origin": "https://teams.microsoft.com",
     "capture_url": "https://teams.microsoft.com/",
     "rt_issued_at": "2026-08-25T17:16:35Z"}

Non-AAD services (halo, kova) and declared-only services (swodp) have no
`client_id`; their `name` is the key. In code, clients are addressed by that
key (client id, else name), so `load_clients` still returns
`{key: entry}` with the record's own fields. `enabled: false` keeps the
record and its token but takes it out of routing, reseed and `services`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config import (
    DEVOPS_CLIENT_ID,
    classify_profile_type,
    iso_utc_now,
    profile_config_path,
    read_doc,
    update_doc,
)
from .oauth import PIM_CLIENT_ID
from .scopes import PIM_PERMISSION

# clients.json key for the HaloITSM service. Not an AAD client id: Halo's own
# identity server mints the token, so the entry is keyed by service name.
HALO_KEY = "halo"

# Kova (Red Cross volunteer schedule): a server-rendered app that signs in
# through Okta and knows the user only by its session cookie. There is no
# token to store; reseed keeps the sidecar's session alive and pins its
# session cookies (capture.capture_session), and owa-kova reads them from the
# sidecar on demand, the way owa-swodp reads ServiceNow's.
KOVA_KEY = "kova"
SESSION_SERVICES: tuple[str, ...] = (KOVA_KEY,)

# Services with nothing for owa-piggy to capture or mint: the record only
# says "this user uses it" so consumers can fan out on it. owa-swodp reads
# ServiceNow's session straight from this profile's sidecar on each call.
DECLARED_SERVICES: tuple[str, ...] = ("swodp",)


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
    PIM_CLIENT_ID: {
        "name": "pim",
        "origin": None,
        "capture_url": None,
    },
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
    # Not an AAD client: Halo's own identity server mints its tokens, and
    # the capture reads them from a cookie (capture.capture_halo). Listed
    # here so `clients add halo=<url>` and reseed treat it like the rest.
    HALO_KEY: {
        "name": "halo",
        "origin": None,
        "capture_url": None,
    },
    KOVA_KEY: {
        "name": "kova",
        "origin": None,
        "capture_url": "https://www.kova.no",
    },
    "swodp": {
        "name": "swodp",
        "origin": None,
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


# Record fields that describe the record rather than the credential.
_RECORD_META = ("name", "client_id", "enabled")

# Consumer-facing service name for a client whose short name differs.
_SERVICE_NAMES = {"devops": "ado"}

# Clients that route audiences inside owa-owned tools rather than being a
# service a consumer fans out on.
_ROUTING_ONLY = frozenset({"teams", "pim"})


def record_key(record: dict[str, Any]) -> str:
    return str(record.get("client_id") or record["name"])


def new_record(key: str, entry: ClientEntry | None = None) -> dict[str, Any]:
    """A fresh `clients[]` record for `key` (client id or service name)."""
    name = client_name(key)
    record: dict[str, Any] = {"name": name}
    if name != key or key not in (HALO_KEY, *SESSION_SERVICES) and len(key) == 36:
        record["client_id"] = key
    record["enabled"] = True
    record.update({k: v for k, v in (entry or {}).items() if k not in _RECORD_META})
    return record


def service_for(record: dict[str, Any]) -> str:
    """The consumer service a record provides ('' when none)."""
    name = str(record.get("name") or "")
    if name in _ROUTING_ONLY or (record.get("client_id") and name == record["client_id"]):
        return ""
    return _SERVICE_NAMES.get(name, name)


def load_records(alias: str) -> list[dict[str, Any]]:
    """Every `clients[]` record, disabled ones included (dashboard, CLI)."""
    return list(read_doc(profile_config_path(alias))["clients"])


def load_clients(alias: str, *, include_disabled: bool = False) -> dict[str, ClientEntry]:
    """Bound clients for a profile as `{key: entry}`, or {} when there are none.

    Disabled records are left out unless asked for, so routing, reseed and
    `services` honour the toggle without each checking it. A malformed file
    degrades to "no bound clients" (read_doc warns) - the FOCI token still
    works, so minting must not break until someone fixes the file.
    """
    return {
        record_key(r): {k: v for k, v in r.items() if k not in _RECORD_META}
        for r in load_records(alias)
        if include_disabled or r.get("enabled", True) is not False
    }


def _put(alias: str, key: str, fields: dict[str, str], *, replace: bool) -> ClientEntry:
    """Locked upsert of one record. `replace` swaps the credential fields
    wholesale (keeping name/client_id/enabled); otherwise they merge."""

    def apply(doc: dict[str, Any]) -> ClientEntry:
        for record in doc["clients"]:
            if record_key(record) == key:
                if replace:
                    for k in [k for k in record if k not in _RECORD_META]:
                        del record[k]
                record.update(fields)
                break
        else:
            record = new_record(key, fields)
            doc["clients"].append(record)
        return {k: v for k, v in record.items() if k not in _RECORD_META}

    return update_doc(profile_config_path(alias), apply)


def declare_service(alias: str, name: str) -> ClientEntry:
    """Record a declared-only service (see DECLARED_SERVICES)."""
    return _put(alias, name, {}, replace=False)


def set_client_enabled(alias: str, key: str, enabled: bool) -> bool:
    """Toggle one record's `enabled`. False when the profile has no such client."""

    def apply(doc: dict[str, Any]) -> bool:
        for record in doc["clients"]:
            if record_key(record) == key:
                record["enabled"] = enabled
                return True
        return False

    return update_doc(profile_config_path(alias), apply)


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
    if client_id == PIM_CLIENT_ID:
        return None, f"PIM requires device sign-in: owa-piggy clients add pim --profile {alias}"
    meta = KNOWN_CLIENTS.get(client_id, {})
    entry = dict(load_clients(alias, include_disabled=True).get(client_id, {}))
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
    fields = {k: v for k, v in entry.items() if v or k == "refresh_token"}
    return _put(alias, client_id, fields, replace=True), ""


def forget_client(alias: str, client_id: str) -> bool:
    """Drop a client from the profile's site list.

    Used when a default client turns out not to apply to this tenant: a
    declaration nobody can capture would otherwise cost an Edge launch and
    a capture timeout on every hourly reseed, forever.
    """

    def apply(doc: dict[str, Any]) -> bool:
        kept = [r for r in doc["clients"] if record_key(r) != client_id]
        removed = len(kept) != len(doc["clients"])
        doc["clients"] = kept
        return removed

    return update_doc(profile_config_path(alias), apply)


def normalize_capture_url(client_id: str, value: str | None) -> str | None:
    """Turn what a human typed into the URL the sidecar should open.

    Azure DevOps is the reason this exists: its sign-in URL is org-specific,
    but nobody should have to remember
    `https://dev.azure.com/<org>/<project>/_workitems` to answer a prompt. A
    bare org name is enough, a full URL is passed through untouched, and
    anything else falls back to the client's default.
    """
    text = (value or "").strip().rstrip("/")
    if not text:
        return KNOWN_CLIENTS.get(client_id, {}).get("capture_url")
    if text.startswith(("http://", "https://")):
        return text
    if client_id == DEVOPS_CLIENT_ID:
        # Bare org name, or `org/project`.
        return f"https://dev.azure.com/{text.lstrip('/')}"
    return text


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
    if client_id == PIM_CLIENT_ID and url:
        return None, None, "PIM uses device sign-in and does not accept a capture URL"
    if client_id in DECLARED_SERVICES and url:
        return None, None, f"{name} is declared only and does not take a URL"
    if client_id not in KNOWN_CLIENTS and not url:
        return None, None, f"client {name!r} needs an explicit =<url>"
    return client_id, normalize_capture_url(client_id, url), ""


def capture_targets(alias: str) -> list[tuple[str, ClientEntry]]:
    """[(client_id, entry)] for every client this profile declares.

    The order is insertion order, so reseed rotates them in the order they
    were declared - deterministic, and the FOCI/OWA token is always done
    first by the caller.
    """
    # Native PIM credentials refresh on demand. Scheduled SPA reseeds must
    # never start an interactive device-code sign-in. Declared-only services
    # (swodp) have no capture_url: nothing to capture.
    return [
        (cid, entry)
        for cid, entry in load_clients(alias).items()
        if cid != PIM_CLIENT_ID and entry.get("capture_url")
    ]


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
    meta = KNOWN_CLIENTS.get(client_id, {})
    existing = load_clients(alias, include_disabled=True).get(client_id, {})
    entry = {
        "refresh_token": refresh_token,
        "origin": origin or existing.get("origin") or meta.get("origin") or "",
        "capture_url": (
            capture_url or existing.get("capture_url") or meta.get("capture_url") or ""
        ),
        "rt_issued_at": rt_issued_at or iso_utc_now(),
    }
    return _put(alias, client_id, {k: v for k, v in entry.items() if v}, replace=True)


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
    if PIM_PERMISSION in scope.split():
        # An empty entry deliberately prevents fallback to the OWA family RT.
        return PIM_CLIENT_ID, load_clients(alias).get(PIM_CLIENT_ID, {})
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


def profile_services(alias: str, config: dict[str, str]) -> list[str]:
    """The services this profile's user signs in to, for consumers that fan
    out per service (owa-tools `-A`: `owa-ado -A` only hits profiles with
    `ado`, `owa-halo` picks the profile with `halo`).

    Derived from the enabled `clients[]` records, in their order: `owa` for
    any AAD profile, then each record's service once it has what it needs -
    a refresh token (ado, halo), a completed capture stamp for session
    services (kova), nothing at all for declared-only services (swodp reads
    its session straight from the sidecar on each call). teams/pim route
    audiences inside owa and are not services.
    """
    ptype = classify_profile_type(config)
    if ptype != "m365":
        return [ptype]
    services = ["owa"]
    for record in load_records(alias):
        service = service_for(record)
        if not service or record.get("enabled", True) is False or service in services:
            continue
        if service in SESSION_SERVICES:
            ready = bool(record.get("rt_issued_at"))
        elif record.get("client_id") or service == HALO_KEY:
            ready = bool(record.get("refresh_token"))
        else:
            ready = True
        if ready:
            services.append(service)
    return services


def overlay_config(config: dict[str, str], client_id: str, entry: ClientEntry) -> dict[str, str]:
    """Config copy that mints as `client_id` instead of the FOCI client.

    A copy, not a mutation: the caller's `config` still describes the
    profile's FOCI token, and only the bound client's rotated token gets
    written back (via `save_client`, not `save_config`).
    """
    overlaid = dict(config)
    overlaid["OWA_CLIENT_ID"] = client_id
    overlaid["OWA_REFRESH_TOKEN"] = entry.get("refresh_token", "")
    if client_id == PIM_CLIENT_ID:
        overlaid.pop("OWA_ORIGIN", None)
        overlaid["OWA_RT_ISSUED_AT"] = entry.get("rt_issued_at", "")
        return overlaid
    origin = entry.get("origin") or KNOWN_CLIENTS.get(client_id, {}).get("origin")
    if origin:
        overlaid["OWA_ORIGIN"] = origin
    return overlaid


def overlay_halo(config: dict[str, str], entry: ClientEntry) -> dict[str, str]:
    """Config copy that mints the profile's Halo token instead of its FOCI one.

    Shaped like a standalone Halo profile (OWA_PROVIDER=halo, the Halo host
    in OWA_TENANT_ID), so token_flow's Halo exchange and the (host, client,
    scope) cache key apply unchanged. The host comes from the entry's
    capture_url: the same URL the sidecar signs in at.
    """
    from urllib.parse import urlsplit

    from .oauth_halo import HALO_CLIENT_ID

    overlaid = dict(config)
    overlaid["OWA_PROVIDER"] = "halo"
    overlaid["OWA_TENANT_ID"] = urlsplit(entry.get("capture_url", "")).netloc
    overlaid["OWA_CLIENT_ID"] = HALO_CLIENT_ID
    overlaid["OWA_REFRESH_TOKEN"] = entry.get("refresh_token", "")
    overlaid.pop("OWA_ORIGIN", None)
    return overlaid


def pim_exchange_config(
    alias: str,
    config: dict[str, str],
    scope: str,
) -> tuple[dict[str, str], Callable[[str], None] | None]:
    """Apply PIM isolation to diagnostic exchanges as well as token minting."""
    if PIM_PERMISSION not in scope.split():
        return config, None
    entry = load_clients(alias).get(PIM_CLIENT_ID, {})
    if not entry.get("refresh_token"):
        raise ValueError(f"PIM needs sign-in: owa-piggy clients add pim --profile {alias}")

    def sink(refresh_token: str) -> None:
        save_client(alias, PIM_CLIENT_ID, refresh_token=refresh_token)

    return overlay_config(config, PIM_CLIENT_ID, entry), sink
