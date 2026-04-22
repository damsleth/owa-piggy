"""Argument parsing and dispatch for the `owa-piggy` command.

Subcommand-based: `owa-piggy <command> [options]`. Bare `owa-piggy`
(or `owa-piggy --profile <alias>`, `owa-piggy --audience teams`, etc.)
dispatches to the implicit `token` subcommand so the most common
invocation stays terse.

argparse's subparsers would normally require the subcommand to come
first, but we want top-level options to flow into the `token` command
for the bareform case. We handle that by detecting whether argv begins
with a known subcommand and injecting `token` explicitly when it does
not. The subsequent argparse pass sees a consistent shape either way.
"""
import argparse
import json
import os
import shutil
import sys
import time

from . import __version__
from .cache import (
    clear_cache,
    get_cached_exp,
    get_cached_token,
    store_token,
)
from .config import (
    ensure_profile_registered,
    list_profiles,
    load_config,
    load_profiles_conf,
    profile_dir,
    resolve_profile,
    save_config,
    save_profiles_conf,
    set_active_profile,
    unregister_profile,
    validate_alias,
)
from .jwt import decode_jwt, decode_jwt_segment, token_minutes_remaining
from .migration import migrate_if_needed
from .oauth import CLIENT_ID, exchange_token
from .reseed import do_reseed
from .scopes import KNOWN_AUDIENCES, resolve_audience
from .setup import interactive_setup
from .status import do_debug, do_status, do_status_all

COMMANDS = (
    'token', 'status', 'debug', 'setup', 'reseed',
    'decode', 'remaining', 'audiences', 'profiles',
)

_EPILOG = """\
one-time setup:
  1. Open https://outlook.cloud.microsoft in Microsoft Edge
     (plain Chromium browsers store a session-bound token AAD rejects)
  2. Open DevTools (F12) > Console
  3. Paste this snippet to print both values:
       const find = s => Object.keys(localStorage).find(k => k.includes(s))
       const parse = s => JSON.parse(localStorage[find(s)])
       const rt = parse('|refreshtoken|'), it = parse('|idtoken|')
       if (!rt.secret) console.warn('WARN: non-MSAL shape.')
       console.log(`OWA_REFRESH_TOKEN=${rt.secret || rt.data}\\nOWA_TENANT_ID=${(it.realm || find('|idtoken|').split('|')[5])}`)
  4. Run: owa-piggy setup --profile <alias>

examples:
  owa-piggy                                        # raw access token to stdout
  owa-piggy --profile work                         # token for the 'work' profile
  OWA_PROFILE=work owa-piggy                       # same, via env
  owa-piggy --audience teams                       # Teams audience
  owa-piggy --scope 'https://graph.microsoft.com/.default'
  owa-piggy token --json | jq .scope               # full response
  eval $(owa-piggy token --env)                    # export into shell
  owa-piggy decode                                 # JWT header + payload
  owa-piggy remaining                              # 73min
  owa-piggy status                                 # all profiles, ISO8601 health
  owa-piggy status --profile work                  # one profile
  owa-piggy debug                                  # full diagnostics
  owa-piggy reseed --profile work                  # recover from 24h hard-expiry
  owa-piggy setup --profile new                    # create a new profile
  pbpaste | owa-piggy setup --profile new          # pipe token from clipboard
  owa-piggy profiles                               # list (TTY: interactive picker)
  owa-piggy profiles set-default work              # change the default pointer
  owa-piggy profiles delete personal               # remove a profile
  owa-piggy audiences                              # list all FOCI audiences

notes:
  - Default audience is Microsoft Graph (superset of Outlook REST plus
    OneDrive, Teams, SharePoint, directory). Set OWA_DEFAULT_AUDIENCE to
    change it persistently; --audience still wins per call.
  - Refresh tokens have TWO expiry rules:
      * 24h sliding window (rotates on every use)
      * 24h absolute hard-cap from original sign-in (AADSTS700084)
    The launchd agent handles the first; `owa-piggy reseed` handles the second.
  - Rotated refresh token is saved automatically after each exchange.
  - OWA is a FOCI client, so the token works across Microsoft first-party APIs.

config:
  ~/.config/owa-piggy/
    profiles.conf                 OWA_DEFAULT_PROFILE + OWA_PROFILES
    profiles/<alias>/config       per-profile KV (OWA_REFRESH_TOKEN, ...)

  Env vars take precedence over the config file. OWA_PROFILE selects
  which profile to load.
"""


