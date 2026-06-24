#!/usr/bin/env python3
"""Generate shell completions for owa-piggy from the live argparse parser.

The point of generating (rather than hand-writing) is that the completions
cannot drift from the CLI: every subcommand, every flag, and the `--audience`
value list are read straight out of `owa_piggy.cli._build_parser()`. Re-run
this after changing the parser and commit the result; tests/test_completions.py
fails if the committed files are stale.

Usage:
    python3 scripts/gen-completions.py            # write scripts/completions/*
    python3 scripts/gen-completions.py --stdout zsh|bash|fish   # print one

The three install lines are documented in scripts/completions/README.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owa_piggy.cli import _build_parser  # noqa: E402

PROG = "owa-piggy"


def _subparsers_action(parser):
    """Return the _SubParsersAction of a parser, or None."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _option_strings(parser):
    """All option flags (e.g. --json, -v) declared directly on `parser`."""
    opts = []
    for action in parser._actions:
        opts.extend(action.option_strings)
    return opts


def _first_line(text):
    return (text or "").strip().splitlines()[0] if text else ""


def _audience_choices(parser):
    """The sorted `--audience` choices, read off whichever subparser declares
    them (they are identical across commands)."""
    sub = _subparsers_action(parser)
    if not sub:
        return []
    for cmd_parser in sub.choices.values():
        for action in cmd_parser._actions:
            if "--audience" in action.option_strings and action.choices:
                return sorted(action.choices)
    return []


def collect(parser):
    """Pull a flat, ordered model of the CLI out of the parser."""
    sub = _subparsers_action(parser)
    commands = []
    if sub:
        # sub.choices preserves insertion (definition) order on py3.7+.
        for name, cmd_parser in sub.choices.items():
            help_text = ""
            for choice_action in sub._choices_actions:
                if choice_action.dest == name:
                    help_text = _first_line(choice_action.help)
            nested = _subparsers_action(cmd_parser)
            commands.append(
                {
                    "name": name,
                    "help": help_text,
                    "options": _option_strings(cmd_parser),
                    "subcommands": (
                        [
                            {"name": n, "help": _first_line(_sub_help(nested, n))}
                            for n in nested.choices
                        ]
                        if nested
                        else []
                    ),
                }
            )
    return {
        "global_options": _option_strings(parser),
        "commands": commands,
        "audiences": _audience_choices(parser),
    }


def _sub_help(sub_action, name):
    for choice_action in sub_action._choices_actions:
        if choice_action.dest == name:
            return choice_action.help
    return ""


# --- zsh ---------------------------------------------------------------


def _zq(text):
    """Escape a help string for a zsh `_describe` 'name:desc' entry."""
    return text.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:")


def render_zsh(model):
    lines = ["#compdef owa-piggy", "", "_owa_piggy() {", "  local -a _cmds", "  _cmds=("]
    for c in model["commands"]:
        lines.append(f"    '{c['name']}:{_zq(c['help'])}'")
    lines += [
        "  )",
        "  if (( CURRENT == 2 )); then",
        "    _describe -t commands 'owa-piggy command' _cmds",
        "    return",
        "  fi",
        "  case ${words[2]} in",
    ]
    for c in model["commands"]:
        words = list(c["options"])
        if c["subcommands"]:
            words += [s["name"] for s in c["subcommands"]]
        joined = " ".join(words)
        if joined:
            lines.append(f"    {c['name']}) compadd -- {joined} ;;")
    lines += [
        "  esac",
        "}",
        "",
        '_owa_piggy "$@"',
        "",
    ]
    return "\n".join(lines)


# --- bash --------------------------------------------------------------


def render_bash(model):
    cmd_names = " ".join(c["name"] for c in model["commands"])
    globals_ = " ".join(model["global_options"])
    lines = [
        "# bash completion for owa-piggy",
        "_owa_piggy() {",
        "  local cur prev cmd",
        '  cur="${COMP_WORDS[COMP_CWORD]}"',
        '  cmd="${COMP_WORDS[1]}"',
        f'  local commands="{cmd_names}"',
        f'  local globals="{globals_}"',
        '  if [ "$COMP_CWORD" -eq 1 ]; then',
        '    COMPREPLY=( $(compgen -W "$commands $globals" -- "$cur") )',
        "    return",
        "  fi",
        '  case "$cmd" in',
    ]
    for c in model["commands"]:
        words = list(c["options"])
        if c["subcommands"]:
            words += [s["name"] for s in c["subcommands"]]
        joined = " ".join(words)
        lines.append(f'    {c["name"]}) COMPREPLY=( $(compgen -W "{joined}" -- "$cur") ) ;;')
    lines += [
        "  esac",
        "}",
        "complete -F _owa_piggy owa-piggy",
        "",
    ]
    return "\n".join(lines)


# --- fish --------------------------------------------------------------


def _fq(text):
    return text.replace("\\", "\\\\").replace("'", "\\'")


def render_fish(model):
    lines = [
        "# fish completion for owa-piggy",
        "# Disable file completion unless a command opts back in.",
        "complete -c owa-piggy -f",
    ]
    cond = "not __fish_seen_subcommand_from " + " ".join(c["name"] for c in model["commands"])
    for c in model["commands"]:
        lines.append(f"complete -c owa-piggy -n '{cond}' -a {c['name']} -d '{_fq(c['help'])}'")
    for c in model["commands"]:
        seen = f"__fish_seen_subcommand_from {c['name']}"
        for sub in c["subcommands"]:
            lines.append(
                f"complete -c owa-piggy -n '{seen}' -a {sub['name']} -d '{_fq(sub['help'])}'"
            )
        for opt in c["options"]:
            if opt.startswith("--"):
                lines.append(f"complete -c owa-piggy -n '{seen}' -l {opt[2:]}")
            elif opt.startswith("-"):
                lines.append(f"complete -c owa-piggy -n '{seen}' -o {opt[1:]}")
    lines.append("")
    return "\n".join(lines)


RENDERERS = {"zsh": render_zsh, "bash": render_bash, "fish": render_fish}
FILENAMES = {"zsh": "_owa-piggy", "bash": "owa-piggy.bash", "fish": "owa-piggy.fish"}


def generate():
    """Return {shell: rendered_text} for all shells."""
    model = collect(_build_parser())
    return {shell: RENDERERS[shell](model) for shell in RENDERERS}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate owa-piggy shell completions.")
    parser.add_argument("--stdout", choices=sorted(RENDERERS), help="print one shell to stdout")
    args = parser.parse_args(argv)
    rendered = generate()
    if args.stdout:
        sys.stdout.write(rendered[args.stdout])
        return 0
    out_dir = Path(__file__).resolve().parent / "completions"
    out_dir.mkdir(exist_ok=True)
    for shell, text in rendered.items():
        (out_dir / FILENAMES[shell]).write_text(text)
        print(f"wrote {out_dir / FILENAMES[shell]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
