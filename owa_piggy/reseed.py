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
from pathlib import Path

from . import config as _config
from .cache import clear_cache
from .config import (
    iso_utc_now,
    list_profiles,
    load_config,
    save_config,
    set_active_profile,
)

RESEED_SCRIPT_NAME = 'reseed-from-edge.sh'


def find_reseed_script():
    """Locate reseed-from-edge.sh across install layouts.

    Search order:
      1. OWA_RESEED_SCRIPT env var (explicit override)
      2. ./scripts/ next to the package (repo checkout)
      3. <sys.prefix>/share/owa-piggy/scripts/ (pip / pipx data-files)
      4. Homebrew share dirs (/usr/local/share, /opt/homebrew/share)

    pyproject.toml ships the scripts as data-files to share/owa-piggy/scripts/
    so installs via pipx/pip/brew get a working --reseed. The repo-checkout
    path stays first so local development picks up edits immediately."""
    override = os.environ.get('OWA_RESEED_SCRIPT')
    if override:
        p = Path(override)
        if p.is_file():
            return p

    # Repo checkout: scripts/ sits one level above the package directory.
    repo_scripts = Path(__file__).resolve().parent.parent / 'scripts' / RESEED_SCRIPT_NAME

    candidates = [
        repo_scripts,
        Path(sys.prefix) / 'share' / 'owa-piggy' / 'scripts' / RESEED_SCRIPT_NAME,
        Path('/usr/local/share/owa-piggy/scripts') / RESEED_SCRIPT_NAME,
        Path('/opt/homebrew/share/owa-piggy/scripts') / RESEED_SCRIPT_NAME,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


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
    status, captured = capture.capture_silent(alias)
    if status == 'reauth':
        email = config.get('OWA_EMAIL', '')
        hint = f' --email {email}' if email else ' --email <addr>'
        print(f'ERROR: [{alias}] sidecar session expired; interactive '
              f'sign-in needed.', file=sys.stderr)
        print(f'       Run: owa-piggy setup --profile {alias}{hint}',
              file=sys.stderr)
        return 1
    if status != 'ok' or not captured:
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
            f'ERROR: {RESEED_SCRIPT_NAME} not found. Searched:\n'
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
    profiles = list_profiles()
    if not profiles:
        print('no profiles configured. Run: owa-piggy setup --profile <alias>',
              file=sys.stderr)
        return 1
    rc = 0
    for i, alias in enumerate(profiles):
        if i:
            print(file=sys.stderr)
        print(f'=== reseed [profile={alias}] ({i + 1}/{len(profiles)}) ===',
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