def _add_common_options(p, *, audience_scope=True):
    """Attach the shared options a command accepts.

    Every command that touches a profile gets --profile. Commands that
    mint or probe a token additionally get --audience and --scope.
    --audience uses argparse `choices=` so typos error at parse time
    with the full list, rather than silently falling back to the default.
    """
    p.add_argument('--profile', metavar='<alias>', default=None,
                   help='target a specific profile (also honored via OWA_PROFILE)')
    if audience_scope:
        p.add_argument('--audience', metavar='<name>', default=None,
                       choices=sorted(KNOWN_AUDIENCES.keys()),
                       help='named FOCI audience (see `owa-piggy audiences`)')
        p.add_argument('--scope', metavar='<scope>', default=None,
                       help='override scope explicitly (takes precedence)')


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='owa-piggy',
        description=f'owa-piggy {__version__} - exchange OWA\'s browser-stored '
                    f'refresh token for an Outlook/Graph access token, no '
                    f'Azure AD app registration required.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument('--version', action='version',
                        version=f'owa-piggy {__version__}')

    sub = parser.add_subparsers(dest='command', metavar='<command>')

    p_token = sub.add_parser(
        'token', help='print access token (default when no command given)')
    _add_common_options(p_token)
    p_token.add_argument('--json', action='store_true',
                         help='print full token response as JSON')
    p_token.add_argument('--env', action='store_true',
                         help='print ACCESS_TOKEN= and EXPIRES_IN= lines')

    p_status = sub.add_parser(
        'status', help='compact ISO8601 health summary (all profiles if --profile omitted)')
    _add_common_options(p_status)

    p_debug = sub.add_parser(
        'debug', help='dump full setup diagnostics for one profile')
    _add_common_options(p_debug)

    p_decode = sub.add_parser(
        'decode', help='print the JWT header and payload of the current access token')
    _add_common_options(p_decode)

    p_remaining = sub.add_parser(
        'remaining', help='print minutes remaining on the current access token')
    _add_common_options(p_remaining)

    p_setup = sub.add_parser(
        'setup', help='interactive first-time setup; creates the profile if new')
    _add_common_options(p_setup, audience_scope=False)

    p_reseed = sub.add_parser(
        'reseed', help='fetch a fresh refresh token headlessly from the Edge sidecar')
    _add_common_options(p_reseed, audience_scope=False)

    sub.add_parser(
        'audiences', help='list all known FOCI-accessible audiences')

    p_profiles = sub.add_parser(
        'profiles', help='list / manage profiles')
    profiles_sub = p_profiles.add_subparsers(
        dest='profiles_command', metavar='<subcommand>')

    p_sd = profiles_sub.add_parser(
        'set-default', help='make <alias> the default profile')
    p_sd.add_argument('alias', metavar='<alias>')

    p_del = profiles_sub.add_parser(
        'delete', help='remove a profile config + Edge sidecar dir')
    p_del.add_argument('alias', metavar='<alias>')
    p_del.add_argument('--force', action='store_true',
                       help='allow deleting the profile currently marked default')

    return parser


def _inject_default_command(argv):
    """Prepend `token` to argv when the user invoked owa-piggy without
    naming a subcommand - either bare (`owa-piggy`) or with only
    options (`owa-piggy --profile work`). `--help` and `--version` are
    passed through untouched so argparse handles them at the root.
    """
    if not argv:
        return ['token']
    head = argv[0]
    if head in COMMANDS:
        return list(argv)
    if head in ('-h', '--help', '--version'):
        return list(argv)
    return ['token'] + list(argv)


def _resolve_and_activate(args, *, allow_missing=False):
    """Resolve args.profile into a concrete alias and activate it.

    Returns (alias, exit_code). exit_code is 0 on success, non-zero on
    failure (the caller should return it; the error is already printed).
    """
    alias, err = resolve_profile(args.profile, allow_missing=allow_missing)
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return '', 1
    set_active_profile(alias)
    return alias, 0


# --- Command handlers ------------------------------------------------------


def _cmd_token(args):
    return _mint_and_emit(args, mode='raw')


def _cmd_decode(args):
    return _mint_and_emit(args, mode='decode')


def _cmd_remaining(args):
    return _mint_and_emit(args, mode='remaining')


