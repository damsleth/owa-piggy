"""Edge-headless reseed (24h hard-cap recovery).

The heavy lifting lives in scripts/reseed-from-edge.sh (shipped as a
data-file to share/owa-piggy/scripts/). This module just finds the
script and invokes it with the right per-profile environment so the
sidecar Edge launches against the correct userdata dir and the nested
`owa-piggy setup` writes into the right profile.
"""
import os
import subprocess
import sys
from pathlib import Path

from . import config as _config

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
    """Run the Edge-headless reseed flow for profile <alias>.

    Boots a sidecar Edge rooted at `profiles/<alias>/edge-profile/`,
    scrapes a fresh FOCI refresh token via CDP, and pipes it into
    `owa-piggy setup --profile <alias>`. On success the new token is
    on disk and a fresh access token has been printed - we just return
    the script's exit code and let it own the user feedback.

    The alias is handed to the shell script via environment variables
    (OWA_PIGGY_PROFILE, OWA_PIGGY_EDGE_PROFILE_DIR) so the script can
    stay argv-free and cleanly forward --profile into the nested
    `owa-piggy setup` call.
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
