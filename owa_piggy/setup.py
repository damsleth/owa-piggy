"""Interactive first-time setup and the raw-tty input helper.

read_input() bypasses cooked-mode line-length limits so pasted refresh
tokens (which can exceed 4KB) don't get truncated. interactive_setup()
is the --save-config flow; it also parses piped stdin so
`pbpaste | owa-piggy --save-config` works.
"""
import sys

from . import config as _config
from .config import iso_utc_now, parse_kv_stream, save_config

# Some tests monkeypatch setup.CONFIG_PATH directly (legacy fixture behavior).
# Expose it as a module attribute so those patches keep working, but read
# `_config.CONFIG_PATH` at call time everywhere it matters so the active
# profile's path is always current.
CONFIG_PATH = _config.CONFIG_PATH


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


def interactive_setup(config, alias='default'):
    """Run the setup flow for profile <alias>. `CONFIG_PATH` must already
    be pointing at that profile's config file (caller's job, typically
    via `config.set_active_profile(alias)`).

    The alias is used only for user-facing labeling so whoever is
    setting up multiple tenants can tell which one they're typing into.
    """
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
        # Stamp issuance time so --status can show the 24h hard-cap. This is
        # set on setup/reseed paths only, never on ordinary rotation (which
        # does not reset the SPA hard-cap timer).
        config['OWA_RT_ISSUED_AT'] = iso_utc_now()
        save_config(config)
        print(f'Config saved to {_config.CONFIG_PATH} [profile={alias}]', file=sys.stderr)
        return True

    print(f'owa-piggy setup [profile={alias}]\n')
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
    print(f'     pbpaste | owa-piggy --save-config --profile {alias}\n')
    rt = read_input(f'[{alias}] Refresh token (starts with "1.AQ..."), then Enter (input hidden):', secret=True)
    if not rt:
        print('ERROR: no refresh token provided', file=sys.stderr)
        return False

    tid = read_input(f'[{alias}] Tenant ID (a UUID), then Enter:')
    if not tid:
        print('ERROR: no tenant ID provided', file=sys.stderr)
        return False

    config['OWA_REFRESH_TOKEN'] = rt
    config['OWA_TENANT_ID'] = tid
    config['OWA_RT_ISSUED_AT'] = iso_utc_now()
    save_config(config)
    print(f'\nConfig saved to {_config.CONFIG_PATH} [profile={alias}]')
    return True