def _mint_and_emit(args, *, mode):
    """Shared token-mint path for token/decode/remaining.

    `mode` is one of 'raw', 'decode', 'remaining', or (token-only)
    'json'/'env'; the latter two are taken from args.json/args.env so
    the caller doesn't have to translate. Every mode goes through the
    same cache short-circuit + exchange + rotate-persist plumbing.
    """
    if mode == 'raw':
        if getattr(args, 'json', False):
            mode = 'json'
        elif getattr(args, 'env', False):
            mode = 'env'

    alias, rc = _resolve_and_activate(args)
    if rc:
        return rc

    scope, err = resolve_audience(args.audience, args.scope)
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1

    config, persist = load_config()
    refresh_token = config.get('OWA_REFRESH_TOKEN', '').strip()
    tenant_id = config.get('OWA_TENANT_ID', '').strip()
    client_id = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()

    # Access-token cache short-circuit. Modes that only need the AT (or
    # something derivable from it locally) can be served from the
    # per-profile cache without round-tripping AAD - this matters when
    # callers shell out to `owa-piggy` in tight loops and would otherwise
    # risk 429s.
    #
    # Bypass for: json (needs the fresh refresh_token from the response
    # which we intentionally don't cache). Other bypass paths (status,
    # debug, reseed) return earlier in main() and never reach here.
    #
    # Cache key is (tenant, client, scope) AND scoped per-profile via
    # a separate cache.json under each profile dir, so switching profiles
    # or tenants naturally misses the old entries.
    if mode != 'json' and tenant_id:
        cached_at = get_cached_token(tenant_id, client_id, scope)
        if cached_at:
            return _emit(cached_at, mode,
                         cache_hit_exp=get_cached_exp(tenant_id, client_id, scope))

    # Sanity-check the token shape before we ship it to AAD. Real FOCI
    # refresh tokens are "{version}.{base64url payload}" with version 0
    # or 1. Plain Chromium browsers store a session-bound opaque token
    # at MSAL's cache location that AAD rejects as malformed. Fail fast
    # with an actionable message instead of letting AADSTS9002313 confuse
    # the user.
    if refresh_token and not (refresh_token.startswith('1.') or refresh_token.startswith('0.')):
        print('ERROR: OWA_REFRESH_TOKEN does not look like an AAD FOCI refresh '
              'token (expected "1.AQ..." or "0.AQ..."). Plain Chromium browsers '
              'store a session-bound token that AAD will not accept. Reseed '
              'from Microsoft Edge via `owa-piggy setup`.', file=sys.stderr)
        return 1

    if not refresh_token or not tenant_id:
        if not refresh_token:
            print(f'ERROR: OWA_REFRESH_TOKEN not set for profile {alias!r}. '
                  f'Run: owa-piggy setup --profile {alias}',
                  file=sys.stderr)
        if not tenant_id:
            print(f'ERROR: OWA_TENANT_ID not set for profile {alias!r}. '
                  f'Run: owa-piggy setup --profile {alias}',
                  file=sys.stderr)
        return 1

    result = exchange_token(refresh_token, tenant_id, client_id, scope)
    if not result:
        return 1

    access_token = result.get('access_token')
    new_refresh = result.get('refresh_token')

    if not access_token:
        print(f'ERROR: no access_token in response: {list(result.keys())}',
              file=sys.stderr)
        return 1

    # Cache the fresh AT keyed by (tenant, client, scope). Failures here
    # (disk full, permission weirdness) must not fail the exchange - we
    # already have the token in hand; caching is an optimisation.
    try:
        payload = decode_jwt_segment(access_token.split('.')[1])
        exp = payload.get('exp')
        if isinstance(exp, (int, float)):
            store_token(tenant_id, client_id, scope, access_token, exp)
    except Exception:
        pass

    # Persist rotated refresh token only when the original came from the
    # config file. Env-only callers stay env-only; writing to disk would
    # silently turn non-persistent usage into persistent secret storage.
    if new_refresh:
        config['OWA_REFRESH_TOKEN'] = new_refresh
        if persist:
            save_config(config)
        elif new_refresh != refresh_token:
            print('NOTE: refresh token rotated; OWA_REFRESH_TOKEN was env-only so '
                  'the new token was not written to disk. Update your environment '
                  'or run `owa-piggy setup` to persist.', file=sys.stderr)

    return _emit(access_token, mode, full_response=result)


