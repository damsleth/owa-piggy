"""owa-piggy - get an Outlook/Graph access token without app registration.

The package entry point is `main`, wired up as the `owa-piggy` console
script via pyproject.toml. See `cli.py` for the dispatch layer and the
per-concern modules (scopes, jwt, config, oauth, reseed, setup, status)
for the pure-function pieces.

`__version__` reads the installed distribution metadata so the value
always matches whatever `pyproject.toml` declared at install time. In a
bare repo checkout (no install), that lookup fails; we fall back to
scraping the adjacent `pyproject.toml` so the launchd dev-path
(`PYTHONPATH=<repo> python3 -m owa_piggy`) still reports a real version.
"""

from __future__ import annotations


def _read_version() -> str:
    """Version string for `--version`.

    The source pyproject.toml wins when we're running from a checkout (our
    dev and launchd-fallback mode): checking installed metadata first would
    report a stale `pip install` from site-packages instead of the code
    actually running. Installed builds have no sibling pyproject.toml and
    fall through to the distribution metadata.
    """
    import re
    from importlib.metadata import PackageNotFoundError, version
    from pathlib import Path

    pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        m = re.search(r'^\s*version\s*=\s*"([^"]+)"', pp.read_text(), re.M)
    except OSError:
        m = None
    if m:
        return m.group(1)
    try:
        return version("owa-piggy")
    except PackageNotFoundError:
        return "unknown"


__version__ = _read_version()

# Defined after __version__ so cli.py can safely `from . import __version__`.
from .cli import main  # noqa: E402

__all__ = ["main", "__version__"]
