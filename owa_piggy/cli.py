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
import subprocess
import sys
import time

from . import __version__
from . import schema as schema_mod
from .cache import (
    clear_cache,
    get_cached_exp,
    get_cached_token,
    store_token,
)
from .config import (
    profile_config_path,
    list_profiles,
    load_config,
    load_profiles_conf,
    resolve_profile,
    save_config,
    set_active_profile,
    validate_alias,
)
from .jwt import decode_jwt, decode_jwt_segment, token_minutes_remaining
from .migration import migrate_if_needed
from .oauth import CLIENT_ID
from .profiles import create_profile, delete_profile, set_default_profile
from .reseed import do_reseed, do_reseed_all, do_reseed_scheduled
from .scopes import KNOWN_AUDIENCES, resolve_audience
from .status import do_debug, do_status, do_status_all, status_all_report, status_report
from .token_flow import exchange_fresh

_EPILOG = """\
one-time setup (two paths):

  A. Network-capture (required for Okta-federated / encrypted-MSAL tenants):
       owa-piggy setup --profile <alias> --email <addr>
     Edge opens, you sign in normally (password, Okta Verify push, MFA -
     whatever the tenant requires). owa-piggy captures the refresh token
     off the /oauth2/v2.0/token response and closes the browser.

  B. Manual paste (legacy MSAL cache, plaintext localStorage):
     1. Open https://outlook.cloud.microsoft in Microsoft Edge
        (plain Chromium stores a session-bound token AAD rejects)
     2. Open DevTools (F12) > Console
     3. Paste this snippet:
          const find = s => Object.keys(localStorage).find(k => k.includes(s))
          const parse = s => JSON.parse(localStorage[find(s)])
          const rt = parse('|refreshtoken|'), it = parse('|idtoken|')
          if (!rt.secret) console.warn('WARN: non-MSAL shape.')
          console.log(`OWA_REFRESH_TOKEN=${rt.secret || rt.data}\\nOWA_TENANT_ID=${(it.realm || find('|idtoken|').split('|')[5])}`)
     4. Run: owa-piggy setup --profile <alias>
        (or: pbpaste | owa-piggy setup --profile <alias>)
     If the snippet warns "non-MSAL shape" the tenant has encrypted cache;
     use path A instead.

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
  owa-piggy reseed --all                           # reseed every configured profile
  owa-piggy reseed --scheduled                      # reseed only OWA_SCHEDULED (launchd uses this)
  owa-piggy edge --profile work                    # open Edge with the profile's sidecar session
  owa-piggy setup --profile new                    # paste-flow setup
  owa-piggy setup --profile new --email me@x.org   # network-capture setup (Okta etc.)
  pbpaste | owa-piggy setup --profile new          # pipe token from clipboard
  owa-piggy profiles                               # list (TTY: interactive picker)
  owa-piggy profiles list                          # non-interactive list (alias of bare)
  owa-piggy profiles list --json                   # machine-readable list
  owa-piggy profiles new work                      # alias of `setup --profile work`
  owa-piggy profiles new work --email me@x.org     # network-capture setup (Okta etc.)
  owa-piggy profiles set-default work              # change the default pointer
  owa-piggy profiles delete personal               # remove a profile
  owa-piggy audiences                              # list all FOCI audiences

machine surface (uniform across the owa suite):
  owa-piggy schema [<command>]                     # JSON command schema
  owa-piggy --help --json                          # the same schema
  owa-piggy --agent <cmd>                          # wrap JSON stdout in an envelope
  owa-piggy --err-json <cmd>                       # structured JSON errors on stderr
  owa-piggy --doctor [--json]                      # health / redaction doctor payload

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
    p_status.add_argument('--json', action='store_true',
                          help='print token health as JSON without token values')
    p_status.add_argument('--verbose', '-v', action='store_true',
                          help='include audience and scopes lines')

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
    # --email switches setup to the network-capture path: launches Edge
    # visibly, lets the user sign in, and intercepts the /token response.
    # Required for tenants whose MSAL.js encrypts the localStorage cache
    # (Okta-federated, recent-MSAL SPAs) - the legacy paste flow returns
    # an AES-GCM envelope rather than a usable refresh token there.
    p_setup.add_argument('--email', metavar='<addr>', default=None,
                         help='use Edge network-capture flow (required for '
                              'encrypted-MSAL/Okta tenants); validates '
                              'captured token belongs to this account')
    p_setup.add_argument('--from-trough', metavar='<url>', default=None,
                         dest='from_trough',
                         help='seed from a tailnet-side trough appliance '
                              '(e.g. http://100.x.y.z:8765). Pulls the '
                              'freshest FOCI RT from its store. Honors '
                              'OWA_TROUGH_URL as a default. Mutually '
                              'exclusive with --email.')
    p_setup.add_argument('--trough-tenant', metavar='<tid>', default=None,
                         dest='trough_tenant',
                         help='filter trough RTs by AAD tenant id GUID; '
                              'use when multiple tenants are present in '
                              'the trough store')
    p_setup.add_argument('--trough-sub', metavar='<oid>', default=None,
                         dest='trough_sub',
                         help='filter trough RTs by AAD user object id; '
                              'narrower than --trough-tenant for shared '
                              'tenants')
    p_setup.add_argument('--user-agent', metavar='<ua>', default=None,
                         dest='user_agent',
                         help='spoof the Edge sidecar User-Agent at sign-in '
                              '(e.g. iOS Teams UA to bypass tenant CA that '
                              'gates on platform). Persisted as '
                              'OWA_USER_AGENT and reused on every reseed.')
    p_setup.add_argument('--json', action='store_true',
                         help='(rejected) setup is interactive; use status --json instead')

    p_reseed = sub.add_parser(
        'reseed', help='fetch a fresh refresh token headlessly from the Edge sidecar')
    _add_common_options(p_reseed, audience_scope=False)
    p_reseed.add_argument('--all', action='store_true', dest='all_profiles',
                          help='reseed every configured profile sequentially')
    p_reseed.add_argument('--scheduled', action='store_true',
                          dest='scheduled_profiles',
                          help='reseed only profiles in OWA_SCHEDULED (the set '
                               'the shared launchd agent rotates); used by the '
                               'launchd agent itself')
    p_reseed.add_argument('--json', action='store_true',
                          help='emit action envelope on stdout')

    p_edge = sub.add_parser(
        'edge', help="open a normal Edge window using a profile's sidecar session")
    _add_common_options(p_edge, audience_scope=False)

    sub.add_parser(
        'audiences', help='list all known FOCI-accessible audiences')

    sub.add_parser(
        'install-owa-tools',
        help='install the companion owa-tools suite via Homebrew')

    p_version = sub.add_parser(
        'version', help='print version information')
    p_version.add_argument('--json', action='store_true',
                           help='print version information as JSON')

    p_profiles = sub.add_parser(
        'profiles', help='list / manage profiles')
    p_profiles.add_argument('--json', action='store_true',
                            help='print profiles as JSON')
    profiles_sub = p_profiles.add_subparsers(
        dest='profiles_command', metavar='<subcommand>')

    p_list = profiles_sub.add_parser(
        'list', help='list profiles (non-interactive alias of bare `profiles`)')
    # Distinct dest so the subparser flag doesn't clobber the parent
    # `profiles --json` (argparse default-merge behavior). _cmd_profiles
    # ORs the two so both spellings yield JSON.
    p_list.add_argument('--json', action='store_true', dest='profiles_list_json',
                        help='print profiles as JSON')

    p_sd = profiles_sub.add_parser(
        'set-default', help='make <alias> the default profile')
    p_sd.add_argument('alias', metavar='<alias>')
    p_sd.add_argument('--json', action='store_true',
                      help='emit action envelope on stdout')

    p_new = profiles_sub.add_parser(
        'new', help='create a new profile (alias of `setup --profile <alias>`)')
    p_new.add_argument('alias', metavar='<alias>')
    p_new.add_argument('--email', metavar='<addr>', default=None,
                       help='use Edge network-capture flow (required for '
                            'encrypted-MSAL/Okta tenants); validates '
                            'captured token belongs to this account')

    p_del = profiles_sub.add_parser(
        'delete', help='remove a profile config + Edge sidecar dir')
    p_del.add_argument('alias', metavar='<alias>')
    p_del.add_argument('--force', action='store_true',
                       help='allow deleting the profile currently marked default')
    p_del.add_argument('--yes', action='store_true',
                       help='skip TTY confirmation (required when stdin is not a TTY)')
    p_del.add_argument('--json', action='store_true',
                       help='emit action envelope on stdout')

    p_sched = profiles_sub.add_parser(
        'schedule', help='add <alias> to the shared launchd reseed schedule')
    p_sched.add_argument('alias', metavar='<alias>')
    p_sched.add_argument('--json', action='store_true',
                         help='emit action envelope on stdout')

    p_unsched = profiles_sub.add_parser(
        'unschedule', help='remove <alias> from the shared launchd reseed schedule')
    p_unsched.add_argument('alias', metavar='<alias>')
    p_unsched.add_argument('--json', action='store_true',
                           help='emit action envelope on stdout')

    return parser


# Subcommand registry. Defined here so `_inject_default_command` can read
# it without forcing the reader to scroll past every handler. The dispatch
# table itself lives next to the handlers below; this is just the name
# tuple used during argv preprocessing.
COMMANDS = (
    'token', 'status', 'debug', 'setup', 'reseed', 'decode',
    'remaining', 'edge', 'audiences', 'version', 'profiles',
    'install-owa-tools',
)


def _inject_default_command(argv):
    """Prepend `token` to argv when the user invoked owa-piggy without
    naming a subcommand - either bare (`owa-piggy`) or with only
    options (`owa-piggy --profile work`). `--help` and `--version` are
    passed through untouched so argparse handles them at the root. A
    bare `help` word is rewritten to `--help` so it reads as a verb
    (mirrors the `version` subcommand / `--version` flag pairing);
    `help <cmd>` becomes `<cmd> --help` (git-style routing) so the
    per-subcommand help is reachable without remembering the flag
    spelling. An unknown help topic falls back to root `--help`.
    """
    if not argv:
        return ['token']
    head = argv[0]
    if head == 'help':
        rest = list(argv[1:])
        if rest and rest[0] in COMMANDS:
            return rest + ['--help']
        return ['--help']
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

    config, persist = load_config()
    tenant_id = config.get('OWA_TENANT_ID', '').strip()
    client_id = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()

    scope, err = resolve_audience(
        args.audience, args.scope,
        profile_default=config.get('OWA_DEFAULT_AUDIENCE', '').strip(),
    )
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1

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

    # exchange_fresh handles config-field extraction, FOCI shape check,
    # stderr capture, AAD-error detection, and rotated-RT persistence.
    # We replay the captured stderr immediately so the user-facing
    # 'ERROR: AADSTS...' line still reaches the terminal verbatim.
    result, info = exchange_fresh(config, scope, persist=persist,
                                  capture_stderr=True)
    if info['stderr_text']:
        sys.stderr.write(info['stderr_text'])
        sys.stderr.flush()

    if not info['rt_present']:
        print(f'ERROR: OWA_REFRESH_TOKEN not set for profile {alias!r}. '
              f'Run: owa-piggy setup --profile {alias}',
              file=sys.stderr)
        return 1
    if info['rt_present'] and not info['rt_shape_ok']:
        # Real FOCI refresh tokens are "{version}.{base64url payload}"
        # with version 0 or 1. Plain Chromium browsers store a session-
        # bound opaque token at MSAL's cache location that AAD rejects
        # as malformed; fail fast with an actionable message instead of
        # letting AADSTS9002313 confuse the user.
        print('ERROR: OWA_REFRESH_TOKEN does not look like an AAD FOCI refresh '
              'token (expected "1.AQ..." or "0.AQ..."). Plain Chromium browsers '
              'store a session-bound token that AAD will not accept. Reseed '
              'from Microsoft Edge via `owa-piggy setup`.', file=sys.stderr)
        return 1
    if not info['tid_present']:
        print(f'ERROR: OWA_TENANT_ID not set for profile {alias!r}. '
              f'Run: owa-piggy setup --profile {alias}',
              file=sys.stderr)
        return 1

    # AADSTS70043 (7-day Conditional Access sign-in-frequency cap) and
    # AADSTS700084 (24h SPA hard-cap) are both unrecoverable without a
    # fresh RT off the wire - but we can fetch one ourselves via
    # do_reseed without surfacing the AAD error to the consumer (owa-cal,
    # owa-mail, ...). One auto-reseed + retry per call. If reseed itself
    # fails (sidecar session also dead, capture timeout, etc.) we fall
    # through to the original error so the user still sees the underlying
    # cause instead of a generic 'reseed failed'.
    #
    # Set OWA_AUTO_RESEED=0 to disable - useful for scripts that want the
    # raw AAD error and the original ~instant failure path, or for
    # debugging the reseed plumbing itself.
    auto_reseed = os.environ.get('OWA_AUTO_RESEED', '1').strip() != '0'
    if not result and info['aad_error'] and auto_reseed:
        print(f'[{alias}] {info["aad_error"]}: refresh token expired; '
              f'auto-reseeding...', file=sys.stderr)
        rc = do_reseed(alias)
        if rc == 0:
            config, persist = load_config()
            result, info = exchange_fresh(config, scope, persist=persist,
                                          capture_stderr=True)
            if info['stderr_text']:
                sys.stderr.write(info['stderr_text'])
                sys.stderr.flush()
    if not result:
        return 1

    access_token = result.get('access_token')
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
            store_token(info['tid'], info['cid'], scope, access_token, exp)
    except Exception:
        pass

    # exchange_fresh persisted the rotated RT when persist=True. The
    # env-only case still rotated the in-memory config dict but did not
    # write to disk - surface that to the user so they know to update
    # their environment (the NOTE writes go to stderr to keep stdout
    # script-clean).
    if info['rotated'] and not persist:
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
    # setup is interactive class per hugr CONVENTIONS.md - --json is
    # rejected with a clear pointer to a machine-friendly alternative.
    if getattr(args, 'json', False):
        print(
            'owa-piggy setup is an interactive command; --json is rejected. '
            'Use `owa-piggy status --json` for machine-readable profile state.',
            file=sys.stderr,
        )
        return 1
    email = getattr(args, 'email', None)
    trough_url = getattr(args, 'from_trough', None) or os.environ.get('OWA_TROUGH_URL') or None
    if email and trough_url:
        print('ERROR: --email and --from-trough are mutually exclusive '
              '(pick one capture source).', file=sys.stderr)
        return 1
    alias, rc = _resolve_and_activate(args, allow_missing=True)
    if rc:
        return rc
    return create_profile(
        alias,
        email=email,
        audience=None,
        full_banner=True,
        trough_url=trough_url,
        trough_tenant=getattr(args, 'trough_tenant', None),
        trough_sub=getattr(args, 'trough_sub', None),
        user_agent=(getattr(args, 'user_agent', None)
                    or os.environ.get('OWA_USER_AGENT') or None),
    )


def _cmd_reseed(args):
    as_json = bool(getattr(args, 'json', False))
    t0 = time.monotonic()
    all_profiles = bool(getattr(args, 'all_profiles', False))
    scheduled = bool(getattr(args, 'scheduled_profiles', False))

    def _usage_error(message):
        if as_json:
            from owa_piggy.conventions import (
                EXIT_USER_ERROR, action_envelope, emit_action,
            )
            emit_action(action_envelope(
                command='reseed', ok=False,
                error={'code': 'usage', 'message': message},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            ))
            return EXIT_USER_ERROR
        print(f'ERROR: {message}', file=sys.stderr)
        return 1

    # --all, --scheduled and --profile are mutually exclusive selectors.
    if all_profiles and scheduled:
        return _usage_error('--all and --scheduled are mutually exclusive')
    if (all_profiles or scheduled) and args.profile:
        flag = '--all' if all_profiles else '--scheduled'
        return _usage_error(f'{flag} and --profile are mutually exclusive')

    if all_profiles or scheduled:
        # Per-profile cache clearing happens inside the reseed loop via
        # set_active_profile + the nested `owa-piggy setup` call's own
        # clear_cache, so we do not pre-clear here.
        scope = 'all' if all_profiles else 'scheduled'
        rc = do_reseed_all() if all_profiles else do_reseed_scheduled()
        if as_json:
            from owa_piggy.conventions import action_envelope, emit_action
            emit_action(action_envelope(
                command='reseed', ok=(rc == 0),
                stats={'scope': scope, 'exit_code': int(rc or 0)},
                error=None if rc == 0 else {
                    'code': 'reseed_failed',
                    'message': f'reseed --{scope} returned nonzero',
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            ))
        return rc

    alias, rc = _resolve_and_activate(args)
    if rc:
        if as_json:
            from owa_piggy.conventions import action_envelope, emit_action
            emit_action(action_envelope(
                command='reseed', ok=False,
                error={'code': 'profile_resolve_failed', 'message': 'could not resolve profile'},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            ))
        return rc
    # Clear the AT cache first so we never serve a token minted for the
    # pre-reseed identity/session after the user has explicitly asked
    # for a fresh credential.
    clear_cache()
    rc = do_reseed(alias)
    if as_json:
        from owa_piggy.conventions import action_envelope, emit_action
        emit_action(action_envelope(
            command='reseed', ok=(rc == 0),
            stats={'profile': alias, 'exit_code': int(rc or 0)},
            error=None if rc == 0 else {
                'code': 'reseed_failed',
                'message': f'do_reseed({alias!r}) returned nonzero',
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        ))
    return rc


def _cmd_edge(args):
    """Open a normal, interactive Edge window against the profile's sidecar
    userdata dir, then return - leaving the browser running.

    Unlike `setup --email` (which drives Edge via CDP and closes it the
    moment it captures a token), this just hands you the browser. Sign in
    however the tenant wants; the session cookies land in the per-profile
    edge-profile dir, so a later `owa-piggy reseed` reseeds from that fresh
    session instead of a stale scraped token.
    """
    alias, rc = _resolve_and_activate(args)
    if rc:
        return rc
    from .capture import open_edge
    try:
        _proc, edge_dir = open_edge(alias)
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    # Collapse $HOME to ~ so the path is readable without leaking the
    # username into a wide terminal dump.
    shown = str(edge_dir)
    home = os.path.expanduser('~')
    if shown.startswith(home):
        shown = '~' + shown[len(home):]
    print(f'[{alias}] launched Edge with profile sidecar')
    print(f'  userdata: {shown}')
    print('  Sign in normally; close Edge when done.')
    print(f'  Then run: owa-piggy reseed --profile {alias}')
    return 0


def _cmd_status(args):
    # No explicit profile + no OWA_PROFILE env: iterate every profile.
    # This bypasses resolve_profile()'s ambiguity error (multiple
    # profiles / no default) which would otherwise make the informational
    # path unusable on the very installs where it is most useful.
    if not args.profile and not os.environ.get('OWA_PROFILE', '').strip():
        if getattr(args, 'json', False):
            print(json.dumps(status_all_report(audience=args.audience, scope=args.scope), indent=2))
            return 0
        return do_status_all(audience=args.audience, scope=args.scope,
                              verbose=getattr(args, 'verbose', False))

    alias, rc = _resolve_and_activate(args)
    if rc:
        return rc
    if getattr(args, 'json', False):
        report = status_report(alias, audience=args.audience, scope=args.scope)
        print(json.dumps(report, indent=2))
        return 0 if report.get('state') in ('ok', 'warn', 'disabled') else 1
    return do_status(alias, audience=args.audience, scope=args.scope,
                     verbose=getattr(args, 'verbose', False))


def _cmd_debug(args):
    alias, rc = _resolve_and_activate(args)
    if rc:
        return rc
    return do_debug(alias, audience=args.audience, scope=args.scope)


def _cmd_audiences(_args):
    # Underscore-prefixed because the dispatcher contract passes args to
    # every handler but this one has nothing to read off it.
    max_name = max(len(n) for n in KNOWN_AUDIENCES)
    max_aud = max(len(aud) for aud, _ in KNOWN_AUDIENCES.values())
    for name, (aud, desc) in KNOWN_AUDIENCES.items():
        print(f'  {name:<{max_name + 2}}{aud:<{max_aud + 2}}{desc}')
    return 0


def _cmd_install_owa_tools(args):
    """Hand off to Homebrew to install the companion owa-tools suite.

    Pure convenience shim - the canonical install path is documented as
    `brew install damsleth/tap/owa-tools`, but typing that from memory is
    annoying enough that a one-shot subcommand earns its keep.
    """
    cmd = ['brew', 'install', 'damsleth/tap/owa-tools']
    print(f'$ {" ".join(cmd)}', file=sys.stderr)
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print('ERROR: brew not found on PATH. Install Homebrew first '
              '(https://brew.sh) or run `pipx install owa-tools` instead.',
              file=sys.stderr)
        return 1


def _cmd_version(args):
    if getattr(args, 'json', False):
        print(json.dumps({'tool': 'owa-piggy', 'version': __version__}, indent=2))
    else:
        print(f'owa-piggy {__version__}')
    return 0


def _cmd_profiles(args):
    sub = getattr(args, 'profiles_command', None)
    # `list` uses a distinct dest (profiles_list_json) so the subparser
    # flag doesn't clobber the parent `--json`; OR them here so both
    # spellings work: `profiles --json list` and `profiles list --json`.
    as_json = bool(getattr(args, 'json', False)
                   or getattr(args, 'profiles_list_json', False))
    if sub == 'new':
        # Thin alias for `setup --profile <alias>`. We synthesize the
        # shape `_cmd_setup` expects (args.profile, args.email, args.json)
        # rather than duplicate the create_profile call, so the two paths
        # cannot drift.
        args.profile = args.alias
        args.json = False
        return _cmd_setup(args)
    if sub == 'set-default':
        return _do_profiles_set_default(args.alias, as_json=as_json)
    if sub == 'delete':
        return _do_profiles_delete(
            args.alias,
            force=args.force,
            yes=bool(getattr(args, 'yes', False)),
            as_json=as_json,
        )
    if sub == 'schedule':
        return _do_profiles_schedule(args.alias, schedule=True, as_json=as_json)
    if sub == 'unschedule':
        return _do_profiles_schedule(args.alias, schedule=False, as_json=as_json)
    if as_json:
        # Data class: raw doc on stdout. No top-level `ok` per the
        # reserved-key contract. Shared by bare `profiles --json` and
        # the explicit `profiles list` subcommand.
        print(json.dumps(_profiles_report(), indent=2))
        return 0
    if sub == 'list':
        # Non-interactive alias of bare `profiles`. Skips the TTY picker
        # so scripts (and hugr doctor) get predictable output regardless
        # of where they run.
        from . import profile_tui
        if not list_profiles():
            print('no profiles configured. Run: owa-piggy setup --profile <alias>')
            return 0
        return profile_tui.print_plain_list()
    # Bare `owa-piggy profiles` - list (with interactive picker on TTY).
    return _do_profiles_list()


def _profiles_report():
    reg = load_profiles_conf()
    enabled = set(reg['OWA_PROFILES'])
    scheduled = set(reg.get('OWA_SCHEDULED', []))
    default = reg['OWA_DEFAULT_PROFILE']
    return {
        'default': default or None,
        'profiles': [
            {
                'alias': alias,
                'default': alias == default,
                'registered': alias in enabled,
                'scheduled': alias in scheduled,
                'has_config': profile_config_path(alias).is_file(),
            }
            for alias in list_profiles()
        ],
    }


def _do_profiles_list():
    """Print configured profiles, marking the default with '*'.

    On an interactive stdout+stdin we hand off to the multi-key TUI in
    `profile_tui.run_picker` so the user can manage profiles without
    memorising the subcommand surface. Non-TTY invocations (pipes,
    redirects, CI) fall through to `profile_tui.print_plain_list` -
    same body the picker itself falls back to when termios is missing,
    so the two output paths cannot drift.

    Empty-state: on a TTY, offer to walk through creating the first
    profile interactively. Non-TTY just prints the hint and exits 0.
    """
    from . import profile_tui
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()

    if not list_profiles():
        if not is_tty:
            print('no profiles configured. Run: owa-piggy setup --profile <alias>')
            return 0
        return profile_tui.empty_state_setup_flow()

    if is_tty:
        return profile_tui.run_picker()

    return profile_tui.print_plain_list()


def _do_profiles_set_default(alias, as_json=False):
    """Mark `alias` as the default profile. Profile must exist on disk."""
    t0 = time.monotonic()
    ok, err = set_default_profile(alias)
    if as_json:
        from owa_piggy.conventions import action_envelope, emit_action
        emit_action(action_envelope(
            command='profiles set-default', ok=ok,
            stats={'alias': alias} if ok else {},
            error=None if ok else {'code': 'set_default_failed', 'message': err},
            duration_ms=(time.monotonic() - t0) * 1000.0,
        ))
        return 0 if ok else 1
    if not ok:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1
    print(f'default profile set to {alias!r}')
    return 0


def _do_profiles_delete(alias, force=False, yes=False, as_json=False):
    """Remove profile <alias> from disk and from profiles.conf.

    Destructive. Refuses to delete the currently marked default without
    --force. Also requires --yes when not on a TTY so an accidental
    machine invocation can't wipe a profile silently.
    """
    t0 = time.monotonic()

    def _fail(code, message, exit_code=1):
        if as_json:
            from owa_piggy.conventions import action_envelope, emit_action
            emit_action(action_envelope(
                command='profiles delete', ok=False,
                error={'code': code, 'message': message},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            ))
        else:
            print(f'ERROR: {message}', file=sys.stderr)
        return exit_code

    # Validate first so bad-input cases keep emitting the same error
    # as before (the destructive gate is for valid-looking deletes).
    ok, verr = validate_alias(alias)
    if not ok:
        return _fail('invalid_alias', str(verr))
    if alias not in list_profiles():
        return _fail('profile_not_found', f'profile {alias!r} not found')
    reg = load_profiles_conf()
    if reg['OWA_DEFAULT_PROFILE'] == alias and not force:
        return _fail(
            'default_profile_protected',
            f'{alias!r} is the default profile. Set another default first '
            f'or pass --force to override.',
        )

    # Destructive gating: alias is valid and exists. Require explicit
    # consent when not on a TTY so a machine invocation can't wipe
    # a profile without saying so.
    if not yes and not sys.stdin.isatty():
        return _fail(
            'confirmation_required',
            'profiles delete is destructive; pass --yes to confirm '
            '(and --force if deleting the default profile)',
        )
    ok, err = delete_profile(
        alias,
        uninstall_launchd=True,
        promote_default=True,
    )
    if not ok:
        return _fail('delete_failed', f'profile {alias!r}: {err}')
    if as_json:
        from owa_piggy.conventions import action_envelope, emit_action
        emit_action(action_envelope(
            command='profiles delete', ok=True,
            stats={'alias': alias, 'removed': True},
            warnings=['Refresh tokens cached in keychain are not auto-purged; remove them manually if needed.'],
            duration_ms=(time.monotonic() - t0) * 1000.0,
        ))
        return 0
    print(f'removed profile {alias!r}.')
    return 0


def _do_profiles_schedule(alias, schedule, as_json=False):
    """Add or remove `alias` from the shared launchd reseed schedule.

    `schedule=True` ensures the shared agent exists and adds `alias` to
    OWA_SCHEDULED; `schedule=False` removes it (and removes the shared
    agent if the schedule empties). Both are pure config edits except for
    the one-time install/uninstall of the single shared plist.
    """
    from .launchd import schedule as launchd_schedule
    from .launchd import unschedule as launchd_unschedule
    t0 = time.monotonic()
    verb = 'profiles schedule' if schedule else 'profiles unschedule'

    def _fail(code, message):
        if as_json:
            from owa_piggy.conventions import action_envelope, emit_action
            emit_action(action_envelope(
                command=verb, ok=False,
                error={'code': code, 'message': message},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            ))
        else:
            print(f'ERROR: {message}', file=sys.stderr)
        return 1

    ok, verr = validate_alias(alias)
    if not ok:
        return _fail('invalid_alias', str(verr))
    if alias not in list_profiles():
        return _fail('profile_not_found', f'profile {alias!r} not found')

    rc = launchd_schedule(alias) if schedule else launchd_unschedule(alias)
    if rc != 0:
        return _fail('launchd_failed',
                     f'{verb} for {alias!r} failed (launchd error)')
    if as_json:
        from owa_piggy.conventions import action_envelope, emit_action
        emit_action(action_envelope(
            command=verb, ok=True,
            stats={'alias': alias, 'scheduled': schedule},
            duration_ms=(time.monotonic() - t0) * 1000.0,
        ))
        return 0
    if schedule:
        print(f'{alias!r} added to the launchd reseed schedule.')
    else:
        print(f'{alias!r} removed from the launchd reseed schedule.')
    return 0


_DISPATCH = {
    'token': _cmd_token,
    'status': _cmd_status,
    'debug': _cmd_debug,
    'setup': _cmd_setup,
    'reseed': _cmd_reseed,
    'decode': _cmd_decode,
    'remaining': _cmd_remaining,
    'edge': _cmd_edge,
    'audiences': _cmd_audiences,
    'version': _cmd_version,
    'profiles': _cmd_profiles,
    'install-owa-tools': _cmd_install_owa_tools,
}

# Sanity check: keep the COMMANDS tuple at the top of the file in sync
# with the dispatch table below. assert at import time so a missing entry
# fails loudly during development rather than at first invocation.
assert set(COMMANDS) == set(_DISPATCH), \
    'COMMANDS / _DISPATCH out of sync'


def _dispatch(raw):
    """Inject the default command, parse, migrate, and run a handler."""
    argv = _inject_default_command(raw)
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


def _run_with_modes(raw, agent, err_json):
    """Run a non-interactive command under --agent / --err-json.

    Mirrors owa_core.modes.run_with_output_modes: captures stdout and (for
    machine commands only) stderr, then wraps success output in the agent
    envelope or renders a structured error. Interactive / UI-launching
    commands are run unwrapped - capturing their stdin prompts or browser
    handoff would break them - so the mode flags are a no-op there.
    """
    import contextlib
    import io

    command = schema_mod.command_name(_inject_default_command(raw))
    if command not in schema_mod.MACHINE_COMMANDS:
        return _dispatch(raw)

    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = _dispatch(raw)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    out, err = out_buf.getvalue(), err_buf.getvalue()

    if err_json and rc != 0:
        payload = {
            "error": {
                "code": "ERROR",
                "message": (err.strip() or f"{command} failed"),
                "exit_code": rc,
                "tool": "owa-piggy",
                "command": command,
            }
        }
        json.dump(payload, sys.stderr, ensure_ascii=False, separators=(",", ":"))
        sys.stderr.write("\n")
        return rc

    if err:
        sys.stderr.write(err)
    if rc != 0 or not agent:
        if out:
            sys.stdout.write(out)
        return rc

    text = out.strip()
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Don't replay the raw stdout - it may be a secret (access token,
            # JWT) and the agent contract is "envelope or nothing on stdout".
            print("--agent requires JSON stdout; this command did not emit JSON "
                  "(try the command's --json flag)", file=sys.stderr)
            return 2
    else:
        data = None
    json.dump(schema_mod.envelope(command, data), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def main():
    raw = list(sys.argv[1:])
    # Top-level --doctor per hugr CONVENTIONS.md. Handle before
    # argparse so it composes with --json without touching the
    # subcommand surface.
    if "--doctor" in raw:
        from owa_piggy.doctor import emit_doctor
        as_json = "--json" in raw
        return emit_doctor(as_json)

    # Machine surface: `schema [<cmd>]` and `--help --json`, handled before
    # argparse so they compose without touching the subcommand grammar.
    handled = schema_mod.maybe_emit_schema(raw, commands=schema_mod.COMMAND_SCHEMA)
    if handled is not None:
        return handled

    # --agent / --err-json wrap JSON output for automation consumers.
    agent, err_json, raw = schema_mod.split_mode_flags(raw)
    if agent or err_json:
        return _run_with_modes(raw, agent, err_json)
    return _dispatch(raw)
