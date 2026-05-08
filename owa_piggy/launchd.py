"""Small launchd helpers shared by status and the profile manager.

The actual plist creation lives in ``scripts/setup-refresh.sh`` because
it needs to be directly runnable by hand and by packaged installs. This
module centralizes the Python-side label/path conventions and the call
into that script so the CLI and diagnostics cannot drift.
"""
import subprocess
import sys
from pathlib import Path

from .scripts import find_packaged_script

LABEL_PREFIX = 'com.damsleth.owa-piggy'
LEGACY_LABEL = LABEL_PREFIX
SETUP_REFRESH_SCRIPT_NAME = 'setup-refresh.sh'


def label_for(alias):
    """Return the per-profile LaunchAgent label."""
    return f'{LABEL_PREFIX}.{alias}'


def plist_path(alias):
    """Return the per-profile LaunchAgent plist path."""
    return Path.home() / 'Library' / 'LaunchAgents' / f'{label_for(alias)}.plist'


def legacy_plist_path():
    """Return the pre-profile single-label plist path."""
    return Path.home() / 'Library' / 'LaunchAgents' / f'{LEGACY_LABEL}.plist'


def is_installed(alias):
    """True when the per-profile plist exists on disk."""
    return plist_path(alias).exists()


def find_setup_refresh_script():
    """Locate setup-refresh.sh across checkout and packaged layouts."""
    return find_packaged_script(
        SETUP_REFRESH_SCRIPT_NAME,
        env_override='OWA_SETUP_REFRESH_SCRIPT',
    )


def run_setup_refresh(alias, *, install):
    """Run setup-refresh.sh for one profile. Returns the script exit code."""
    script = find_setup_refresh_script()
    if not script:
        print('ERROR: setup-refresh.sh not found. Reinstall owa-piggy or set '
              'OWA_SETUP_REFRESH_SCRIPT=/path/to/setup-refresh.sh',
              file=sys.stderr)
        return 1
    args = [str(script), '--profile', alias]
    if not install:
        args.insert(1, '--uninstall')
    try:
        return subprocess.call(args)
    except OSError as e:
        print(f'ERROR: failed to run {script}: {e}', file=sys.stderr)
        return 1
