"""The one HTTP call: refresh_token -> access_token at AAD.

Do not change CLIENT_ID, ORIGIN, or the Content-Type header without a
very clear reason. Those values make AAD accept the request; changing
them silently breaks the tool.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = '9199bf20-a13f-4107-85dc-02114787ef48'
ORIGIN = 'https://outlook.cloud.microsoft'

# AAD's cross-origin check (AADSTS9002327) ties an SPA refresh-token grant
# to an Origin registered on that client's app registration. The default
# Outlook origin works for the default Teams Web client (9199bf20); the
# Teams web app (5e3ce6c0) is registered against a Teams origin instead.
# Callers can override per-profile via OWA_ORIGIN; this map supplies the
# right default when only OWA_CLIENT_ID is set.
KNOWN_CLIENT_ORIGINS = {
    '9199bf20-a13f-4107-85dc-02114787ef48': 'https://outlook.cloud.microsoft',
    '5e3ce6c0-2b1f-4285-8d4b-75ee78787346': 'https://teams.microsoft.com',
}


def origin_for_client(client_id, override=None):
    """Resolve the Origin header for a token exchange. An explicit
    override (OWA_ORIGIN) wins; otherwise fall back to the per-client
    default, then the global Outlook origin."""
    if override:
        return override
    return KNOWN_CLIENT_ORIGINS.get(client_id, ORIGIN)


def exchange_token(refresh_token, tenant_id, client_id, scope, origin=None):
    url = f'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token'
    data = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'client_id': client_id,
        'refresh_token': refresh_token,
        'scope': scope,
    }).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            # SPA clients require Origin to satisfy AAD's cross-origin check (AADSTS9002327)
            'Origin': origin_for_client(client_id, origin),
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='replace')
        try:
            err = json.loads(err_body)
            code = err.get('error', '')
            desc = err.get('error_description', '').split('\r\n')[0]
            print(f'ERROR: {code}: {desc}', file=sys.stderr)
            # AADSTS700084 is the 24h SPA hard-expiry: the refresh token has
            # hit its absolute lifetime ceiling (not the sliding window) and
            # cannot be extended by any amount of hourly rotation. The only
            # remedy is a fresh token from a live browser session. Point the
            # user at the automated reseed path so they are not left parsing
            # AAD error codes to figure out what to do next.
            if 'AADSTS700084' in err_body:
                print('hint: refresh token has hit its 24h SPA hard-expiry. '
                      'Run `owa-piggy reseed` to fetch a fresh token '
                      'headlessly from the Edge sidecar profile.',
                      file=sys.stderr)
            elif 'AADSTS70043' in err_body:
                # The tenant-side Conditional Access sign-in-frequency cap
                # (typically 7 days). Same recovery path as 700084, but the
                # error code is different so we surface it explicitly.
                print('hint: refresh token expired by Conditional Access '
                      'sign-in-frequency policy. Run `owa-piggy reseed` to '
                      'fetch a fresh token headlessly from the Edge sidecar '
                      'profile (Edge must still have a live tenant session).',
                      file=sys.stderr)
        except Exception:
            print(f'ERROR: HTTP {e.code}: {err_body[:200]}', file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f'ERROR: {e.reason}', file=sys.stderr)
        return None
