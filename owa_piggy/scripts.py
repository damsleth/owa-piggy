"""Locate helper scripts across checkout and packaged installs.

The shell helpers are shipped as data files for pip/pipx/brew installs,
but local development runs them straight from the repo checkout. Keeping
the search order here prevents each command from growing its own copy of
the same path dance.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_packaged_script(name: str, *, env_override: str | None = None) -> Path | None:
    """Return the first existing helper script path, or None.

    Search order is: explicit env var override, repo checkout, pip/pipx
    data-files under ``sys.prefix``, then the two common Homebrew prefixes.
    """
    if env_override:
        override = os.environ.get(env_override)
        if override:
            p = Path(override)
            if p.is_file():
                return p

    repo = Path(__file__).resolve().parent.parent / "scripts" / name
    candidates = [
        repo,
        Path(sys.prefix) / "share" / "owa-piggy" / "scripts" / name,
        Path("/usr/local/share/owa-piggy/scripts") / name,
        Path("/opt/homebrew/share/owa-piggy/scripts") / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# Per-script wrappers. Each command's `find_*_script` lives next to
# `find_packaged_script` so the asymmetry between reseed.py and launchd.py
# is gone - the only thing that varies between them is the filename and
# the env var name, both encoded as a single line below.


def find_reseed_script() -> Path | None:
    """Locate reseed-from-edge.sh across install layouts. Honors
    OWA_RESEED_SCRIPT for explicit overrides."""
    return find_packaged_script("reseed-from-edge.sh", env_override="OWA_RESEED_SCRIPT")


def find_setup_refresh_script() -> Path | None:
    """Locate setup-refresh.sh across install layouts. Honors
    OWA_SETUP_REFRESH_SCRIPT for explicit overrides."""
    return find_packaged_script("setup-refresh.sh", env_override="OWA_SETUP_REFRESH_SCRIPT")
