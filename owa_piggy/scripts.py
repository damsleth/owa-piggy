"""Locate helper scripts across checkout and packaged installs.

The shell helpers are shipped as data files for pip/pipx/brew installs,
but local development runs them straight from the repo checkout. Keeping
the search order here prevents each command from growing its own copy of
the same path dance.
"""
import os
import sys
from pathlib import Path


def find_packaged_script(name, *, env_override=None):
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

    repo = Path(__file__).resolve().parent.parent / 'scripts' / name
    candidates = [
        repo,
        Path(sys.prefix) / 'share' / 'owa-piggy' / 'scripts' / name,
        Path('/usr/local/share/owa-piggy/scripts') / name,
        Path('/opt/homebrew/share/owa-piggy/scripts') / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
