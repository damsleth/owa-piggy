"""Argument parsing and dispatch for the `owa-piggy` command."""
import json
import sys

from .config import load_config, save_config
from .jwt import decode_jwt, token_minutes_remaining
from .oauth import CLIENT_ID, exchange_token
from .reseed import do_reseed
from .scopes import KNOWN_SCOPES, resolve_scope
from .setup import interactive_setup
from .status import do_debug, do_status


def print_help():
    print("""usage: owa-piggy [options]

  Piggybacks on OWA's first-party SPA client to get an Outlook/Graph
  access token without registering an app in Azure AD.

options:
  (none)            print access token to stdout
  --json            print full token response as JSON
  --env             print ACCESS_TOKEN and EXPIRES_IN as KEY=value lines
  --decode          decode and print the JWT header and payload
  --remaining       print minutes remaining on the current token
  --graph           use Microsoft Graph scope
  --teams           use Microsoft Teams scope
  --<name>          use any known FOCI scope (see --list-scopes)
  --list-scopes     list all known FOCI-accessible audiences
  --scope <scope>   override scope explicitly (takes precedence)
  --save-config     interactive first-time setup, saves to config file
  --setup           alias for --save-config
  --reseed          fetch a fresh refresh token headlessly from the Edge
                    sidecar profile (for when the 24h SPA hard-expiry hits)
  --status          compact health: authtoken/scope/refreshtoken expiries
                    (ISO8601), or 'no valid token' if setup is broken
  --debug           dump setup diagnostics: config, token shape, live probe,
                    launchd agent, PATH install, sidecar profile
  --help            show this help

config:
  ~/.config/owa-piggy/config   KEY=value file (auto-created by --save-config)

  OWA_REFRESH_TOKEN      MSAL refresh token secret from browser localStorage
  OWA_TENANT_ID          your Azure AD tenant ID
  OWA_CLIENT_ID          override client ID (default: OWA's public client)
  OWA_DEFAULT_AUDIENCE   override default audience (short name from
                         --list-scopes, e.g. 'outlook', or a full https URL).
                         --<name> / --scope on the command line still win.

  Environment variables take precedence over the config file.

one-time setup:
  1. Open https://outlook.cloud.microsoft in your browser
  2. Open DevTools (F12) > Console
  3. Paste this snippet to print both values (use Microsoft Edge - other
     Chromium browsers store a session-bound token AAD rejects):
       const find = s => Object.keys(localStorage).find(k => k.includes(s))
       const parse = s => JSON.parse(localStorage[find(s)])
       const rt = parse('|refreshtoken|'), it = parse('|idtoken|')
       if (!rt.secret) console.warn('WARN: non-MSAL shape; seed from Edge.')
       console.log('OWA_REFRESH_TOKEN=' + (rt.secret || rt.data))
       console.log('OWA_TENANT_ID=' + (it.realm || find('|idtoken|').split('|')[5]))
  4. Run: owa-piggy --save-config

examples:
  owa-piggy                                         # raw token to stdout
  owa-piggy --remaining                             # 73min
  token=$(owa-piggy)                                # use in scripts
  owa-piggy --graph                                 # Microsoft Graph token
  owa-piggy --teams                                 # Teams token
  owa-piggy --list-scopes                           # show all FOCI audiences
  owa-piggy --scope 'https://graph.microsoft.com/.default'
  owa-piggy --json | jq .scope
  eval $(owa-piggy --env)                           # export into shell
  owa-piggy --decode                                # inspect JWT claims
  owa-piggy --status                                # ISO8601 health summary
  owa-piggy --debug                                 # full diagnostics
  owa-piggy --reseed                                # automated token refresh
  pbpaste | owa-piggy --save-config                 # pipe tokens from clipboard

notes:
  - Default audience is Microsoft Graph (superset of Outlook REST plus
    OneDrive, Teams, SharePoint, directory). Set OWA_DEFAULT_AUDIENCE to
    change it persistently; --outlook and friends still work per-call.
  - Refresh tokens have TWO expiry rules:
      * 24h sliding window (rotates on every use)
      * 24h absolute hard-cap from original sign-in (AADSTS700084)
    The launchd agent handles the first; --reseed handles the second.
  - Rotated refresh token is saved automatically after each exchange
  - OWA is a FOCI client, so the token works across Microsoft first-party APIs""")


