#!/bin/bash
# reseed-from-edge.sh - acquire a fresh FOCI refresh token by driving Edge
# against the owa-piggy sidecar profile, then hand the values to
# `owa-piggy --save-config`.
#
# Invoked via `owa-piggy --reseed [--profile <alias>]`, which sets:
#   OWA_PIGGY_PROFILE            the alias (default: "default")
#   OWA_PIGGY_EDGE_PROFILE_DIR   per-profile Edge userdata dir
# Both have sensible fallbacks so the script is still runnable by hand.
#
# Prerequisite (one time, per profile):
#   alias=default   # or work, personal, ...
#   dir="$HOME/.config/owa-piggy/profiles/$alias/edge-profile"
#   mkdir -p "$dir"
#   /Applications/Microsoft\ Edge.app/Contents/MacOS/Microsoft\ Edge \
#     --user-data-dir="$dir" https://outlook.cloud.microsoft
#   # log in, close Edge.
#
# Thereafter, `owa-piggy --reseed --profile $alias` drives the rest.
# Edge flashes visible for a couple of seconds by default because
# --headless=new refuses localStorage on the OWA origin (MS broker-SSO
# will not complete without the full profile runtime, so localStorage
# returns SecurityError). Opt into headless with OWA_RESEED_HEADLESS=1
# if you have verified it works for your tenant.
#
# Flow:
#   1. Attempt 1: launch Edge offscreen, scrape MSAL localStorage.
#   2. If the scraper exits 2 (REAUTH) the sidecar session cookies have
#      expired - kill the offscreen Edge, relaunch visibly onscreen so the
#      user can sign in, then scrape again.
#   3. Pipe the successful scrape output into
#      `owa-piggy --save-config --profile <alias>`.

set -e

PROFILE_ALIAS="${OWA_PIGGY_PROFILE:-default}"
DEFAULT_DIR="$HOME/.config/owa-piggy/profiles/$PROFILE_ALIAS/edge-profile"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROFILE_DIR="${OWA_PIGGY_EDGE_PROFILE_DIR:-$DEFAULT_DIR}"
PORT="${CDP_PORT:-9222}"
SCRAPE="$(cd "$(dirname "$0")" && pwd)/scrape_edge.py"
HEADLESS="${OWA_RESEED_HEADLESS:-0}"
URL="https://outlook.cloud.microsoft"

log() { echo "[$PROFILE_ALIAS] $*" >&2; }

if [ ! -x "$EDGE" ]; then
  log "ERROR: Edge not found at $EDGE"
  exit 1
fi
if [ ! -d "$PROFILE_DIR" ]; then
  log "ERROR: sidecar profile not found at $PROFILE_DIR"
  log "  Run the one-time setup from this script's header first."
  exit 1
fi
if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  log "ERROR: port $PORT is busy. Kill the occupant or set CDP_PORT."
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
  # shellcheck disable=SC2054  # commas inside Chrome flags are literal syntax, not array separators
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
  log ""
  log ">> Sidecar profile session has expired."
  log ">> Opening Edge so you can sign in to Outlook."
  log ">> Once the inbox has loaded, close Edge (or press Enter here) to continue."
  log ""

  launch_edge visible
  # Wait for either Edge to exit on its own or the user to press Enter.
  # The earlier version backgrounded `( wait "$EDGE_PID" ... ) &`, but bash
  # only lets `wait` block on children of the CURRENT shell - in that
  # subshell Edge is not a child, so `wait` returned immediately and the
  # reauth flow raced through before the user could sign in.
  #
  # Poll instead: read stdin with a 1-second timeout; each iteration also
  # checks whether Edge is still alive via `kill -0`. Either signal breaks
  # the loop.
  while kill -0 "$EDGE_PID" 2>/dev/null; do
    if read -r -t 1 _; then
      break
    fi
  done
  cleanup_edge

  # Attempt 2: offscreen again now that cookies should be fresh.
  launch_edge offscreen
  scrape_status=0
  scrape_output=$(python3 "$SCRAPE") || scrape_status=$?
  cleanup_edge
fi

if [ "$scrape_status" -ne 0 ] || [ -z "$scrape_output" ]; then
  log "ERROR: scrape failed (exit $scrape_status); no token to save."
  exit 1
fi

# Feed the scraped KEY=value lines into --save-config for this profile.
# Using a here-string instead of a pipe so the token doesn't transit a
# new subshell's stderr.
if command -v owa-piggy >/dev/null 2>&1; then
  owa-piggy --save-config --profile "$PROFILE_ALIAS" <<<"$scrape_output"
else
  # Repo-checkout fallback: run the package directly via `-m`. The flat
  # owa_piggy.py at the repo root no longer exists after the package split.
  repo_root="$(cd "$(dirname "$0")/.." && pwd)"
  PYTHONPATH="$repo_root" python3 -m owa_piggy --save-config --profile "$PROFILE_ALIAS" <<<"$scrape_output"
fi
