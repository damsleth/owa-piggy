#!/bin/bash
# reseed-from-edge.sh - acquire a fresh FOCI refresh token by driving Edge
# against the owa-piggy sidecar profile, then hand the values to
# `owa-piggy setup`.
#
# Invoked via `owa-piggy reseed [--profile <alias>]`, which sets:
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
# Thereafter, `owa-piggy reseed --profile $alias` drives the rest.
# Defaults to --headless=new so scheduled reseeds don't flash Edge
# onscreen. Tested working against multiple tenants (first-party SPA
# auth, not brokered SSO). If a tenant ever needs the full profile
# runtime for MSAL to initialize, set OWA_RESEED_HEADLESS=0 to fall
# back to an offscreen-but-non-headless window.
#
# Flow:
#   1. Attempt 1: launch Edge (headless or offscreen), scrape MSAL
#      localStorage.
#   2. If the scraper exits 2 (REAUTH) the sidecar session cookies have
#      expired - kill Edge, relaunch visibly onscreen so the user can sign
#      in, then scrape again.
#   3. Pipe the successful scrape output into
#      `owa-piggy setup --profile <alias>`.
#   4. Verify the scraped refresh token by probing AAD via
#      `owa-piggy status --profile <alias>`. This closes a real hole: the
#      scraper's staleness check is an ID-token-iat heuristic that can let
#      through an RT past the 24h SPA hard-cap (AADSTS700084) when MSAL
#      silently refreshed only the ID token. On verify failure, fall back
#      to visible sign-in once more and re-scrape. This is the core reseed
#      loop - if it does not produce a working token, the tool is broken.

set -e

PROFILE_ALIAS="${OWA_PIGGY_PROFILE:-default}"
DEFAULT_DIR="$HOME/.config/owa-piggy/profiles/$PROFILE_ALIAS/edge-profile"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROFILE_DIR="${OWA_PIGGY_EDGE_PROFILE_DIR:-$DEFAULT_DIR}"
PORT="${CDP_PORT:-9222}"
SCRAPE="$(cd "$(dirname "$0")" && pwd)/scrape_edge.py"
HEADLESS="${OWA_RESEED_HEADLESS:-1}"
URL="https://outlook.cloud.microsoft"

log() { echo "[$PROFILE_ALIAS] $*" >&2; }

if [ ! -x "$EDGE" ]; then
  log "ERROR: Edge not found at $EDGE"
  exit 1
fi
# Each profile gets its own Edge userdata dir. Auto-create it so a fresh
# profile can reseed without a separate manual bootstrap step - the
# first run will still land in the visible-signin branch (scrape exits 2
# because there are no session cookies), after which subsequent reseeds
# are headless. `owa-piggy setup` also does this eagerly.
if [ ! -d "$PROFILE_DIR" ]; then
  log "creating sidecar profile at $PROFILE_DIR"
  mkdir -p "$PROFILE_DIR"
  chmod 700 "$PROFILE_DIR" 2>/dev/null || true
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
#   headless  - --headless=new (default; no window, no dock icon)
#   offscreen - normal runtime but window parked at -32000,-32000
#               (fallback via OWA_RESEED_HEADLESS=0 if a tenant ever
#               refuses localStorage under headless)
#   visible   - normal window onscreen (for interactive sign-in)
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

# Preferred attempt mode: headless by default, offscreen if the user has
# forced non-headless via OWA_RESEED_HEADLESS=0.
ATTEMPT_MODE="headless"
[ "$HEADLESS" = "0" ] && ATTEMPT_MODE="offscreen"

# Repo-checkout fallback path for owa-piggy (setup + status). When the
# package is not installed on PATH we invoke `python3 -m owa_piggy` with
# PYTHONPATH pointing at the repo so the scripts keep working in dev.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

scrape_output=""
scrape_status=0

# Run nested owa-piggy commands against the just-saved profile config, not
# any credential overrides inherited from the parent shell. OWA_PROFILE and
# OWA_PIGGY_PROFILE stay intact; --profile remains explicit below.
owa_piggy_clean_env() {
  (
    unset OWA_REFRESH_TOKEN OWA_TENANT_ID OWA_CLIENT_ID
    if command -v owa-piggy >/dev/null 2>&1; then
      owa-piggy "$@"
    else
      PYTHONPATH="$REPO_ROOT" python3 -m owa_piggy "$@"
    fi
  )
}

