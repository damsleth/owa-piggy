"""Derive a tenant's SharePoint hostname from Microsoft Graph.

The SharePoint admin/content URL embeds the tenant's SharePoint name
(e.g. ``norconsult365``), which is the tenant's initial ``.onmicrosoft.com``
prefix - NOT derivable from the user's email domain (``norconsult.com``)
nor the AAD tenant GUID. Graph's ``/sites/root`` returns the root site
collection's hostname (``norconsult365.sharepoint.com``) directly, so we
read it there and strip the ``.sharepoint.com`` suffix rather than
assuming the SharePoint prefix equals the onmicrosoft prefix.

This needs only a Graph access token, which any owa-piggy profile can
already mint from its FOCI refresh token - the same token family used for
the SharePoint audience itself. Stays urllib-only to honour the suite's
no-third-party-runtime-dependency axiom.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import save_config
from .scopes import resolve_audience
from .token_flow import exchange_fresh

_SITES_ROOT = "https://graph.microsoft.com/v1.0/sites/root?$select=siteCollection"
_SP_SUFFIX = ".sharepoint.com"


def derive_sharepoint_tenant(config, *, persist):
    """Mint a Graph token for the profile in ``config`` and read the
    tenant's SharePoint hostname prefix from ``/sites/root``.

    Returns ``(tenant, err)``. On success ``tenant`` is e.g.
    ``norconsult365`` and ``err`` is ''. On failure ``tenant`` is '' and
    ``err`` explains why (the caller can fall back to its own
    needs-a-tenant message).

    Side effect: when ``persist`` is True and derivation succeeds,
    ``config['OWA_SHAREPOINT_TENANT']`` is written and saved to disk so
    every later call skips this Graph round-trip. ``exchange_fresh`` may
    also rotate+persist the refresh token as usual.
    """
    graph_scope, scope_err = resolve_audience(audience="graph")
    if scope_err:
        return "", scope_err
    result, info = exchange_fresh(config, graph_scope, persist=persist, capture_stderr=True)
    if not result:
        suffix = f" ({info['aad_error']})" if info.get("aad_error") else ""
        return "", f"could not mint a Graph token to derive the SharePoint tenant{suffix}"

    req = urllib.request.Request(
        _SITES_ROOT,
        headers={
            "Authorization": f"Bearer {result.get('access_token')}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return "", f"Graph /sites/root returned HTTP {e.code} while deriving SharePoint tenant"
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        return "", f"Graph /sites/root request failed while deriving SharePoint tenant: {e}"

    host = (body.get("siteCollection") or {}).get("hostname", "") or ""
    if not host.endswith(_SP_SUFFIX) or len(host) <= len(_SP_SUFFIX):
        return "", f"unexpected SharePoint hostname {host!r} from Graph /sites/root"
    tenant = host[: -len(_SP_SUFFIX)]

    if persist:
        config["OWA_SHAREPOINT_TENANT"] = tenant
        save_config(config)
    return tenant, ""
