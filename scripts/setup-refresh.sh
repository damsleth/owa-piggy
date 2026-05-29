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
# Login Items branding: the launched program is a tiny launcher app bundle at
# ~/.config/owa-piggy/OwaPiggy.app, not the bare CLI, and the plist carries an
# AssociatedBundleIdentifiers key pointing at it. That makes macOS's Login
# Items & Extensions row show "owa-piggy refresh" + the pig icon instead of a
# generic placeholder. The bundle executable is a shell shim that execs the
# same `owa-piggy reseed --scheduled` command; it carries no Developer ID
# signature, so the row still reads "unidentified developer" - expected and
# harmless for an agent you installed yourself.
#
# Usage:
#   setup-refresh.sh --install-shared      write + load the shared agent
#   setup-refresh.sh --uninstall-shared    bootout + delete the shared agent

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$HOME/.config/owa-piggy"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_PREFIX="com.damsleth.owa-piggy"
SHARED_LABEL="$LABEL_PREFIX.scheduled"
# Launcher app bundle that brands the Login Items row. Its CFBundleIdentifier
# matches SHARED_LABEL so AssociatedBundleIdentifiers links plist -> bundle.
APP_BUNDLE="$CONFIG_DIR/OwaPiggy.app"
APP_EXECUTABLE="owa-piggy-reseed"
ICON_SRC="$SCRIPT_DIR/owa-piggy.png"
APP_DISPLAY_NAME="owa-piggy refresh"

# --- Arg parsing ---
MODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --install-shared)   MODE="install" ;;
    --uninstall-shared) MODE="uninstall" ;;
    -h|--help)
      sed -n '2,36p' "$0"
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
  if [ -d "$APP_BUNDLE" ]; then
    rm -rf "$APP_BUNDLE"
    echo "Removed $APP_BUNDLE"
  fi
}

# Build the .icns for the launcher bundle from ICON_SRC. Best-effort: returns
# non-zero (without failing the install) if the source PNG or the macOS image
# tools are missing, in which case the bundle just gets a generic icon.
make_icns() {
  local src="$1" out="$2"
  [ -f "$src" ] || return 1
  command -v sips >/dev/null 2>&1 || return 1
  command -v iconutil >/dev/null 2>&1 || return 1

  local workdir iconset size double
  workdir="$(mktemp -d)" || return 1
  iconset="$workdir/owa-piggy.iconset"
  mkdir -p "$iconset" || { rm -rf "$workdir"; return 1; }

  for size in 16 32 128 256 512; do
    double=$((size * 2))
    sips -z "$size" "$size" "$src" \
      --out "$iconset/icon_${size}x${size}.png" >/dev/null 2>&1 || true
    sips -z "$double" "$double" "$src" \
      --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
  done

  iconutil -c icns "$iconset" -o "$out" >/dev/null 2>&1
  local rc=$?
  rm -rf "$workdir"
  return $rc
}

# Write the launcher's C source (execs `full_args` via execv) to $1.
# A compiled Mach-O executable is what makes macOS's Login Items row show the
# bundle's CFBundleDisplayName + icon: a .app whose executable is a *shell
# script* is flagged `shell-script` by LaunchServices and labelled by its
# script filename instead. full_args[0] is always an absolute path (an
# absolute `owa-piggy`, or `/usr/bin/env`), so execv(args[0], ...) resolves.
write_launcher_c() {
  local out="$1"; shift
  local arg esc
  {
    printf '#include <unistd.h>\n'
    printf '/* Auto-generated by setup-refresh.sh. Execs the scheduled reseed. */\n'
    printf 'int main(void) {\n'
    printf '    char *args[] = {'
    for arg in "$@"; do
      esc="${arg//\\/\\\\}"   # backslash first
      esc="${esc//\"/\\\"}"   # then double-quote
      printf '"%s", ' "$esc"
    done
    printf '0};\n'
    printf '    execv(args[0], args);\n'
    printf '    return 127;\n'
    printf '}\n'
  } > "$out"
}

# Write a shell-shim fallback launcher to $1 (used only when no C compiler is
# available). Each arg is %q-quoted so paths with spaces survive the exec.
write_shell_shim() {
  local out="$1"; shift
  local arg
  {
    printf '#!/bin/bash\n'
    printf '# Auto-generated by setup-refresh.sh. Launches the scheduled reseed.\n'
    printf 'exec'
    for arg in "$@"; do
      printf ' %q' "$arg"
    done
    printf '\n'
  } > "$out"
}

