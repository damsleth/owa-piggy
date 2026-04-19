#!/bin/bash
# Install owa-piggy as an editable pipx package so the `owa-piggy` console
# script lands on PATH. Replaces the old direct-symlink approach (which
# relied on the pre-package flat owa_piggy.py).
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx not found. Install it first: brew install pipx" >&2
  exit 1
fi

pipx install --force -e "$REPO_DIR"
echo
echo "owa-piggy installed via pipx. Run: owa-piggy --help"
