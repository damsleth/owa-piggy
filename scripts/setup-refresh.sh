#!/bin/bash
# Installs a single macOS LaunchAgent that keeps the *scheduled* owa-piggy
# profiles' refresh tokens alive. One plist for the whole tool, labelled
# `com.damsleth.owa-piggy.scheduled`, running `owa-piggy reseed --scheduled`.
#
# Which profiles actually get reseeded is the OWA_SCHEDULED set in
# ~/.config/owa-piggy/profiles.conf, read at run time - the plist itself is
# static and is only written once. This keeps a single row in macOS's Login
# Items & Extensions regardless of profile count, and means toggling a
# profile's schedule (owa-piggy schedule/unschedule, or the `profiles` TUI)
# is a pure config edit that never re-pokes launchd.
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
#
# Usage:
#   setup-refresh.sh --install-shared      write + load the shared agent
#   setup-refresh.sh --uninstall-shared    bootout + delete the shared agent

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$HOME/.config/owa-piggy"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_PREFIX="com.damsleth.owa-piggy"
SHARED_LABEL="$LABEL_PREFIX.scheduled"

# --- Arg parsing ---
MODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --install-shared)   MODE="install" ;;
    --uninstall-shared) MODE="uninstall" ;;
    -h|--help)
      sed -n '2,27p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown arg: $1" >&2
      exit 1
      ;;
  esac
  shift
done

if [ -z "$MODE" ]; then
  echo "ERROR: pass --install-shared or --uninstall-shared" >&2
  exit 1
fi

# --- Helpers ---

# Resolve how to invoke the tool. Prefer an installed `owa-piggy` on PATH;
# fall back to `python3 -m owa_piggy` with PYTHONPATH=<repo> so this script
# works in a clean checkout before add-to-path.sh or pipx install have run.
resolve_program_args() {
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
}

bootout_label() {
  local label="$1"
  local domain target
  domain="gui/$(id -u)"
  target="$domain/$label"
  if launchctl print "$target" >/dev/null 2>&1; then
    launchctl bootout "$target" 2>/dev/null || true
  fi
}

# Remove the suffix-less legacy plist from pre-profile installs, if any, and
# any pre-consolidation per-profile plists (com.damsleth.owa-piggy.<alias>),
# but never the shared agent itself.
uninstall_old_plists() {
  bootout_label "$LABEL_PREFIX"
  rm -f "$AGENTS_DIR/$LABEL_PREFIX.plist"
  if [ -d "$AGENTS_DIR" ]; then
    for plist in "$AGENTS_DIR/$LABEL_PREFIX".*.plist; do
      [ -e "$plist" ] || continue
      case "$plist" in
        "$AGENTS_DIR/$SHARED_LABEL.plist") continue ;;
      esac
      local base label
      base="$(basename "$plist")"
      label="${base%.plist}"
      echo "Removing pre-consolidation agent: $label"
      bootout_label "$label"
      rm -f "$plist"
    done
  fi
}

uninstall_shared() {
  bootout_label "$SHARED_LABEL"
  if [ -f "$AGENTS_DIR/$SHARED_LABEL.plist" ]; then
    rm -f "$AGENTS_DIR/$SHARED_LABEL.plist"
    echo "Removed $AGENTS_DIR/$SHARED_LABEL.plist"
  fi
}

install_shared() {
  local log="$CONFIG_DIR/refresh.log"
  local plist="$AGENTS_DIR/$SHARED_LABEL.plist"

  mkdir -p "$CONFIG_DIR" "$AGENTS_DIR"

  local domain target
  domain="gui/$(id -u)"
  target="$domain/$SHARED_LABEL"

  # Bootout any previously installed shared agent before rewriting the plist.
  bootout_label "$SHARED_LABEL"

  # The shared agent runs `owa-piggy reseed --scheduled`, which reads the
  # OWA_SCHEDULED set from profiles.conf at run time. No per-profile args,
  # no per-profile CDP_PORT (the scrape backend derives its port from the
  # alias itself; capture mode picks a free port).
  local full_args=("${PROGRAM_ARGS[@]}" "reseed" "--scheduled")

  # Emit one <string> per ProgramArguments element.
  local program_args_xml="" arg escaped
  for arg in "${full_args[@]}"; do
    # Escape all five XML-predefined entities. `&` must go first so we do
    # not double-escape the ampersands introduced by the other replacements.
    escaped="${arg//&/&amp;}"
    escaped="${escaped//</&lt;}"
    escaped="${escaped//>/&gt;}"
    escaped="${escaped//\"/&quot;}"
    escaped="${escaped//\'/&apos;}"
    program_args_xml+="    <string>$escaped</string>
"
  done

  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$SHARED_LABEL</string>
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
  <string>$log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF

  chmod 0644 "$plist"
  launchctl bootstrap "$domain" "$plist"
  # Kickstart once so the first rotation happens immediately without waiting for
  # RunAtLoad semantics (which differ subtly between bootstrap and load).
  launchctl kickstart "$target" >/dev/null 2>&1 || true

  echo "Shared LaunchAgent installed: $SHARED_LABEL"
  echo "  plist:    $plist"
  echo "  command:  ${full_args[*]}"
  echo "  schedule: hourly (top of each hour, catches up on wake)"
  echo "  profiles: OWA_SCHEDULED in $CONFIG_DIR/profiles.conf"
  echo "  logs:     $log"
  echo "  verify:   launchctl print $target"
  echo "  remove:   $0 --uninstall-shared"
  echo ""
}

# --- Dispatch ---

# Remove any legacy cron entry from the old setup script (applies to all modes).
if crontab -l 2>/dev/null | grep -q "owa-piggy"; then
  echo "Removing legacy cron entry..."
  crontab -l 2>/dev/null | grep -v "owa-piggy" | crontab -
fi

case "$MODE" in
  install)
    resolve_program_args
    uninstall_old_plists
    install_shared
    ;;
  uninstall)
    uninstall_shared
    ;;
esac
