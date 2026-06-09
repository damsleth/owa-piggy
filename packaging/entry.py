"""Frozen-binary entry point for owa-piggy.

Mirrors the ``owa-piggy = "owa_piggy:main"`` console script so the
PyInstaller bundle behaves exactly like a pip install.
"""
import sys

from owa_piggy import main

if __name__ == "__main__":
    sys.exit(main() or 0)
