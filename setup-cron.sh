#!/bin/bash
# Sets up an hourly cron job to keep the owa-piggy refresh token alive.
#
# The refresh token has a 24h sliding window and rotates on every use.
# Running hourly keeps it well within that window and ensures a fresh
# access token is always available even if individual runs fail.

TOOL="$(which owa-piggy 2>/dev/null || echo "$(cd "$(dirname "$0")" && pwd)/owa-piggy")"
LOG="$HOME/.config/owa-piggy/cron.log"
CRON_JOB="0 * * * * $TOOL > /dev/null 2>> $LOG"

if ! command -v "$TOOL" &>/dev/null && [ ! -x "$TOOL" ]; then
  echo "ERROR: owa-piggy not found. Run ./add-to-path.sh first."
  exit 1
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