def _emit(access_token, mode, *, full_response=None, cache_hit_exp=None):
    """Print access token in the requested mode. Returns 0."""
    if mode == 'json':
        # json is only used on fresh-exchange path (bypasses cache), so
        # full_response is always present.
        print(json.dumps(full_response, indent=2))
    elif mode == 'env':
        print(f'ACCESS_TOKEN={access_token}')
        if full_response is not None:
            print(f'EXPIRES_IN={full_response.get("expires_in", "")}')
        else:
            exp = cache_hit_exp or 0
            print(f'EXPIRES_IN={max(0, int(exp - time.time()))}')
    elif mode == 'decode':
        print(decode_jwt(access_token))
    elif mode == 'remaining':
        remaining = token_minutes_remaining(access_token)
        print(f'{remaining}min' if remaining is not None else 'unknown')
    else:
        print(access_token)
    return 0


def _cmd_setup(args):
    # The user is explicitly re-identifying; any cached AT belongs to
    # the pre-setup identity and must not leak past this point.
    alias, rc = _resolve_and_activate(args, allow_missing=True)
    if rc:
        return rc
    clear_cache()
    config, _ = load_config()
    if not interactive_setup(config, alias):
        return 1
    # Register the profile in profiles.conf so `profiles` sees it and
    # resolve_profile can find it. If this is the first profile ever
    # created, make it the default.
    ensure_profile_registered(alias, make_default_if_first=True)
    print(f'\n\tOWA-PIGGY 🐽  CONFIGURED [{alias}]', file=sys.stderr)
    print('\n\tENJOY YOUR APP-REG-FREE SCOPES\n', file=sys.stderr)
    return 0


def _cmd_reseed(args):
    alias, rc = _resolve_and_activate(args)
    if rc:
        return rc
    # Clear the AT cache first so we never serve a token minted for the
    # pre-reseed identity/session after the user has explicitly asked
    # for a fresh credential.
    clear_cache()
    return do_reseed(alias)


def _cmd_status(args):
    # No explicit profile + no OWA_PROFILE env: iterate every profile.
    # This bypasses resolve_profile()'s ambiguity error (multiple
    # profiles / no default) which would otherwise make the informational
    # path unusable on the very installs where it is most useful.
    if not args.profile and not os.environ.get('OWA_PROFILE', '').strip():
        return do_status_all(audience=args.audience, scope=args.scope)

    alias, rc = _resolve_and_activate(args)
    if rc:
        return rc
    return do_status(alias, audience=args.audience, scope=args.scope)


def _cmd_debug(args):
    alias, rc = _resolve_and_activate(args)
    if rc:
        return rc
    return do_debug(alias, audience=args.audience, scope=args.scope)


def _cmd_audiences(args):
    max_name = max(len(n) for n in KNOWN_AUDIENCES)
    max_aud = max(len(aud) for aud, _ in KNOWN_AUDIENCES.values())
    for name, (aud, desc) in KNOWN_AUDIENCES.items():
        print(f'  {name:<{max_name + 2}}{aud:<{max_aud + 2}}{desc}')
    return 0


def _cmd_profiles(args):
    sub = getattr(args, 'profiles_command', None)
    if sub == 'set-default':
        return _do_profiles_set_default(args.alias)
    if sub == 'delete':
        return _do_profiles_delete(args.alias, force=args.force)
    # Bare `owa-piggy profiles` - list (with interactive picker on TTY).
    return _do_profiles_list()


def _do_profiles_list():
    """Print configured profiles, marking the default with '*'.

    On an interactive stdout+stdin we draw a simple up/down picker so
    the user can change the default profile without memorising
    `profiles set-default`. Non-TTY invocations (pipes, redirects, CI)
    fall through to the plain printed list so scripts stay parseable.
    """
    profiles = list_profiles()
    if not profiles:
        print('no profiles configured. Run: owa-piggy setup --profile <alias>')
        return 0
    reg = load_profiles_conf()
    default = reg['OWA_DEFAULT_PROFILE']
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _interactive_profile_picker(profiles, default)
    for alias in profiles:
        marker = ' *' if alias == default else '  '
        print(f'{marker} {alias}')
    return 0


