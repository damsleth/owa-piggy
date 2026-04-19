#!/bin/bash
# reseed-from-edge.sh - acquire a fresh FOCI refresh token by driving Edge
# against the owa-piggy sidecar profile, then hand the values to
# `owa-piggy --save-config`.
#
# Prerequisite (one time):
#   mkdir -p "$HOME/.config/owa-piggy/edge-profile"
#   /Applications/Microsoft\ Edge.app/Contents/MacOS/Microsoft\ Edge \
#     --user-data-dir="$HOME/.config/owa-piggy/edge-profile" \
#     https://outlook.cloud.microsoft
#   # log in, close Edge.
#
# Thereafter, this script does the rest. Edge flashes visible for a couple
# of seconds by default because --headless=new refuses localStorage on the
# OWA origin (MS broker-SSO will not complete without the full profile
# runtime, so localStorage returns SecurityError). Opt into headless with
# OWA_RESEED_HEADLESS=1 if you have verified it works for your tenant.
#
# Flow:
#   1. Attempt 1: launch Edge offscreen, scrape MSAL localStorage.
#   2. If the scraper exits 2 (REAUTH) the sidecar session cookies have
#      expired - kill the offscreen Edge, relaunch visibly onscreen so the
#      user can sign in, then scrape again.
#   3. Pipe the successful scrape output into `owa-piggy --save-config`.

set -e

EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROFILE_DIR="$HOME/.config/owa-piggy/edge-profile"
PORT="${CDP_PORT:-9222}"
SCRAPE="$(cd "$(dirname "$0")" && pwd)/scrape_edge.py"
HEADLESS="${OWA_RESEED_HEADLESS:-0}"
URL="https://outlook.cloud.microsoft"

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

EDGE_PID=""

cleanup_edge() {
  [ -z "$EDGE_PID" ] && return 0
  if kill -0 "$EDGE_PID" 2>/dev/null; then
    kill "$EDGE_PID" 2>/dev/null || true
    sleep 0.5
    kill -9 "$EDGE_PID" 2>/dev/null || true
  fi
  EDGE_PID=""
}

trap cleanup_edge EXIT INT TERM

# Launch Edge with the given mode. Modes:
#   offscreen - normal runtime but window parked at -32000,-32000 (default)
#   visible   - normal window onscreen (for interactive sign-in)
#   headless  - --headless=new (opt-in via OWA_RESEED_HEADLESS=1)
launch_edge() {
  local mode="$1"
  local -a args=(
    --disable-gpu
    --no-first-run
    --no-default-browser-check
    --remote-debugging-port="$PORT"
    --user-data-dir="$PROFILE_DIR"
  )
  case "$mode" in
    headless)
      args=(--headless=new --window-position=-32000,-32000 --window-size=1,1 "${args[@]}")
      ;;
    offscreen)
      args+=(--window-position=-32000,-32000 --window-size=1,1)
      ;;
    visible)
      args+=(--window-position=100,100 --window-size=900,700)
      ;;
  esac
  args+=("$URL")
  "$EDGE" "${args[@]}" >/dev/null 2>&1 &
  EDGE_PID=$!
}

# Attempt 1: offscreen (or headless if opted in).
if [ "$HEADLESS" = "1" ]; then
  launch_edge headless
else
  launch_edge offscreen
fi

scrape_output=""
scrape_status=0
scrape_output=$(python3 "$SCRAPE") || scrape_status=$?
cleanup_edge

# Exit 2 = interactive sign-in required. Relaunch visibly, wait for the user,
# then scrape again.
if [ "$scrape_status" -eq 2 ]; then
  echo "" >&2
  echo ">> Sidecar profile session has expired." >&2
  echo ">> Opening Edge so you can sign in to Outlook." >&2
  echo ">> Once the inbox has loaded, close Edge (or press Enter here) to continue." >&2
  echo "" >&2

  launch_edge visible
  # Wait for either Edge to exit on its own or the user to press Enter.
  # `wait -n $EDGE_PID` would block on Edge; running `read` in parallel lets
  # whichever happens first win. Background a watcher that sends SIGUSR1 to
  # ourselves when Edge exits, and just read in the foreground.
  (
    wait "$EDGE_PID" 2>/dev/null
    kill -USR1 $$ 2>/dev/null || true
  ) &
  watcher_pid=$!
  trap 'true' USR1  # wake up the read; do nothing else
  read -r _ || true
  trap cleanup_edge EXIT INT TERM
  kill "$watcher_pid" 2>/dev/null || true
  cleanup_edge

  # Attempt 2: offscreen again now that cookies should be fresh.
  launch_edge offscreen
  scrape_status=0
  scrape_output=$(python3 "$SCRAPE") || scrape_status=$?
  cleanup_edge
fi

if [ "$scrape_status" -ne 0 ] || [ -z "$scrape_output" ]; then
  echo "ERROR: scrape failed (exit $scrape_status); no token to save." >&2
  exit 1
fi

# Feed the scraped KEY=value lines into --save-config. Using a here-string
# instead of a pipe so the token doesn't transit a new subshell's stderr.
if command -v owa-piggy >/dev/null 2>&1; then
  owa-piggy --save-config <<<"$scrape_output"
else
  python3 "$(dirname "$0")/../owa_piggy.py" --save-config <<<"$scrape_output"
fi
