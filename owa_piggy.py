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
  owa-piggy --env                  # print ACCESS_TOKEN=... EXPIRES_IN=...
  owa-piggy --decode               # decode and print the JWT header + payload
  owa-piggy --remaining            # print minutes remaining on token
  owa-piggy --graph                # get a Microsoft Graph token
  owa-piggy --teams                # get a Teams token
  owa-piggy --list-scopes          # show all FOCI audiences
  owa-piggy --scope <scope>        # override scope explicitly

Config (any of):
  Environment variables:           OWA_REFRESH_TOKEN, OWA_TENANT_ID
  ~/.config/owa-piggy/config       KEY=value file

One-time setup:
  1. Open https://outlook.cloud.microsoft in your browser
  2. Use Microsoft Edge (plain Chromium browsers like Vivaldi/Brave/Chrome
     store a session-bound token in `.data` that AAD will reject as malformed;
     Edge integrates with the MS SSO broker and stores a real FOCI refresh
     token in `.secret`). Open DevTools (F12) > Console and run:
       const find = s => Object.keys(localStorage).find(k => k.includes(s))
       const parse = s => JSON.parse(localStorage[find(s)])
       const rt = parse('|refreshtoken|'), it = parse('|idtoken|')
       if (!rt.secret) console.warn('WARN: non-MSAL shape; seed from Edge.')
       console.log('OWA_REFRESH_TOKEN=' + (rt.secret || rt.data))
       console.log('OWA_TENANT_ID=' + (it.realm || find('|idtoken|').split('|')[5]))
  3. owa-piggy --save-config
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

# Well-known FOCI-accessible audiences (same refresh token works for all)
KNOWN_SCOPES = {
    'outlook':    ('https://outlook.office.com',                   'Outlook REST (default)'),
    'graph':      ('https://graph.microsoft.com',                  'Microsoft Graph'),
    'teams':      ('https://api.spaces.skype.com',                 'Microsoft Teams'),
    'azure':      ('https://management.azure.com',                 'Azure Resource Manager'),
    'keyvault':   ('https://vault.azure.net',                      'Azure Key Vault'),
    'storage':    ('https://storage.azure.com',                    'Azure Blob/Table/Queue Storage'),
    'sql':        ('https://database.windows.net',                 'Azure SQL'),
    'outlook365': ('https://outlook.office365.com',                'Outlook REST (alternate)'),
    'substrate':  ('https://substrate.office.com',                 'Office Substrate (Copilot, search)'),
    'manage':     ('https://manage.office.com',                    'Office Management API'),
    'powerbi':    ('https://analysis.windows.net/powerbi/api',     'Power BI'),
    'flow':       ('https://service.flow.microsoft.com',           'Power Automate'),
    'devops':     ('https://app.vssps.visualstudio.com',           'Azure DevOps'),
}
CONFIG_PATH = Path.home() / '.config' / 'owa-piggy' / 'config'


def load_config():
    """Returns (config, persist). persist is True only when OWA_REFRESH_TOKEN
    came from the on-disk config; env-only callers keep env-only semantics so
    rotated tokens are never silently written to disk."""
    config = {}
    file_keys = set()
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                k = k.strip()
                config[k] = v.strip().strip('"')
                file_keys.add(k)
    # Environment overrides file
    for key in ('OWA_REFRESH_TOKEN', 'OWA_TENANT_ID', 'OWA_CLIENT_ID'):
        if key in os.environ:
            config[key] = os.environ[key]
    persist = 'OWA_REFRESH_TOKEN' in file_keys
    return config, persist


def save_config(config):
    """Atomically rewrite the config file.

    Refresh tokens rotate on every successful exchange, so a partial write here
    would corrupt the only live token and force the user to reseed from the
    browser. Write the new contents to a sibling temp file, fsync, chmod, then
    rename over the target - rename within a filesystem is atomic on POSIX, so
    either the old or the new file is visible, never a truncated mix.
    """
    import tempfile
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
    payload = '\n'.join(lines) + '\n'

    fd, tmp_path = tempfile.mkstemp(
        prefix='.config.', suffix='.tmp', dir=str(CONFIG_PATH.parent)
    )
    tmp = Path(tmp_path)
    try:
        os.chmod(tmp, 0o600)  # apply perms before the file holds any secret
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


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


