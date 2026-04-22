#!/bin/bash
# Installs a macOS LaunchAgent that keeps an owa-piggy profile's refresh
# token alive. One plist per profile, labelled
# `com.damsleth.owa-piggy.<alias>`.
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
#   setup-refresh.sh --profile <alias>            install/update one profile
#   setup-refresh.sh --all                        install/update every profile
#                                                 listed in profiles.conf
#   setup-refresh.sh --uninstall --profile <a>    bootout + delete one plist
#   setup-refresh.sh --uninstall --all            uninstall every profile's
#                                                 plist

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$HOME/.config/owa-piggy"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_PREFIX="com.damsleth.owa-piggy"

# --- Arg parsing ---
MODE="install"
TARGET_PROFILE=""
ALL_MODE=false
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)
      shift
      TARGET_PROFILE="${1:-}"
      ;;
    --profile=*)
      TARGET_PROFILE="${1#--profile=}"
      ;;
    --all)
      ALL_MODE=true
      ;;
    --uninstall)
      MODE="uninstall"
      ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown arg: $1" >&2
      exit 1
      ;;
  esac
  shift
done

if [ "$ALL_MODE" = false ] && [ -z "$TARGET_PROFILE" ]; then
  echo "ERROR: pass --profile <alias> or --all" >&2
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

# List every profile registered in profiles.conf, falling back to disk
# directory listing if the registry isn't set up yet.
list_all_profiles() {
  local conf="$CONFIG_DIR/profiles.conf"
  if [ -f "$conf" ]; then
    awk -F'=' '
      /^OWA_PROFILES=/ {
        val = $2
        gsub(/^[[:space:]"\047]+|[[:space:]"\047]+$/, "", val)
        print val
      }
    ' "$conf"
  elif [ -d "$CONFIG_DIR/profiles" ]; then
    (cd "$CONFIG_DIR/profiles" && /bin/ls -1)
  fi
}

uninstall_plist() {
  local alias="$1"
  local label="$LABEL_PREFIX.$alias"
  local plist="$AGENTS_DIR/$label.plist"
  local domain target
  domain="gui/$(id -u)"
  target="$domain/$label"
  if launchctl print "$target" >/dev/null 2>&1; then
    launchctl bootout "$target" 2>/dev/null || true
  fi
  if [ -f "$plist" ]; then
    rm -f "$plist"
    echo "Removed $plist"
  fi
}

# Remove the suffix-less legacy plist from pre-profile installs, if any.
# One-shot cleanup so the old com.damsleth.owa-piggy agent doesn't keep
# running against a config path that no longer exists post-migration.
uninstall_legacy_plist() {
  local label="$LABEL_PREFIX"
  local plist="$AGENTS_DIR/$label.plist"
  local domain target
  domain="gui/$(id -u)"
  target="$domain/$label"
  if launchctl print "$target" >/dev/null 2>&1; then
    echo "Removing legacy single-profile plist..."
    launchctl bootout "$target" 2>/dev/null || true
  fi
  if [ -f "$plist" ]; then
    rm -f "$plist"
  fi
}

install_plist_for_profile() {
  local alias="$1"
  local config="$CONFIG_DIR/profiles/$alias/config"
  local log="$CONFIG_DIR/profiles/$alias/refresh.log"
  local label="$LABEL_PREFIX.$alias"
  local plist="$AGENTS_DIR/$label.plist"

  # launchd runs agents without the user's shell profile, so OWA_REFRESH_TOKEN
  # and OWA_TENANT_ID exported there are invisible. The profile's config
  # file is always readable by the agent. Block install if credentials
  # aren't on disk.
  if [ ! -f "$config" ]; then
    echo "ERROR: profile $alias has no config at $config"
    echo "  Run: owa-piggy setup --profile $alias"
    return 1
  fi
  local has_token=false has_tenant=false
  grep -q '^OWA_REFRESH_TOKEN=' "$config" && has_token=true
  grep -q '^OWA_TENANT_ID=' "$config" && has_tenant=true
  if ! $has_token || ! $has_tenant; then
    echo "ERROR: profile $alias is missing OWA_REFRESH_TOKEN and/or OWA_TENANT_ID in $config"
    echo "  Re-run: owa-piggy setup --profile $alias"
    return 1
  fi

  mkdir -p "$(dirname "$log")" "$AGENTS_DIR"

  local domain target
  domain="gui/$(id -u)"
  target="$domain/$label"

  # Bootout any previously installed agent before rewriting the plist. If the
  # agent is not currently bootstrapped, bootout exits non-zero - swallow it.
  if launchctl print "$target" >/dev/null 2>&1; then
    launchctl bootout "$target" 2>/dev/null || true
  fi

  # Build the ProgramArguments list, appending the `reseed` subcommand
  # + `--profile <alias>` so the agent runs against this profile
  # specifically.
  local full_args=("${PROGRAM_ARGS[@]}" "reseed" "--profile" "$alias")

  # Emit one <string> per ProgramArguments element (handles python3 + script path)
  local program_args_xml="" arg escaped
  for arg in "${full_args[@]}"; do
    # Escape XML-special chars in case a path contains them
    escaped="${arg//&/&amp;}"
    escaped="${escaped//</&lt;}"
    escaped="${escaped//>/&gt;}"
    program_args_xml+="    <string>$escaped</string>
"
  done

  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
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

  echo "LaunchAgent installed: $label"
  echo "  plist:    $plist"
  echo "  command:  ${full_args[*]}"
  echo "  schedule: hourly (top of each hour, catches up on wake)"
  echo "  logs:     $log"
  echo "  verify:   launchctl print $target"
  echo "  remove:   $0 --uninstall --profile $alias"
  echo ""
}

# --- Dispatch ---

# Remove any legacy cron entry from the old setup script (applies to both
# install and uninstall modes).
if crontab -l 2>/dev/null | grep -q "owa-piggy"; then
  echo "Removing legacy cron entry..."
  crontab -l 2>/dev/null | grep -v "owa-piggy" | crontab -
fi

# Always clean up the pre-profile single-label plist on install runs so
# we never leave a zombie agent pointed at the old layout.
if [ "$MODE" = "install" ]; then
  uninstall_legacy_plist
  resolve_program_args
fi

if [ "$MODE" = "uninstall" ]; then
  if [ "$ALL_MODE" = true ]; then
    profiles="$(list_all_profiles)"
    if [ -z "$profiles" ]; then
      echo "No profiles registered; nothing to uninstall."
      exit 0
    fi
    for alias in $profiles; do
      uninstall_plist "$alias"
    done
  else
    uninstall_plist "$TARGET_PROFILE"
  fi
  exit 0
fi

# Install path.
if [ "$ALL_MODE" = true ]; then
  profiles="$(list_all_profiles)"
  if [ -z "$profiles" ]; then
    echo "ERROR: no profiles registered. Run: owa-piggy setup --profile <alias>"
    exit 1
  fi
  rc=0
  for alias in $profiles; do
    install_plist_for_profile "$alias" || rc=1
  done
  exit "$rc"
else
  install_plist_for_profile "$TARGET_PROFILE"
fi