# Register the bundle with LaunchServices so its CFBundleIdentifier resolves
# (it lives under ~/.config, which LS does not scan on its own) and BTM can
# attribute the Login Items row to it. Best-effort.
register_bundle() {
  local lsreg="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  [ -x "$lsreg" ] && "$lsreg" -f "$APP_BUNDLE" >/dev/null 2>&1 || true
}

# Build the launcher app bundle whose executable execs `full_args` (the fully
# resolved `... reseed --scheduled` command). Relies on `full_args` being set
# by the caller (install_shared); bash's dynamic scoping makes the caller's
# local array visible here.
build_app_bundle() {
  local contents="$APP_BUNDLE/Contents"
  local macos="$contents/MacOS"
  local resources="$contents/Resources"

  rm -rf "$APP_BUNDLE"
  mkdir -p "$macos" "$resources"

  # Prefer a compiled Mach-O launcher (proper app -> branded Login Items row);
  # fall back to a shell shim if there's no C compiler (still functional, and
  # still grouped via AssociatedBundleIdentifiers, just with a plainer label).
  local exe="$macos/$APP_EXECUTABLE" built=0
  if command -v cc >/dev/null 2>&1; then
    local csrc; csrc="$(mktemp)"
    write_launcher_c "$csrc" "${full_args[@]}"
    if cc -O2 -x c -o "$exe" "$csrc" >/dev/null 2>&1; then
      built=1
    fi
    rm -f "$csrc"
  fi
  if [ "$built" -ne 1 ]; then
    write_shell_shim "$exe" "${full_args[@]}"
    echo "  note: no C compiler found; using a shell-shim launcher. The Login"
    echo "        Items row may show the script name rather than \"$APP_DISPLAY_NAME\"."
  fi
  chmod 0755 "$exe"

  local icon_key=""
  if make_icns "$ICON_SRC" "$resources/owa-piggy.icns"; then
    icon_key="  <key>CFBundleIconFile</key>
  <string>owa-piggy</string>
"
  fi

  cat > "$contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>$SHARED_LABEL</string>
  <key>CFBundleName</key>
  <string>$APP_DISPLAY_NAME</string>
  <key>CFBundleDisplayName</key>
  <string>$APP_DISPLAY_NAME</string>
  <key>CFBundleExecutable</key>
  <string>$APP_EXECUTABLE</string>
${icon_key}  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>LSBackgroundOnly</key>
  <true/>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
EOF

  # Bump the bundle mtime so LaunchServices re-reads the (possibly changed)
  # Info.plist instead of serving a cached attribution, then (re)register it.
  touch "$APP_BUNDLE"
  register_bundle
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

  # Wrap that command in the launcher app bundle so the Login Items row is
  # branded. launchd runs the bundle's shim, which execs `full_args`.
  build_app_bundle
  local shim_path="$APP_BUNDLE/Contents/MacOS/$APP_EXECUTABLE"

  # launchd runs the single bundle executable; the shim carries the real args.
  local escaped="${shim_path//&/&amp;}"
  escaped="${escaped//</&lt;}"
  escaped="${escaped//>/&gt;}"
  escaped="${escaped//\"/&quot;}"
  escaped="${escaped//\'/&apos;}"
  local program_args_xml="    <string>$escaped</string>
"

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
  <key>AssociatedBundleIdentifiers</key>
  <array>
    <string>$SHARED_LABEL</string>
  </array>
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
  echo "  bundle:   $APP_BUNDLE ($APP_DISPLAY_NAME)"
  echo "  command:  ${full_args[*]}"
  echo "  schedule: hourly (top of each hour, catches up on wake)"
  echo "  profiles: OWA_SCHEDULED in $CONFIG_DIR/profiles.conf"
  echo "  logs:     $log"
  echo "  verify:   launchctl print $target"
  echo "  remove:   $0 --uninstall-shared"
  echo ""
  echo "  Login Items row shows \"$APP_DISPLAY_NAME\" + the pig icon. macOS"
  echo "  caches background-item attribution, so an old row may linger until"
  echo "  you toggle the item off/on in System Settings > General > Login"
  echo "  Items, or until next logout/login. It will still say \"unidentified"
  echo "  developer\" - expected for a self-installed, unsigned agent."
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
