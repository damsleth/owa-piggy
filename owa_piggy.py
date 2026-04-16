#!/usr/bin/env python3
"""
owa-piggy - get an Outlook/Graph access token without app registration

Piggybacks on OWA's first-party SPA client (One Outlook Web) to exchange
a refresh token for a fresh access token via Microsoft's OAuth2 endpoint.

No app registration. No client secret. Just the refresh token from your
browser's MSAL cache and the Origin header that makes AAD happy.

Usage:
  owa-piggy                        # print access token to stdout
  owa-piggy --json                 # print full response as JSON
  owa-piggy --remaining            # print minutes remaining on token
  owa-piggy --scope <scope>        # override default scope

Config (any of):
  Environment variables:           OWA_REFRESH_TOKEN, OWA_TENANT_ID
  ~/.config/owa-piggy/config       KEY=value file

One-time setup:
  1. Open https://outlook.cloud.microsoft in your browser
  2. Open DevTools (F12) > Console and run:
       const key = Object.keys(localStorage).find(k => k.includes('|refreshtoken|'))
       const token = JSON.parse(localStorage.getItem(key)).secret
       console.log(token)
  3. For your tenant ID, run:
       const key = Object.keys(localStorage).find(k => k.includes('|idtoken|'))
       const tenant = JSON.parse(localStorage.getItem(key)).realm
       console.log(tenant)
  4. owa-piggy --save-config
     or: export OWA_REFRESH_TOKEN=... OWA_TENANT_ID=...

Notes:
  - Refresh tokens are SPA-scoped: 24h sliding window, rotates on each use
  - New refresh token is saved automatically after each exchange
  - Default scope targets outlook.office.com (Calendars.ReadWrite + more)
  - Use --scope 'https://graph.microsoft.com/.default' for Graph
  - OWA is a FOCI client - the token works across Microsoft first-party APIs
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = '9199bf20-a13f-4107-85dc-02114787ef48'
ORIGIN = 'https://outlook.cloud.microsoft'
DEFAULT_SCOPE = 'https://outlook.office.com/Calendars.ReadWrite openid profile offline_access'
CONFIG_PATH = Path.home() / '.config' / 'owa-piggy' / 'config'


def load_config():
    config = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                config[k.strip()] = v.strip().strip('"')
    # Environment overrides file
    for key in ('OWA_REFRESH_TOKEN', 'OWA_TENANT_ID', 'OWA_CLIENT_ID'):
        if key in os.environ:
            config[key] = os.environ[key]
    return config


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = []
    if CONFIG_PATH.exists():
        # Preserve existing lines, update known keys
        existing_keys = set()
        for line in CONFIG_PATH.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith('#') and '=' in stripped:
                k = stripped.split('=', 1)[0].strip()
                if k in config:
                    lines.append(f'{k}="{config[k]}"')
                    existing_keys.add(k)
                    continue
            lines.append(line)
        for k, v in config.items():
            if k not in existing_keys:
                lines.append(f'{k}="{v}"')
    else:
        for k, v in config.items():
            lines.append(f'{k}="{v}"')
    CONFIG_PATH.write_text('\n'.join(lines) + '\n')
    CONFIG_PATH.chmod(0o600)


def exchange_token(refresh_token, tenant_id, client_id, scope):
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
            'Origin': ORIGIN,
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
        except Exception:
            print(f'ERROR: HTTP {e.code}: {err_body[:200]}', file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f'ERROR: {e.reason}', file=sys.stderr)
        return None


def read_input(prompt):
    """Read input in raw tty mode to bypass the terminal line-length limit (~4096 bytes).
    Strips embedded newlines so paste from browser console works regardless of wrapping."""
    print(prompt)
    sys.stdout.flush()
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        chars = []
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ('\r', '\n'):
                    if chars:
                        break  # Enter ends input once we have something
                elif ch == '\x03':
                    raise KeyboardInterrupt
                elif ch in ('\x7f', '\x08'):  # backspace / ctrl-H
                    if chars:
                        chars.pop()
                        sys.stdout.write('\b \b')
                        sys.stdout.flush()
                else:
                    chars.append(ch)
                    sys.stdout.write(ch)
                    sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()
        return ''.join(chars).strip()
    except (ImportError, Exception):
        return input().strip()


def interactive_setup(config):
    print('owa-piggy setup\n')
    print('1. Open https://outlook.cloud.microsoft in your browser')
    print('2. Open DevTools (F12) > Console')
    print('3. Run this to get your refresh token:\n')
    print('   const key = Object.keys(localStorage).find(k => k.includes(\'|refreshtoken|\'))')
    print('   const token = JSON.parse(localStorage.getItem(key)).secret')
    print('   console.log(token)\n')
    rt = read_input('Refresh token (starts with "1.AQ..."), then press Enter:')
    if not rt:
        print('ERROR: no refresh token provided', file=sys.stderr)
        return False

    print('\n4. Run this to get your tenant ID:\n')
    print('   const key = Object.keys(localStorage).find(k => k.includes(\'|idtoken|\'))')
    print('   const tenant = JSON.parse(localStorage.getItem(key)).realm')
    print('   console.log(tenant)\n')
    tid = read_input('Tenant ID (a UUID), then press Enter:')
    if not tid:
        print('ERROR: no tenant ID provided', file=sys.stderr)
        return False

    config['OWA_REFRESH_TOKEN'] = rt
    config['OWA_TENANT_ID'] = tid
    save_config(config)
    print(f'\nConfig saved to {CONFIG_PATH}')
    return True


def token_minutes_remaining(access_token):
    import base64
    import time
    try:
        payload_b64 = access_token.split('.')[1]
        payload_b64 += '=' * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int((payload.get('exp', 0) - time.time()) / 60)
    except Exception:
        return None


def print_help():
    print("""usage: owa-piggy [options]

  Piggybacks on OWA's first-party SPA client to get an Outlook/Graph
  access token without registering an app in Azure AD.