# Run a single scrape against a freshly launched Edge. Writes to the
# globals scrape_output / scrape_status so the main flow can branch on
# them without capturing through another subshell.
scrape_attempt() {
  scrape_output=""
  scrape_status=0
  launch_edge "$ATTEMPT_MODE"
  scrape_output=$(python3 "$SCRAPE") || scrape_status=$?
  cleanup_edge
}

# Block until the visible-signin Edge closes or the user hits Enter.
# bash's `wait` only sees direct children; we ran Edge in the background
# of this shell so kill-check polling is the portable option. On launchd
# (no tty) `read -t` returns immediately on EOF, so we sleep instead.
wait_for_signin() {
  while kill -0 "$EDGE_PID" 2>/dev/null; do
    if [ -t 0 ]; then
      if read -r -t 1 _; then
        break
      fi
    else
      sleep 1
    fi
  done
  cleanup_edge
}

# Bail out cleanly when interactive sign-in is needed but we have no tty
# (launchd, cron, CI). Without this guard the hourly refresh agent would
# pop a visible Edge window on the user's screen with nobody at the
# keyboard - a visible regression from the pre-v0.5.2 "silent failure".
# The user can re-run `owa-piggy reseed` interactively to recover; the
# error reason lands in the per-profile refresh.log.
require_tty_or_exit() {
  if [ ! -t 0 ]; then
    log "ERROR: $1"
    log "       Re-run interactively: owa-piggy reseed --profile $PROFILE_ALIAS"
    exit 1
  fi
}

# Open Edge visibly so the user can sign in, then wait for them to finish.
# $1 is the headline reason shown to the user so the two callsites
# (cookies-gone vs AAD-rejected-scraped-token) give different context.
# Callers must gate this with `require_tty_or_exit` - we do not redundantly
# check here so the error message at the callsite can describe which
# failure mode triggered the need for sign-in.
visible_signin() {
  log ""
  log ">> $1"
  log ">> Opening Edge so you can sign in to Outlook."
  log ">> Once the inbox has loaded, close Edge (or press Enter here) to continue."
  log ""
  launch_edge visible
  wait_for_signin
}

# Pipe the last scrape into `owa-piggy setup --profile <alias>`. Here-string
# (not a pipe) so the token doesn't transit a new subshell's stderr.
save_token() {
  owa_piggy_clean_env setup --profile "$PROFILE_ALIAS" <<<"$scrape_output"
}

# Probe the saved RT against AAD. Exit 0 = AAD accepted it, non-zero =
# rejected (stale past 24h SPA cap, tenant mismatch, etc.). Output is
# discarded - we use the exit code and own the messaging here.
verify_token() {
  owa_piggy_clean_env status --profile "$PROFILE_ALIAS" >/dev/null 2>&1
}

# --- Attempt 1: scrape from the current sidecar session. ----------------
scrape_attempt

# Exit 2 = scraper detected a login-host redirect: sidecar cookies gone.
# Fall straight through to visible signin before ever touching setup.
if [ "$scrape_status" -eq 2 ]; then
  require_tty_or_exit "Sidecar profile session has expired; interactive sign-in needed."
  visible_signin "Sidecar profile session has expired."
  scrape_attempt
fi

if [ "$scrape_status" -ne 0 ] || [ -z "$scrape_output" ]; then
  log "ERROR: scrape failed (exit $scrape_status); no token to save."
  exit 1
fi

save_token

if verify_token; then
  exit 0
fi

# --- Attempt 2: AAD rejected the scraped RT despite a successful scrape.
# Most likely cause: MSAL silent-refreshed the ID token via iframe
# auth-code against the SSO cookie, updating iat but leaving the RT past
# its 24h SPA hard-cap (AADSTS700084). Visible signin forces MSAL to
# mint a truly fresh RT - but only when a human is around to sign in.
# Under launchd we exit 1 and let the error land in refresh.log.
require_tty_or_exit "Scraped refresh token rejected by AAD (likely AADSTS700084, past the 24h SPA hard-cap); interactive sign-in needed."
log ""
log ">> Scraped refresh token was rejected by AAD (likely past the 24h SPA"
log ">> hard-cap). Falling back to visible sign-in to refresh the session."
visible_signin "Refreshing the sidecar session."
scrape_attempt

if [ "$scrape_status" -ne 0 ] || [ -z "$scrape_output" ]; then
  log "ERROR: scrape after sign-in failed (exit $scrape_status)."
  exit 1
fi

save_token

if verify_token; then
  exit 0
fi

log "ERROR: refresh token still rejected by AAD after visible sign-in."
log "       Run \`owa-piggy debug --profile $PROFILE_ALIAS\` for details."
exit 1
