"""owa-piggy - get an Outlook/Graph access token without app registration.

The package entry point is `main`, wired up as the `owa-piggy` console
script via pyproject.toml. See `cli.py` for the dispatch layer and the
per-concern modules (scopes, jwt, config, oauth, reseed, setup, status)
for the pure-function pieces.
"""
from .cli import main

__all__ = ["main"]
