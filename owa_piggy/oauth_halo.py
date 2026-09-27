"""HaloITSM: refresh_token -> access_token against a tenant's Halo identity server.

Halo agent sign-in goes through Entra ID SSO, but Entra is only the IdP:
Halo's own identity server (`https://<tenant>.haloitsm.com/auth`) mints
the session, and its access tokens are opaque 43-char bearer strings,
not JWTs. So no owa-piggy AAD token reaches Halo; the profile instead
holds Halo's own refresh token (pasted from the agent web app's
`refresh_token` cookie) and exchanges it here.

`HALO_CLIENT_ID` is the Halo agent web app's public client id, hardcoded in
its SPA bundle and the same on every tenant. The grant needs no secret.
Verified live against norconsult.haloitsm.com on 2026-09-25: 200 with a
3600s access token, and the refresh token did not rotate.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

HALO_CLIENT_ID = "24fe0a24-85d5-46d4-b9c6-721e23f25843"
HALO_SCOPE = "all offline_access"
EXCHANGE_TIMEOUT = 15

# The response also carries an id_token (user claims). Consumers only need
# the bearer; `token --json` echoes the response dict, so keep it narrow.
_KEEP = ("access_token", "token_type", "expires_in", "scope", "refresh_token")


def normalize_host(value: str) -> str:
    """`norconsult`, `norconsult.haloitsm.com` or a full URL -> bare host."""
    value = value.strip()
    if "://" in value:
        value = urllib.parse.urlsplit(value).netloc
    value = value.strip("/")
    if value and "." not in value:
        value = f"{value}.haloitsm.com"
    return value


def refresh_access_token(host: str, refresh_token: str) -> dict[str, Any] | None:
    """Refresh_token grant. Returns the narrowed token dict, or None on
    failure (error already printed to stderr, matching oauth.exchange_token)."""
    base = f"https://{normalize_host(host)}"
    url = f"{base}/auth/token?Redirect_Uri={urllib.parse.quote(base + '/auth', safe='')}"
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": HALO_CLIENT_ID,
            "scope": HALO_SCOPE,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=EXCHANGE_TIMEOUT) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            detail = f"{err.get('error', '?')}: {err.get('error_description', '')}"
        except json.JSONDecodeError:
            detail = f"HTTP {e.code}"
        print(f"ERROR: Halo token exchange failed ({detail})", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERROR: Halo token exchange failed ({getattr(e, 'reason', e)})", file=sys.stderr)
        return None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        print("ERROR: Halo token exchange returned no access_token", file=sys.stderr)
        return None
    return {k: tokens[k] for k in _KEEP if k in tokens}