options:
  (none)            print access token to stdout
  --json            print full token response as JSON
  --remaining       print minutes remaining on the current token
  --scope <scope>   override default scope (default: outlook.office.com)
  --save-config     interactive first-time setup, saves to config file
  --setup           alias for --save-config
  --help            show this help

config:
  ~/.config/owa-piggy/config   KEY=value file (auto-created by --save-config)

  OWA_REFRESH_TOKEN   MSAL refresh token secret from browser localStorage
  OWA_TENANT_ID       your Azure AD tenant ID
  OWA_CLIENT_ID       override client ID (default: OWA's public client)

  Environment variables take precedence over the config file.

one-time setup:
  1. Open https://outlook.cloud.microsoft in your browser
  2. Open DevTools (F12) > Console
  3. Run to get your refresh token:
       const key = Object.keys(localStorage).find(k => k.includes('|refreshtoken|'))
       const token = JSON.parse(localStorage.getItem(key)).secret
       console.log(token)
  4. Run to get your tenant ID:
       const key = Object.keys(localStorage).find(k => k.includes('|idtoken|'))
       const tenant = JSON.parse(localStorage.getItem(key)).realm
       console.log(tenant)
  5. Run: owa-piggy --save-config

examples:
  owa-piggy                                         # raw token to stdout
  owa-piggy --remaining                             # 73min
  token=$(owa-piggy)                                # use in scripts
  owa-piggy --scope 'https://graph.microsoft.com/.default'
  owa-piggy --json | jq .scope

notes:
  - Refresh tokens have a 24h sliding window - use daily to keep alive
  - Rotated refresh token is saved automatically after each exchange
  - OWA is a FOCI client, so the token works across Microsoft first-party APIs""")


def main():
    args = sys.argv[1:]
    want_json = '--json' in args
    want_remaining = '--remaining' in args
    do_setup = '--save-config' in args or '--setup' in args

    if '--help' in args or '-h' in args:
        print_help()
        return 0

    scope = DEFAULT_SCOPE
    if '--scope' in args:
        idx = args.index('--scope')
        if idx + 1 < len(args):
            scope = args[idx + 1]
        else:
            print('ERROR: --scope requires a value', file=sys.stderr)
            return 1

    config = load_config()

    if do_setup:
        if not interactive_setup(config):
            return 1
        config = load_config()

    refresh_token = config.get('OWA_REFRESH_TOKEN', '').strip()
    tenant_id = config.get('OWA_TENANT_ID', '').strip()
    client_id = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()

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

    # Persist rotated refresh token
    if new_refresh:
        config['OWA_REFRESH_TOKEN'] = new_refresh
        save_config(config)

    if want_json:
        print(json.dumps(result, indent=2))
    elif want_remaining:
        remaining = token_minutes_remaining(access_token)
        print(f'{remaining}min' if remaining is not None else 'unknown')
    else:
        print(access_token)

    if do_setup:
        print('\n\tOWA-PIGGY 🐽  CONFIGURED!\n', file=sys.stderr)
        print('  REMEMBER TO REFRESH IN THE 24-HOUR WINDOW', file=sys.stderr)
        print('  TO INSTALL:', file=sys.stderr)
        print('    pipx install .           (recommended)', file=sys.stderr)
        print('    ./add-to-path.sh         (symlink to /usr/local/bin/)', file=sys.stderr)
        print('  RUN ./setup-cron.sh TO REFRESH THE TOKEN EVERY HOUR', file=sys.stderr)
        print('\n\tENJOY YOUR APP-REG-FREE SCOPES\n', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())
