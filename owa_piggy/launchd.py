"""Small launchd helpers shared by status and the profile manager.

There is a single shared LaunchAgent, labelled ``com.damsleth.owa-piggy
.scheduled``, that runs ``owa-piggy reseed --scheduled`` at the top of
every hour. Which profiles it actually reseeds is the ``OWA_SCHEDULED``
set in ``profiles.conf`` - the plist itself is static and is written once,
never rewritten when the schedule changes. This keeps a single row in
macOS's Login Items & Extensions (BackgroundTaskManagement) regardless of
profile count, and means toggling a profile's schedule never re-poke
launchd (so macOS never re-prompts for background-item approval).

The actual plist creation lives in ``scripts/setup-refresh.sh`` because
it needs to be directly runnable by hand and by packaged installs. This
module centralizes the Python-side label/path conventions, the registry
edits, and the call into that script so the CLI and diagnostics cannot
drift.
"""

import subprocess
import sys
from pathlib import Path

from . import config as _config
from .scripts import find_setup_refresh_script

LABEL_PREFIX = "com.damsleth.owa-piggy"
SHARED_LABEL = f"{LABEL_PREFIX}.scheduled"


def shared_plist_path():
    """Return the shared LaunchAgent plist path."""
    return Path.home() / "Library" / "LaunchAgents" / f"{SHARED_LABEL}.plist"


def shared_agent_installed():
    """True when the shared LaunchAgent plist exists on disk."""
    return shared_plist_path().exists()


def is_scheduled(alias):
    """True when `alias` is in OWA_SCHEDULED - i.e. the shared agent will
    reseed it. This is the consolidated replacement for the old
    "is a per-profile plist installed" check.
    """
    return _config.is_scheduled(alias)


def _run_setup_refresh_script(*script_args):
    """Invoke setup-refresh.sh with `script_args`. Returns its exit code,
    or 1 if the script can't be found / can't be launched.
    """
    script = find_setup_refresh_script()
    if not script:
        print(
            "ERROR: setup-refresh.sh not found. Reinstall owa-piggy or set "
            "OWA_SETUP_REFRESH_SCRIPT=/path/to/setup-refresh.sh",
            file=sys.stderr,
        )
        return 1
    try:
        return subprocess.call([str(script), *script_args])
    except OSError as e:
        print(f"ERROR: failed to run {script}: {e}", file=sys.stderr)
        return 1


def schedule(alias):
    """Add `alias` to the schedule and ensure the shared agent is installed.

    Edits OWA_SCHEDULED (a pure config write - never touches launchd) and,
    only if the shared plist is not already present, installs it via
    setup-refresh.sh. Returns 0 on success, non-zero on failure.
    """
    _config.schedule_profile(alias)
    if shared_agent_installed():
        # Already installed - the static plist reads OWA_SCHEDULED at run
        # time, so nothing else to do. No launchd churn, no re-prompt.
        return 0
    rc = _run_setup_refresh_script("--install-shared")
    if rc != 0:
        _config.unschedule_profile(alias)
    return rc


def unschedule(alias):
    """Remove `alias` from the schedule. If the schedule becomes empty,
    bootout + remove the shared agent (no point keeping an hourly job that
    reseeds nothing). Returns 0 on success, non-zero on failure.
    """
    _config.unschedule_profile(alias)
    remaining = _config.load_profiles_conf().get("OWA_SCHEDULED", [])
    if remaining:
        return 0
    if shared_agent_installed():
        return _run_setup_refresh_script("--uninstall-shared")
    return 0