def read_input(prompt, secret=False):
    """Read input in raw tty mode to bypass the terminal line-length limit (~4096 bytes).

    Modern terminals wrap pasted text with bracketed-paste escape sequences
    (ESC [200~ ... ESC [201~). In cooked mode the terminal strips these; in
    raw mode they leak through as literal bytes and corrupt the payload, which
    for a refresh token means AAD rejects the exchange as malformed. We detect
    the BP start/end sequences and drop them, and strip any stray CSI escape.

    When secret=True, characters are not echoed and backspace does not emit
    visual feedback."""
    import re
    print(prompt)
    sys.stdout.flush()
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        chars = []
        in_paste = False
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                # Handle CSI escape sequences (bracketed paste + anything else)
                if ch == '\x1b':
                    seq = ch + sys.stdin.read(1)  # expect '['
                    if seq == '\x1b[':
                        tail = ''
                        while True:
                            c = sys.stdin.read(1)
                            tail += c
                            if c.isalpha() or c == '~':
                                break
                        full = seq + tail
                        if full == '\x1b[200~':
                            in_paste = True
                        elif full == '\x1b[201~':
                            in_paste = False
                        # drop any other CSI sequence silently
                    continue
                if ch in ('\r', '\n'):
                    # Inside a pasted block, a newline is data, not submit.
                    if in_paste:
                        continue  # silently drop embedded newlines
                    if chars:
                        break
                    continue
                if ch == '\x03':
                    raise KeyboardInterrupt
                if ch in ('\x7f', '\x08'):  # backspace / ctrl-H
                    if chars and not in_paste:
                        chars.pop()
                        if not secret:
                            sys.stdout.write('\b \b')
                            sys.stdout.flush()
                    continue
                if ord(ch) < 0x20:
                    continue  # drop other control chars (tabs, etc.)
                chars.append(ch)
                if not secret:
                    sys.stdout.write(ch)
                    sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()
        # Belt-and-suspenders: strip any residual CSI sequence that slipped
        # through a partial read, then trim whitespace.
        cleaned = re.sub(r'\x1b\[[\d;]*[ -/]*[@-~]', '', ''.join(chars))
        return cleaned.strip()
    except (ImportError, Exception):
        if secret:
            import getpass
            return getpass.getpass('').strip()
        return input().strip()


def parse_kv_stream(text):
    """Parse KEY=value lines. Only recognises known OWA_* keys to avoid
    writing arbitrary junk to the config file."""
    allowed = {'OWA_REFRESH_TOKEN', 'OWA_TENANT_ID', 'OWA_CLIENT_ID'}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k in allowed and v:
            out[k] = v
    return out


