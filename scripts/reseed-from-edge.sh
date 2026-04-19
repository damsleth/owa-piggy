#!/bin/bash
# reseed-from-edge.sh - acquire a fresh FOCI refresh token by driving Edge
# headlessly against the owa-piggy sidecar profile, then hand the values to
# `owa-piggy --save-config`.
#
# Prerequisite (one time):
#   mkdir -p "$HOME/.config/owa-piggy/edge-profile"
#   /Applications/Microsoft\ Edge.app/Contents/MacOS/Microsoft\ Edge \
#     --user-data-dir="$HOME/.config/owa-piggy/edge-profile" \
#     https://outlook.cloud.microsoft
#   # log in, close Edge.
#
# Thereafter, this script does the rest headlessly.

set -e

EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROFILE_DIR="$HOME/.config/owa-piggy/edge-profile"
PORT="${CDP_PORT:-9222}"
SCRAPE="$(cd "$(dirname "$0")" && pwd)/scrape_edge.py"

if [ ! -x "$EDGE" ]; then
  echo "ERROR: Edge not found at $EDGE" >&2
  exit 1
fi
if [ ! -d "$PROFILE_DIR" ]; then
  echo "ERROR: sidecar profile not found at $PROFILE_DIR" >&2
  echo "  Run the one-time setup from this script's header first." >&2
  exit 1
fi
if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: port $PORT is busy. Kill the occupant or set CDP_PORT." >&2
  exit 1
fi

# Start Edge headlessly against the sidecar profile. Discard stderr noise;
# crash chatter is not useful to our flow.
"$EDGE" \
  --headless=new \
  --disable-gpu \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR" \
  "https://outlook.cloud.microsoft" \
  >/dev/null 2>&1 &
EDGE_PID=$!

cleanup() {
  if kill -0 "$EDGE_PID" 2>/dev/null; then
    kill "$EDGE_PID" 2>/dev/null || true
    # give it a moment, then force if still alive
    sleep 0.5
    kill -9 "$EDGE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Scrape and feed straight into owa-piggy. scrape_edge.py polls /json until
# the OWA tab is loaded, so no manual sleep is needed.
if command -v owa-piggy >/dev/null 2>&1; then
  python3 "$SCRAPE" | owa-piggy --save-config
else
  python3 "$SCRAPE" | python3 "$(dirname "$0")/../owa_piggy.py" --save-config
fi