def main():
    args = sys.argv[1:]
    want_json = '--json' in args
    want_env = '--env' in args
    want_decode = '--decode' in args
    want_remaining = '--remaining' in args
    do_setup = '--save-config' in args or '--setup' in args

    if '--help' in args or '-h' in args:
        print_help()
        return 0

    # --reseed shells out to the Edge-headless reseed script and exits with
    # its status. Handled before load_config so an expired on-disk token
    # cannot block recovery.
    if '--reseed' in args:
        return do_reseed()

    if '--debug' in args:
        return do_debug()

    if '--status' in args:
        return do_status()

    if '--list-scopes' in args:
        max_name = max(len(n) for n in KNOWN_SCOPES)
        max_aud = max(len(aud) for aud, _ in KNOWN_SCOPES.values())
        for name, (aud, desc) in KNOWN_SCOPES.items():
            flag = f'--{name}'
            print(f'  {flag:<{max_name + 3}} {aud:<{max_aud + 2}} {desc}')
        return 0

    scope, err = resolve_scope(args)
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1

    config, persist = load_config()

    if do_setup:
        if not interactive_setup(config):
            return 1
        config, persist = load_config()

    refresh_token = config.get('OWA_REFRESH_TOKEN', '').strip()
    tenant_id = config.get('OWA_TENANT_ID', '').strip()
    client_id = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()

    # Sanity-check the token shape before we ship it to AAD. Real FOCI refresh
    # tokens are "{version}.{base64url payload}" with version 0 or 1. Plain
    # Chromium browsers (Vivaldi/Brave/Chrome) store a session-bound opaque
    # token at MSAL's cache location that lacks this prefix and that AAD
    # rejects as malformed. Fail fast with an actionable message instead of
    # letting AADSTS9002313 confuse the user.
    if refresh_token and not (refresh_token.startswith('1.') or refresh_token.startswith('0.')):
        print('ERROR: OWA_REFRESH_TOKEN does not look like an AAD FOCI refresh '
              'token (expected "1.AQ..." or "0.AQ..."). Plain Chromium browsers '
              'store a session-bound token that AAD will not accept. Reseed '
              'from Microsoft Edge via `owa-piggy --setup`.', file=sys.stderr)
        return 1

    if not refresh_token or not tenant_id:
        if not refresh_token:
            print('ERROR: OWA_REFRESH_TOKEN not set. Run: owa-piggy --save-config', file=sys.stderr)
        if not tenant_id:
            print('ERROR: OWA_TENANT_ID not set. Run: owa-piggy --save-config', file=sys.stderr)
        return 1

    result = exchange_token(refresh_token, tenant_id, client_id, scope)
    if not result:
        return 1

    access_token = result.get('access_token')
    new_refresh = result.get('refresh_token')

    if not access_token:
        print(f'ERROR: no access_token in response: {list(result.keys())}', file=sys.stderr)
        return 1

    # Persist rotated refresh token only when the original came from the config
    # file. Env-only callers stay env-only; writing to disk would silently turn
    # non-persistent usage into persistent secret storage.
    if new_refresh:
        config['OWA_REFRESH_TOKEN'] = new_refresh
        if persist:
            save_config(config)
        elif new_refresh != refresh_token:
            print('NOTE: refresh token rotated; OWA_REFRESH_TOKEN was env-only so '
                  'the new token was not written to disk. Update your environment '
                  'or run `owa-piggy --save-config` to persist.', file=sys.stderr)

    if want_json:
        print(json.dumps(result, indent=2))
    elif want_env:
        print(f'ACCESS_TOKEN={access_token}')
        print(f'EXPIRES_IN={result.get("expires_in", "")}')
    elif want_decode:
        print(decode_jwt(access_token))
    elif want_remaining:
        remaining = token_minutes_remaining(access_token)
        print(f'{remaining}min' if remaining is not None else 'unknown')
    else:
        print(access_token)

    if do_setup:
        print('\n\tOWA-PIGGY 🐽  CONFIGURED', file=sys.stderr)
        print('\n\tENJOY YOUR APP-REG-FREE SCOPES\n', file=sys.stderr)

    return 0