def interactive_setup(config):
    # Non-interactive path: if stdin is piped, parse KEY=value lines from it.
    # This avoids the bracketed-paste corruption that raw-tty input is prone
    # to with very long secrets, and pairs directly with the JS snippet's
    # KEY=value output (e.g. `pbpaste | owa-piggy --save-config`).
    if not sys.stdin.isatty():
        parsed = parse_kv_stream(sys.stdin.read())
        if not parsed.get('OWA_REFRESH_TOKEN') or not parsed.get('OWA_TENANT_ID'):
            print('ERROR: stdin missing OWA_REFRESH_TOKEN and/or OWA_TENANT_ID. '
                  'Expected KEY=value lines as printed by the browser snippet.',
                  file=sys.stderr)
            return False
        config.update(parsed)
        save_config(config)
        print(f'Config saved to {CONFIG_PATH}', file=sys.stderr)
        return True

    print('owa-piggy setup\n')
    print('1. Open https://outlook.cloud.microsoft in Microsoft Edge')
    print('   (plain Chromium browsers store a session-bound token that')
    print('    AAD rejects as malformed - seed from Edge only.)')
    print('2. Open DevTools (F12) > Console')
    print('3. Paste this snippet to print both values:\n')
    print('   const find = s => Object.keys(localStorage).find(k => k.includes(s))')
    print('   const parse = s => JSON.parse(localStorage[find(s)])')
    print('   const rt = parse(\'|refreshtoken|\'), it = parse(\'|idtoken|\')')
    print('   if (!rt.secret) console.warn(\'WARN: non-MSAL shape; seed from Edge.\')')
    print('   console.log(\'OWA_REFRESH_TOKEN=\' + (rt.secret || rt.data))')
    print('   console.log(\'OWA_TENANT_ID=\' + (it.realm || find(\'|idtoken|\').split(\'|\')[5]))\n')
    print('   Tip: to avoid terminal paste-corruption on very long tokens,')
    print('   copy the two output lines and pipe them in instead:')
    print('     pbpaste | owa-piggy --save-config\n')
    rt = read_input('Refresh token (starts with "1.AQ..."), then press Enter (input hidden):', secret=True)
    if not rt:
        print('ERROR: no refresh token provided', file=sys.stderr)
        return False

    tid = read_input('Tenant ID (a UUID), then press Enter:')
    if not tid:
        print('ERROR: no tenant ID provided', file=sys.stderr)
        return False

    config['OWA_REFRESH_TOKEN'] = rt
    config['OWA_TENANT_ID'] = tid
    save_config(config)
    print(f'\nConfig saved to {CONFIG_PATH}')
    return True


def decode_jwt_segment(segment):
    import base64
    segment += '=' * ((4 - len(segment) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


def token_minutes_remaining(access_token):
    import time
    try:
        payload = decode_jwt_segment(access_token.split('.')[1])
        return int((payload.get('exp', 0) - time.time()) / 60)
    except Exception:
        return None


def decode_jwt(access_token):
    parts = access_token.split('.')
    lines = []
    for i, label in enumerate(['Header', 'Payload']):
        if i >= len(parts):
            break
        try:
            decoded = decode_jwt_segment(parts[i])
            lines.append(f'=== {label} ===')
            lines.append(json.dumps(decoded, indent=2))
        except Exception as e:
            print(f'Error decoding {label}: {e}', file=sys.stderr)
    return '\n'.join(lines)


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
  owa-piggy --graph                                  # Microsoft Graph token
  owa-piggy --teams                                  # Teams token
  owa-piggy --list-scopes                            # show all FOCI audiences
  owa-piggy --scope 'https://graph.microsoft.com/.default'
  owa-piggy --json | jq .scope
  eval $(owa-piggy --env)                             # export into shell
  owa-piggy --decode                                  # inspect JWT claims

notes:
  - Refresh tokens have a 24h sliding window - use daily to keep alive
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

    if '--list-scopes' in args:
        max_name = max(len(n) for n in KNOWN_SCOPES)
        max_aud = max(len(aud) for aud, _ in KNOWN_SCOPES.values())
        for name, (aud, desc) in KNOWN_SCOPES.items():
            flag = f'--{name}'
            print(f'  {flag:<{max_name + 3}} {aud:<{max_aud + 2}} {desc}')
        return 0

    scope = DEFAULT_SCOPE
    for name, (aud, _) in KNOWN_SCOPES.items():
        if f'--{name}' in args:
            scope = f'{aud}/.default openid profile offline_access'
            break
    if '--scope' in args:
        idx = args.index('--scope')
        if idx + 1 < len(args):
            scope = args[idx + 1]
        else:
            print('ERROR: --scope requires a value', file=sys.stderr)
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
        print('\n\tOWA-PIGGY 🐽  CONFIGURED!\n', file=sys.stderr)
        print('  REMEMBER TO REFRESH IN THE 24-HOUR WINDOW', file=sys.stderr)
        print('  TO INSTALL:', file=sys.stderr)
        print('    pipx install .           (recommended)', file=sys.stderr)
        print('    ./scripts/add-to-path.sh (symlink to /usr/local/bin/)', file=sys.stderr)
        print('  RUN ./scripts/setup-refresh.sh TO REFRESH THE TOKEN EVERY HOUR', file=sys.stderr)
        print('\n\tENJOY YOUR APP-REG-FREE SCOPES\n', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())
