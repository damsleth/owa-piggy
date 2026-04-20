#!/bin/bash
# Installs a macOS LaunchAgent that keeps the owa-piggy refresh token alive.
#
# Why launchd instead of cron:
#   cron on macOS does not fire missed jobs when the Mac was asleep. If the
#   laptop stays closed across the 24h SPA refresh-token window, the token
#   dies and a fresh browser-side token is required. launchd, by contrast,
#   runs a StartCalendarInterval job on wake if the scheduled time passed
#   while the machine was asleep, so the rotation survives overnight sleep.
#
# Schedule: top of every hour. The plist sets RunAtLoad=true so the first
# rotation happens immediately after install and is also attempted on every
# boot/login.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$HOME/.config/owa-piggy"
CONFIG="$CONFIG_DIR/config"
LOG="$CONFIG_DIR/refresh.log"
LABEL="com.damsleth.owa-piggy"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"

# Resolve how to invoke the tool. Prefer an installed `owa-piggy` on PATH;
# fall back to `python3 -m owa_piggy` with PYTHONPATH=<repo> so this script
# works in a clean checkout before add-to-path.sh or pipx install have run.
# (The package used to be a single owa_piggy.py at the repo root, hence the
#  legacy fallback - that file no longer exists.)
PROGRAM_ARGS=()
if owa_piggy_bin="$(command -v owa-piggy 2>/dev/null)"; then
  PROGRAM_ARGS=("$owa_piggy_bin")
elif [ -d "$REPO_DIR/owa_piggy" ] && [ -f "$REPO_DIR/owa_piggy/__main__.py" ]; then
  if ! python3_bin="$(command -v python3 2>/dev/null)"; then
    echo "ERROR: python3 not found on PATH; cannot run owa_piggy from $REPO_DIR"
    exit 1
  fi
  # `python3 -m owa_piggy` requires PYTHONPATH to include the repo dir so the
  # package resolves. launchd's environment is minimal, so set it explicitly
  # via env. One array element each so ProgramArguments XML stays correct.
  PROGRAM_ARGS=(
    "/usr/bin/env"
    "PYTHONPATH=$REPO_DIR"
    "$python3_bin"
    "-m"
    "owa_piggy"
  )
else
  echo "ERROR: neither owa-piggy on PATH nor $REPO_DIR/owa_piggy package found."
  exit 1
fi

# launchd runs agents without the user's shell profile, so OWA_REFRESH_TOKEN
# and OWA_TENANT_ID exported there are invisible. The config file is always
# readable by the agent. Block install if credentials only exist as env vars.
config_has_token=false
config_has_tenant=false
if [ -f "$CONFIG" ]; then
  grep -q '^OWA_REFRESH_TOKEN=' "$CONFIG" && config_has_token=true
  grep -q '^OWA_TENANT_ID=' "$CONFIG" && config_has_tenant=true
fi

if ! $config_has_token || ! $config_has_tenant; then
  missing=()
  $config_has_token || missing+=("OWA_REFRESH_TOKEN")
  $config_has_tenant || missing+=("OWA_TENANT_ID")

  env_only=true
  for key in "${missing[@]}"; do
    [ -z "${!key}" ] && env_only=false
  done

  if $env_only; then
    echo "WARNING: ${missing[*]} found only in environment, not in $CONFIG"
    echo "  launchd agents run without your shell profile, so those values"
    echo "  won't be available. Persist them now with:  owa-piggy --save-config"
    echo "  Then re-run this script."
    exit 1
  else
    echo "ERROR: credentials not configured. Run: owa-piggy --save-config"
    exit 1
  fi
fi

mkdir -p "$CONFIG_DIR" "$AGENTS_DIR"

# Remove any legacy cron entry from the old setup script
if crontab -l 2>/dev/null | grep -q "owa-piggy"; then
  echo "Removing legacy cron entry..."
  crontab -l 2>/dev/null | grep -v "owa-piggy" | crontab -
fi

# Modern launchctl uses domain-targeted bootstrap/bootout instead of load/unload.
# The GUI domain for the current user is gui/<uid>; the target for a specific
# agent is gui/<uid>/<label>.
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"

# Bootout any previously installed agent before rewriting the plist. If the
# agent is not currently bootstrapped, bootout exits non-zero - swallow it.
if launchctl print "$TARGET" >/dev/null 2>&1; then
  launchctl bootout "$TARGET" 2>/dev/null || true
fi

# Emit one <string> per ProgramArguments element (handles python3 + script path)
program_args_xml=""
for arg in "${PROGRAM_ARGS[@]}"; do
  # Escape XML-special chars in case a path contains them
  escaped="${arg//&/&amp;}"
  escaped="${escaped//</&lt;}"
  escaped="${escaped//>/&gt;}"
  program_args_xml+="    <string>$escaped</string>
"
done

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
${program_args_xml}  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/dev/null</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF

chmod 0644 "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
# Kickstart once so the first rotation happens immediately without waiting for
# RunAtLoad semantics (which differ subtly between bootstrap and load).
launchctl kickstart "$TARGET" >/dev/null 2>&1 || true

echo "LaunchAgent installed: $LABEL"
echo "  plist:    $PLIST"
echo "  command:  ${PROGRAM_ARGS[*]}"
echo "  schedule: hourly (top of each hour, catches up on wake)"
echo "  logs:     $LOG"
echo ""
echo "To verify:  launchctl print $TARGET"
echo "To remove:  launchctl bootout $TARGET && rm $PLIST"
