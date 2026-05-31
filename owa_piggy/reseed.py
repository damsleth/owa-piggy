"""Edge-headless reseed (24h hard-cap recovery).

Two backends:

* Default (legacy MSAL cache): the heavy lifting lives in
  scripts/reseed-from-edge.sh (shipped as a data-file to
  share/owa-piggy/scripts/). This module finds the script and invokes
  it with the right per-profile environment so the sidecar Edge
  launches against the correct userdata dir and the nested
  `owa-piggy setup` writes into the right profile.

* Network capture (`OWA_AUTH_MODE=capture`): the profile config has
  this key set by `setup --email`. We dispatch to capture.capture_silent
  which drives a headless Edge entirely from Python and intercepts the
  rotated refresh token off the /oauth2/v2.0/token response. This is
  the only path that works for tenants whose MSAL.js encrypts the
  localStorage cache (modern Okta-federated SPAs).
"""
import os
import subprocess
import sys

from . import config as _config
from .cache import clear_cache
from .config import (
    iso_utc_now,
    list_profiles,
    load_config,
    load_profiles_conf,
    profiles_conf_path,
    save_config,
    set_active_profile,
)
from .scripts import find_reseed_script


def _profile_cdp_port(alias):
    """Derive a stable CDP debug port for `alias`, matching the formula
    in scripts/setup-refresh.sh (`9222 + cksum % 10000`).

    The legacy scrape backend (reseed-from-edge.sh) reads CDP_PORT from
    the environment. The per-profile launchd plists used to inject it;
    the single shared agent does not, so the scrape path derives it here
    instead. Capture mode is unaffected - it picks a free port at runtime.
    """
    return 9222 + (_posix_cksum(alias.encode()) % 10000)


def _posix_cksum(data):
    """POSIX `cksum` CRC over `data`, returning the same integer the
    `cksum` utility prints. Implements the standard CRC-32/CKSUM: the
    byte length is appended to the stream and the CRC is final-XOR'd.
    """
    crc = 0
    table = _CKSUM_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) ^ byte) & 0xFF]
    n = len(data)
    while n:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) ^ (n & 0xFF)) & 0xFF]
        n >>= 8
    return (~crc) & 0xFFFFFFFF