def _interactive_profile_picker(profiles, default):
    """Draw an up/down picker. Enter sets the highlighted profile as
    default; q/Esc quits without changing anything. '*' marks the current
    registry default; '>' marks the cursor.

    Falls back to the plain list if termios is unavailable (non-POSIX)
    or any I/O step raises before the first key read.
    """
    try:
        import termios
        import tty
    except ImportError:
        for alias in profiles:
            marker = ' *' if alias == default else '  '
            print(f'{marker} {alias}')
        return 0

    idx = profiles.index(default) if default in profiles else 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def draw(first=False):
        # Rewind over the previous frame (header + one line per profile +
        # footer). On first draw there is nothing to clear.
        if not first:
            sys.stdout.write(f'\x1b[{len(profiles) + 2}A')
        sys.stdout.write('profiles (enter = set default, q = quit):\r\n')
        for i, alias in enumerate(profiles):
            star = '*' if alias == default else ' '
            cursor = '>' if i == idx else ' '
            # \x1b[K clears to end-of-line so the previous frame's trailing
            # characters do not bleed through when lines differ in length.
            sys.stdout.write(f'{cursor} {star} {alias}\x1b[K\r\n')
        sys.stdout.write('\x1b[K\r\n')
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        draw(first=True)
        while True:
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                seq = sys.stdin.read(1)
                if seq == '[':
                    arrow = sys.stdin.read(1)
                    if arrow == 'A' and idx > 0:
                        idx -= 1
                    elif arrow == 'B' and idx < len(profiles) - 1:
                        idx += 1
                    draw()
                    continue
                # Bare ESC = quit.
                return 0
            if ch in ('q', 'Q', '\x03'):  # q / ctrl-C
                return 0
            if ch in ('\r', '\n'):
                break
            if ch == 'k' and idx > 0:
                idx -= 1
                draw()
            elif ch == 'j' and idx < len(profiles) - 1:
                idx += 1
                draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    chosen = profiles[idx]
    if chosen == default:
        print(f'{chosen!r} is already the default; no change.')
        return 0
    reg = load_profiles_conf()
    reg['OWA_DEFAULT_PROFILE'] = chosen
    if chosen not in reg['OWA_PROFILES']:
        reg['OWA_PROFILES'].append(chosen)
    save_profiles_conf(reg)
    print(f'default profile set to {chosen!r}')
    return 0


def _do_profiles_set_default(alias):
    """Mark `alias` as the default profile. Profile must exist on disk."""
    ok, verr = validate_alias(alias)
    if not ok:
        print(f'ERROR: {verr}', file=sys.stderr)
        return 1
    if alias not in list_profiles():
        print(
            f'ERROR: profile {alias!r} not found. Available: '
            f'{", ".join(list_profiles()) or "(none)"}',
            file=sys.stderr,
        )
        return 1
    reg = load_profiles_conf()
    reg['OWA_DEFAULT_PROFILE'] = alias
    # Re-register so the profile appears in OWA_PROFILES even if this is
    # a pre-registry profile (shouldn't happen post-migration but harmless).
    if alias not in reg['OWA_PROFILES']:
        reg['OWA_PROFILES'].append(alias)
    save_profiles_conf(reg)
    print(f'default profile set to {alias!r}')
    return 0


def _do_profiles_delete(alias, force=False):
    """Remove profile <alias> from disk and from profiles.conf.

    Refuses to delete the currently marked default without --force, so a
    fat-finger doesn't leave the registry pointing at nothing.
    """
    ok, verr = validate_alias(alias)
    if not ok:
        print(f'ERROR: {verr}', file=sys.stderr)
        return 1
    if alias not in list_profiles():
        print(f'ERROR: profile {alias!r} not found', file=sys.stderr)
        return 1
    reg = load_profiles_conf()
    if reg['OWA_DEFAULT_PROFILE'] == alias and not force:
        print(
            f'ERROR: {alias!r} is the default profile. Choose another default '
            f'with `owa-piggy profiles set-default <alias>` first, or pass '
            f'--force to override.',
            file=sys.stderr,
        )
        return 1
    target = profile_dir(alias)
    try:
        shutil.rmtree(target)
    except OSError as e:
        print(f'ERROR: failed to remove {target}: {e}', file=sys.stderr)
        return 1
    unregister_profile(alias)
    print(f'removed profile {alias!r}.')
    remaining = list_profiles()
    if remaining:
        print(
            f'  NB: if you had a launchd job for it, drop it with: '
            f'setup-refresh.sh --uninstall --profile {alias}',
            file=sys.stderr,
        )
    return 0


_DISPATCH = {
    'token': _cmd_token,
    'status': _cmd_status,
    'debug': _cmd_debug,
    'setup': _cmd_setup,
    'reseed': _cmd_reseed,
    'decode': _cmd_decode,
    'remaining': _cmd_remaining,
    'audiences': _cmd_audiences,
    'profiles': _cmd_profiles,
}


def main():
    argv = _inject_default_command(sys.argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Idempotent on fresh or already-migrated installs.
    migrate_if_needed()

    command = args.command or 'token'
    handler = _DISPATCH.get(command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)
