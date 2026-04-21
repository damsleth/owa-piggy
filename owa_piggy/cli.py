"""Argument parsing and dispatch for the `owa-piggy` command."""
import json
import shutil
import sys
import time

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
from .scopes import KNOWN_SCOPES, resolve_scope
from .setup import interactive_setup
from .status import do_debug, do_status


def print_help():
    print("""usage: owa-piggy [options]

  Piggybacks on OWA's first-party SPA client to get an Outlook/Graph
  access token without registering an app in Azure AD.

options:
  (none)              print access token to stdout
  --json              print full token response as JSON
  --env               print ACCESS_TOKEN and EXPIRES_IN as KEY=value lines
  --decode            decode and print the JWT header and payload
  --remaining         print minutes remaining on the current token
  --graph             use Microsoft Graph scope
  --teams             use Microsoft Teams scope
  --<name>            use any known FOCI scope (see --list-scopes)
  --list-scopes       list all known FOCI-accessible audiences
  --scope <scope>     override scope explicitly (takes precedence)
  --save-config       interactive first-time setup, saves to config file
  --setup             alias for --save-config
  --reseed            fetch a fresh refresh token headlessly from the Edge
                      sidecar profile (for when the 24h SPA hard-expiry hits)
  --status            compact health: authtoken/scope/refreshtoken expiries
                      (ISO8601), or 'no valid token' if setup is broken
  --debug             dump setup diagnostics: config, token shape, live probe,
                      launchd agent, PATH install, sidecar profile

profile management:
  --profile <alias>   target a specific profile for this invocation (also
                      honored via OWA_PROFILE env var). See `--list-profiles`
                      for what's configured. With --setup, creates the
                      profile if it does not yet exist.
  --list-profiles     print configured profiles, marking the default with *
  --set-default <a>   make <alias> the default profile
  --delete-profile <a>  remove a profile's config and Edge sidecar dir.
                      Refuses to delete the default unless --force is given.
                      Does not touch launchd - run setup-refresh.sh
                      --uninstall --profile <alias> to drop that too.

  --help              show this help

config:
  ~/.config/owa-piggy/
    profiles.conf                 OWA_DEFAULT_PROFILE + OWA_PROFILES
    profiles/<alias>/config       per-profile KV (OWA_REFRESH_TOKEN, ...)

  Keys inside each profile's `config`:
    OWA_REFRESH_TOKEN      MSAL refresh token secret from browser localStorage
    OWA_TENANT_ID          your Azure AD tenant ID
    OWA_CLIENT_ID          override client ID (default: OWA's public client)
    OWA_DEFAULT_AUDIENCE   override default audience (short name from
                           --list-scopes, e.g. 'outlook', or a full https URL).
                           --<name> / --scope on the command line still win.

  Environment variables take precedence over the config file.
  OWA_PROFILE selects which profile to load.

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
  4. Run: owa-piggy --save-config --profile <alias>

examples:
  owa-piggy                                         # raw token to stdout
  owa-piggy --remaining                             # 73min
  owa-piggy --profile work                          # token for the 'work' profile
  OWA_PROFILE=work owa-piggy                        # same, via env
  owa-piggy --list-profiles                         # show configured profiles
  owa-piggy --set-default work                      # change default profile
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
  owa-piggy --reseed --profile work                 # refresh one profile
  pbpaste | owa-piggy --save-config --profile work  # pipe tokens from clipboard

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


_VALUE_FLAGS = {'--profile', '--set-default', '--delete-profile', '--scope'}

_STATIC_FLAGS = {
    '--help', '-h',
    '--list-profiles', '--list-scopes',
    '--force',
    '--save-config', '--setup',
    '--reseed', '--debug', '--status',
    '--json', '--env', '--decode', '--remaining',
}


def _validate_known_flags(args):
    """Reject unknown `-`/`--` flags up front.

    Without this, an unrecognised flag (typo like `--somewrongparam`) just
    falls through to the main flow and quietly mints a default token,
    which hides bugs in caller scripts. Return an error string or None.
    Values that follow a space-separated value-taking flag are skipped so
    `--scope https://...` doesn't get flagged as unknown.
    """
    known = set(_STATIC_FLAGS) | _VALUE_FLAGS
    known.update(f'--{n}' for n in KNOWN_SCOPES)
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if not a.startswith('-'):
            continue
        # Support `--flag=value`; validate the flag name only.
        flag = a.split('=', 1)[0] if a.startswith('--') and '=' in a else a
        if flag not in known:
            return f'parameter [{flag}] not found'
        if flag in _VALUE_FLAGS and '=' not in a:
            skip_next = True
    return None


def _extract_option(args, flag):
    """Pop `flag <value>` from args and return `value`, or None if flag absent.

    Also supports `--flag=value`. Mutates `args` in place. Returns (None,
    None) when the flag isn't present, and (value, None) on success.
    Returns (None, err) when the flag is present but the value is missing,
    so callers can surface the error without continuing.
    """
    for i, a in enumerate(args):
        if a == flag:
            if i + 1 >= len(args):
                return None, f'{flag} requires a value'
            value = args[i + 1]
            del args[i:i + 2]
            return value, None
        if a.startswith(flag + '='):
            value = a.split('=', 1)[1]
            del args[i]
            return value, None
    return None, None


def _do_list_profiles():
    """Print configured profiles, marking the default with '*'."""
    profiles = list_profiles()
    if not profiles:
        print('no profiles configured. Run: owa-piggy --setup --profile <alias>')
        return 0
    reg = load_profiles_conf()
    default = reg['OWA_DEFAULT_PROFILE']
    for alias in profiles:
        marker = ' *' if alias == default else '  '
        print(f'{marker} {alias}')
    return 0


def _do_set_default(alias):
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


def _do_delete_profile(alias, force=False):
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
            f'with `owa-piggy --set-default <alias>` first, or pass --force to '
            f'override.',
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


def main():
    args = sys.argv[1:]

    if '--help' in args or '-h' in args:
        print_help()
        return 0

    flag_err = _validate_known_flags(args)
    if flag_err:
        print(f'ERROR: {flag_err}', file=sys.stderr)
        return 1

    # --- Profile-management subcommands ---------------------------------
    # These act on profiles.conf / the profiles directory directly; they
    # never mint a token, so they run before the usual dispatch.

    migrate_if_needed()  # idempotent; no-op on fresh or already-migrated installs

    if '--list-profiles' in args:
        return _do_list_profiles()

    # --list-scopes is purely informational (prints the KNOWN_SCOPES table).
    # Handle it before profile resolution so it works on installs with
    # multiple profiles and no default, where resolve_profile() would
    # otherwise error out on ambiguity.
    if '--list-scopes' in args:
        max_name = max(len(n) for n in KNOWN_SCOPES)
        max_aud = max(len(aud) for aud, _ in KNOWN_SCOPES.values())
        for name, (aud, desc) in KNOWN_SCOPES.items():
            flag = f'--{name}'
            print(f'  {flag:<{max_name + 3}} {aud:<{max_aud + 2}} {desc}')
        return 0

    set_default, err = _extract_option(args, '--set-default')
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1
    if set_default is not None:
        return _do_set_default(set_default)

    delete_alias, err = _extract_option(args, '--delete-profile')
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1
    force = False
    if '--force' in args:
        args.remove('--force')
        force = True
    if delete_alias is not None:
        return _do_delete_profile(delete_alias, force=force)

    # --- Profile selection for the main flow ----------------------------

    cli_profile, err = _extract_option(args, '--profile')
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1

    do_setup = '--save-config' in args or '--setup' in args
    alias, perr = resolve_profile(cli_profile, allow_missing=do_setup)
    if perr:
        print(f'ERROR: {perr}', file=sys.stderr)
        return 1
    set_active_profile(alias)

    want_json = '--json' in args
    want_env = '--env' in args
    want_decode = '--decode' in args
    want_remaining = '--remaining' in args

    # --reseed shells out to the Edge-headless reseed script and exits with
    # its status. Handled before load_config so an expired on-disk token
    # cannot block recovery. Clear the AT cache first so we never serve a
    # token minted for the pre-reseed identity/session after the user has
    # explicitly asked for a fresh credential.
    if '--reseed' in args:
        clear_cache()
        return do_reseed(alias)

    if '--debug' in args:
        return do_debug(alias)

    if '--status' in args:
        return do_status(alias)

    scope, err = resolve_scope(args)
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1

    config, persist = load_config()

    if do_setup:
        # The user is explicitly re-identifying; any cached AT belongs to
        # the pre-setup identity and must not leak past this point.
        clear_cache()
        if not interactive_setup(config, alias):
            return 1
        # Register the profile in profiles.conf so --list-profiles sees
        # it and resolve_profile can find it. If this is the first profile
        # ever created, make it the default.
        ensure_profile_registered(alias, make_default_if_first=True)
        config, persist = load_config()

    refresh_token = config.get('OWA_REFRESH_TOKEN', '').strip()
    tenant_id = config.get('OWA_TENANT_ID', '').strip()
    client_id = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()

    # Access-token cache short-circuit. Output modes that need only the AT
    # (or something derivable from it locally) can be served from
    # ~/.config/owa-piggy/profiles/<alias>/cache.json without round-tripping
    # AAD, avoiding 429s when callers shell out to `owa-piggy` in tight
    # loops.
    #
    # Bypass for: --json (full response includes a fresh refresh_token we
    # don't cache), --save-config/--setup (the whole point is to rotate),
    # and anywhere earlier in main() that returns before this block
    # (--status, --debug, --reseed all probe or mint on purpose).
    #
    # Cache is keyed by (tenant, client, scope) AND scoped per-profile
    # (separate cache.json under each profile dir), so switching profiles
    # or tenants naturally misses the old entries.
    cache_eligible = not want_json and not do_setup and tenant_id
    if cache_eligible:
        cached_at = get_cached_token(tenant_id, client_id, scope)
        if cached_at:
            if want_env:
                exp = get_cached_exp(tenant_id, client_id, scope) or 0
                print(f'ACCESS_TOKEN={cached_at}')
                print(f'EXPIRES_IN={max(0, int(exp - time.time()))}')
            elif want_decode:
                print(decode_jwt(cached_at))
            elif want_remaining:
                remaining = token_minutes_remaining(cached_at)
                print(f'{remaining}min' if remaining is not None else 'unknown')
            else:
                print(cached_at)
            return 0

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
            print(f'ERROR: OWA_REFRESH_TOKEN not set for profile {alias!r}. '
                  f'Run: owa-piggy --save-config --profile {alias}',
                  file=sys.stderr)
        if not tenant_id:
            print(f'ERROR: OWA_TENANT_ID not set for profile {alias!r}. '
                  f'Run: owa-piggy --save-config --profile {alias}',
                  file=sys.stderr)
        return 1

    result = exchange_token(refresh_token, tenant_id, client_id, scope)
    if not result:
        return 1

    access_token = result.get('access_token')
    new_refresh = result.get('refresh_token')

    if not access_token:
        print(f'ERROR: no access_token in response: {list(result.keys())}', file=sys.stderr)
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
        print(f'\n\tOWA-PIGGY 🐽  CONFIGURED [{alias}]', file=sys.stderr)
        print('\n\tENJOY YOUR APP-REG-FREE SCOPES\n', file=sys.stderr)

    return 0