def _build_cksum_table():
    poly = 0x04C11DB7
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFFFFFF if crc & 0x80000000 \
                else (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


_CKSUM_TABLE = _build_cksum_table()


def do_reseed(alias):
    """Run the headless reseed flow for profile <alias>.

    Two backends, picked by the profile's OWA_AUTH_MODE:

      * 'capture' - drive Edge headless from Python and intercept the
        /oauth2/v2.0/token response. Used by profiles set up via
        `setup --email` (encrypted MSAL cache, e.g. Okta-federated).

      * (anything else / unset) - shell out to reseed-from-edge.sh
        which scrapes the legacy localStorage MSAL cache. This is
        what every profile that worked before this change still uses.

    Returns 0 on success, non-zero on failure (script exit code or 1).
    """
    set_active_profile(alias)
    config, _ = load_config()
    auth_mode = (config.get('OWA_AUTH_MODE') or '').strip()
    if auth_mode == 'capture':
        return _do_reseed_capture(alias, config)
    return _do_reseed_scrape(alias)


def _do_reseed_capture(alias, config):
    """Network-capture reseed: headless Edge, intercept /token, persist
    the rotated refresh token. No window appears under any condition;
    if the sidecar session has expired we exit non-zero and ask the
    user to re-run `setup --email` interactively."""
    # Capture-mode reseed can be reached either through the single-profile
    # CLI path (which already cleared the cache) or via do_reseed_all(),
    # which calls us directly per profile. Clear again here so both call
    # paths guarantee the pre-reseed AT cannot survive the identity change.
    clear_cache()

    # Local import: keeps the CDP/capture machinery off the import path
    # for the legacy reseed users that don't need it.
    from . import capture

    print(f'[{alias}] reseed via network capture (OWA_AUTH_MODE=capture)',
          file=sys.stderr)
    is_tty = sys.stdin.isatty()
    # Env override wins so an operator can experiment with a UA without
    # rewriting the profile config; otherwise the persisted per-profile
    # UA (set at `setup --user-agent ...`) is what keeps silent reseed
    # consistent with the original sign-in.
    user_agent = os.environ.get('OWA_USER_AGENT') or config.get('OWA_USER_AGENT') or None
    status, captured = capture.capture_silent(alias, user_agent=user_agent)
    # Transient 'error' on the first attempt (CDP hiccup, slow /token
    # round-trip past the timeout, etc.) is the most common cause of
    # hourly cron failures in the refresh.log. One retry recovers nearly
    # all of them. Skip the retry for 'reauth' (user action required) and
    # 'headless_blocked' (handled by the headless->offscreen fallback
    # below) since neither benefits from re-running the same path.
    if status == 'error':
        print(f'[{alias}] capture returned transient error; retrying once...',
              file=sys.stderr)
        status, captured = capture.capture_silent(alias, user_agent=user_agent)
    if status == 'headless_blocked' and not is_tty:
        # No human present (launchd) - try the offscreen-non-headless
        # silent path before giving up, since we can't fall back to
        # interactive sign-in unattended. The window stays parked at
        # -32000,-32000 so the user's display stays clean.
        print(f'[{alias}] headless Edge blocked by tenant; retrying '
              f'non-headless (offscreen)...', file=sys.stderr)
        status, captured = capture.capture_silent(alias, headless=False,
                                                   user_agent=user_agent)
    if status == 'reauth' or (status == 'headless_blocked' and is_tty):
        # 'headless_blocked' on a TTY skips straight here - the
        # offscreen-silent retry leaves stale in-flight auth state in
        # the user-data-dir which then trips AAD error 500121 when
        # capture_signin reuses the same profile dir.
        email = config.get('OWA_EMAIL', '')
        if email and is_tty:
            print(f'[{alias}] sidecar cookies expired - opening Edge for '
                  f'interactive sign-in (complete MFA in the window)...',
                  file=sys.stderr)
            try:
                captured = capture.capture_signin(alias, email, timeout=300,
                                                   user_agent=user_agent)
                status = 'ok'
            except (RuntimeError, TimeoutError, ConnectionError,
                    KeyboardInterrupt) as e:
                kind = 'cancelled' if isinstance(e, KeyboardInterrupt) \
                    else 'failed'
                print(f'ERROR: [{alias}] interactive sign-in {kind}: {e}',
                      file=sys.stderr)
                return 1
        else:
            hint = f' --email {email}' if email else ' --email <addr>'
            print(f'ERROR: [{alias}] sidecar session expired; interactive '
                  f'sign-in needed.', file=sys.stderr)
            print(f'       Run: owa-piggy setup --profile {alias}{hint}',
                  file=sys.stderr)
            return 1
    if status != 'ok' or not captured:
        if os.environ.get('OWA_CAPTURE_DEBUG'):
            print(f'ERROR: [{alias}] capture-based reseed failed.',
                  file=sys.stderr)
        else:
            print(f'ERROR: [{alias}] capture-based reseed failed. '
                  f'Set OWA_CAPTURE_DEBUG=1 and re-run for diagnostics.',
                  file=sys.stderr)
        return 1

    # Merge captured fields into the existing config (preserves OWA_EMAIL,
    # OWA_CLIENT_ID overrides, etc.) and stamp issuance time so `status`
    # can compute the 24h SPA hard-cap remaining.
    config.update(captured)
    config['OWA_RT_ISSUED_AT'] = iso_utc_now()
    save_config(config)
    print(f'[{alias}] reseeded; new RT persisted to {_config.CONFIG_PATH}',
          file=sys.stderr)
    return 0


def _do_reseed_scrape(alias):
    """Legacy reseed: shell out to scripts/reseed-from-edge.sh.

    The shell script handles its own Edge lifecycle (headless attempt,
    visible-signin fallback for cookie expiry, post-scrape verify). We
    just hand it the per-profile env vars and surface its exit code.
    """
    script = find_reseed_script()
    if not script:
        print(
            'ERROR: reseed-from-edge.sh not found. Searched:\n'
            '    $OWA_RESEED_SCRIPT\n'
            '    <module_dir>/scripts/ (repo checkout)\n'
            '    <sys.prefix>/share/owa-piggy/scripts/ (pipx/pip)\n'
            '    /usr/local/share/owa-piggy/scripts/ (brew intel)\n'
            '    /opt/homebrew/share/owa-piggy/scripts/ (brew apple silicon)\n'
            '  Reinstall, or set OWA_RESEED_SCRIPT=/path/to/reseed-from-edge.sh',
            file=sys.stderr,
        )
        return 1

    env = os.environ.copy()
    env['OWA_PIGGY_PROFILE'] = alias
    env['OWA_PIGGY_EDGE_PROFILE_DIR'] = str(_config.profile_edge_dir(alias))
    # The single shared launchd agent no longer injects a per-profile
    # CDP_PORT (the old per-profile plists did). Derive it here so the
    # sidecar's debug port stays stable and collision-free per profile.
    # An explicit CDP_PORT already in the environment wins (manual runs,
    # debugging).
    env.setdefault('CDP_PORT', str(_profile_cdp_port(alias)))

    try:
        return subprocess.call([str(script)], env=env)
    except OSError as e:
        print(f'ERROR: failed to run {script}: {e}', file=sys.stderr)
        return 1


def do_reseed_all():
    """Run the headless reseed flow for every configured profile, in order.

    Sequential rather than parallel: each profile drives its own Edge
    instance with a unique CDP port, but spinning up multiple Edge
    processes against shared OS resources (Keychain prompts, login.live
    cookie jar, port allocation) is the kind of complexity this tool
    has consistently chosen to avoid. The wallclock cost is ~15s per
    profile, which is fine for a hand-run command.

    Returns the max exit code across profiles so a partial failure is
    still surfaced to scripts (`if owa-piggy reseed --all; then ...`).
    Each profile's stderr is prefixed with [alias] by the shell script
    so output stays attributable.
    """
    on_disk = list_profiles()
    if not on_disk:
        print('no profiles configured. Run: owa-piggy setup --profile <alias>',
              file=sys.stderr)
        return 1
    # Honor the registry: if profiles.conf has an OWA_PROFILES list, that's
    # the set of *active* profiles - anything on disk but absent from the
    # list is disabled (no launchd agent, no auto-token-rotation) and
    # should not be reseeded automatically. A missing registry means a
    # legacy install that predates profiles.conf, in which case treat
    # everything on disk as active (matches status's behavior). A present
    # but empty registry means every profile is disabled.
    profiles_conf_exists = profiles_conf_path().exists()
    registered = load_profiles_conf().get('OWA_PROFILES', [])
    if profiles_conf_exists:
        active = [a for a in on_disk if a in registered]
        skipped = [a for a in on_disk if a not in registered]
        for alias in skipped:
            print(f'skipping disabled profile: {alias}', file=sys.stderr)
    else:
        active = on_disk
    if not active:
        print('no active profiles to reseed (registry has no enabled '
              'profiles).', file=sys.stderr)
        return 1
    return _reseed_aliases(active)


def do_reseed_scheduled():
    """Reseed only the profiles in OWA_SCHEDULED - the set the single
    shared launchd agent rotates hourly (`reseed --scheduled`).

    This is the launchd entry point. It is deliberately narrower than
    do_reseed_all(): a profile can be enabled (reseeded by `--all`,
    visible in `status`) without being scheduled for unattended hourly
    rotation. The on-disk profile set is intersected with OWA_SCHEDULED;
    any scheduled alias whose profile dir has gone missing is skipped
    with a warning rather than aborting the whole run.

    An empty schedule is a valid state (no profile opted in), so this
    returns 0 - the agent fires hourly and a no-op run is not a failure.
    """
    on_disk = set(list_profiles())
    scheduled = load_profiles_conf().get('OWA_SCHEDULED', [])
    aliases = []
    for alias in scheduled:
        if alias in on_disk:
            aliases.append(alias)
        else:
            print(f'skipping scheduled profile with no config on disk: '
                  f'{alias}', file=sys.stderr)
    if not aliases:
        print('no scheduled profiles to reseed (OWA_SCHEDULED is empty).',
              file=sys.stderr)
        return 0
    return _reseed_aliases(aliases)


def _reseed_aliases(aliases):
    """Reseed each alias in `aliases` sequentially. Returns the max exit
    code so a partial failure is still surfaced. Shared by do_reseed_all
    and do_reseed_scheduled.
    """
    rc = 0
    for i, alias in enumerate(aliases):
        if i:
            print(file=sys.stderr)
        print(f'=== reseed [profile={alias}] ({i + 1}/{len(aliases)}) ===',
              file=sys.stderr)
        # set_active_profile rebinds CONFIG_PATH so the nested setup writes
        # land in the right per-profile config. do_reseed shells out to a
        # script that calls owa-piggy setup --profile <alias>, which sets
        # the active profile itself - but the parent caller (cli.py
        # _cmd_reseed) clears the cache for the explicit alias, and we do
        # not have that hook here, so re-bind explicitly per profile to
        # keep the cache layer coherent.
        set_active_profile(alias)
        rc = max(rc, do_reseed(alias))
    return rc
