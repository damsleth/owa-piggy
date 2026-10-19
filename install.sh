#!/usr/bin/env bash
# owa-piggy installer: curl -fsSL https://owa-piggy.dv.ms/install.sh | bash
#
# Installs owa-piggy and, if you want them, the owa-tools CLI suite
# (recommended) and owa-tui (optional) from PyPI, with uv (preferred) or
# pipx. Homebrew users: `brew install damsleth/tap/owa-tools` instead.
#
# Non-interactive: OWA_TOOLS=1|0 OWA_TUI=1|0 (defaults: tools yes, tui no).
# Prompts read /dev/tty, because under `curl | bash` stdin is this script.

set -euo pipefail

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }

# ask VAR "question" default(y|n): env VAR wins, then the terminal, then default.
ask() {
  local var=$1 q=$2 def=$3 ans
  if [ -n "${!var:-}" ]; then
    [ "${!var}" = 1 ]
    return
  fi
  if [ -r /dev/tty ] && { exec 3</dev/tty; } 2>/dev/null; then
    printf '%s [%s] ' "$q" "$([ "$def" = y ] && echo Y/n || echo y/N)" >&2
    read -r ans <&3 || ans=
    exec 3<&-
  fi
  ans=${ans:-$def}
  case $ans in [Yy]*) return 0 ;; *) return 1 ;; esac
}

main() {
  local backend
  if command -v uv >/dev/null 2>&1; then
    backend=uv
  elif command -v pipx >/dev/null 2>&1; then
    backend=pipx
  else
    say "Neither uv nor pipx found."
    ask OWA_GET_UV "Install uv (https://docs.astral.sh/uv) to manage the tools?" y \
      || die "install uv or pipx, then re-run"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv install failed"
    backend=uv
  fi

  local pkgs=()
  if ask OWA_TOOLS "Install owa-tools (owa-cal, owa-mail, owa-teams, ...; recommended, several agent skills need it)?" y; then
    pkgs+=(owa-tools)
  fi
  if ask OWA_TUI "Install owa-tui (terminal UI; nice to have)?" n; then
    pkgs+=(owa-tui)
  fi
  # owa-tools depends on owa-piggy, but each tool only exposes its own
  # commands, so owa-piggy gets an install of its own either way.
  pkgs=(owa-piggy "${pkgs[@]}")

  local p
  for p in "${pkgs[@]}"; do
    say "==> $backend: installing $p"
    if [ "$backend" = uv ]; then
      uv tool install --upgrade "$p"
    else
      pipx install --force "$p"
    fi
  done

  # The questions above were owa-piggy's first-run offer; don't repeat it.
  mkdir -p "$HOME/.config/owa-piggy" && touch "$HOME/.config/owa-piggy/.companions-offered"

  say "==> adding the tool bin dir to PATH"
  if [ "$backend" = uv ]; then uv tool update-shell || true; else pipx ensurepath || true; fi

  say ""
  say "Done: ${pkgs[*]}. Open a new shell, then sign in:"
  say "  owa-piggy setup --profile work --email you@yourcompany.com"
}

main "$@"
