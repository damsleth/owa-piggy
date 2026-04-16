#!/bin/bash
# Sets up an hourly cron job to keep the owa-piggy refresh token alive.
#
# The refresh token has a 24h sliding window and rotates on every use.
# Running hourly keeps it well within that window and ensures a fresh
# access token is always available even if individual runs fail.

TOOL="$(which owa-piggy 2>/dev/null || echo "$(cd "$(dirname "$0")" && pwd)/owa-piggy")"
LOG="$HOME/.config/owa-piggy/cron.log"
CONFIG="$HOME/.config/owa-piggy/config"
CRON_JOB="0 * * * * $TOOL > /dev/null 2>> $LOG"

if ! command -v "$TOOL" &>/dev/null && [ ! -x "$TOOL" ]; then
  echo "ERROR: owa-piggy not found. Run ./add-to-path.sh first."
  exit 1
fi

# Cron does not source the user's shell profile, so OWA_REFRESH_TOKEN and
# OWA_TENANT_ID exported there are invisible to the cron job. The config file
# at $CONFIG is always readable by cron. Detect an env-var-only setup and
# block before installing a cron job that will silently fail every hour.
config_has_token=false
config_has_tenant=false
if [ -f "$CONFIG" ]; then
  grep -q '^OWA_REFRESH_TOKEN=' "$CONFIG" && config_has_token=true
  grep -q '^OWA_TENANT_ID=' "$CONFIG" && config_has_tenant=true
fi

if ! $config_has_token || ! $config_has_tenant; then
  # Check whether the missing values exist only as env vars
  missing=()
  $config_has_token || missing+=("OWA_REFRESH_TOKEN")
  $config_has_tenant || missing+=("OWA_TENANT_ID")

  env_only=true
  for key in "${missing[@]}"; do
    [ -z "${!key}" ] && env_only=false
  done

  if $env_only; then
    echo "WARNING: ${missing[*]} found only in environment, not in $CONFIG"
    echo "  Cron runs without your shell profile, so those values won't be available."
    echo "  Persist them now by running:  owa-piggy --save-config"
    echo "  Then re-run this script."
    exit 1
  else
    echo "ERROR: credentials not configured. Run: owa-piggy --save-config"
    exit 1
  fi
fi

existing=$(crontab -l 2>/dev/null | grep "owa-piggy")
if [ -n "$existing" ]; then
  echo "Cron job already exists:"
  echo "  $existing"
  read -p "Replace? (y/N): " answer
  [[ "$answer" != "y" && "$answer" != "Y" ]] && exit 0
  crontab -l 2>/dev/null | grep -v "owa-piggy" | crontab -
fi

(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
echo "Cron job added (hourly):"
echo "  $CRON_JOB"
echo ""
echo "Logs: $LOG"
echo ""
echo "To remove:  crontab -e  (delete the owa-piggy line)"
echo "To verify:  crontab -l | grep owa-piggy"
