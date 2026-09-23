"""Machine surface for owa-piggy: command schema + agent/error envelopes.

This mirrors the owa-tools consumer contract (``owa_core.schema`` and
``owa_core.modes``) so an agent driving the broker sees the
same introspection surface as on the consumer CLIs:

    owa-piggy schema            # JSON command schema
    owa-piggy schema <command>  # one command
    owa-piggy --help --json     # same schema
    owa-piggy --agent <cmd>     # {"_owa": {...}, "data": <json>}
    owa-piggy --err-json <cmd>  # structured JSON error on stderr

Standalone (stdlib only) on purpose: the action/doctor envelopes live
in owa_piggy.conventions, and the schema and agent-mode layers are kept
here. The wire shapes are deliberately identical to owa_core's so the
introspection surface stays consistent across the tools.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import __version__

SCHEMA_VERSION = 1
SUITE = "owa-piggy"

# Commands whose output is non-interactive and stdout-clean, so --agent /
# --err-json can safely capture and wrap them. Interactive or UI-launching
# commands (setup, edge, reseed, debug, install-owa-tools) run unwrapped.
MACHINE_COMMANDS = frozenset({"token", "status", "version", "profiles", "clients"})

_TRUTHY = {"1", "true", "yes", "on"}


# --- schema, read off the live argparse parser ---------------------------

# What argparse cannot say about a command. Everything else - names,
# summaries, flags - is read from cli._build_parser() so the schema cannot
# drift from the CLI (scripts/gen-completions.py does the same for shells).
_META: dict[str, dict[str, Any]] = {
    "setup": {"mutates": True},
    "reseed": {"mutates": True},
    "tui": {"output": "text", "mutates": True},
    "decode": {"output": "text"},
    "remaining": {"output": "text"},
    "audiences": {"output": "text"},
    "profiles": {"mutates": True},
    "clients": {"mutates": True},
    "install-owa-tools": {"mutates": True},
}


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def command_schema() -> list[dict[str, Any]]:
    import argparse

    from .cli import _build_parser

    sub = next(a for a in _build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: _first_line(a.help) for a in sub._choices_actions}
    out = []
    for name, cmd_parser in sub.choices.items():
        flags = []
        for action in cmd_parser._actions:
            if not action.option_strings or action.help == argparse.SUPPRESS:
                continue
            if isinstance(action, argparse._HelpAction):
                continue
            row: dict[str, Any] = {"name": max(action.option_strings, key=len)}
            if action.nargs != 0:
                row["value"] = str(action.metavar or f"<{action.dest}>")
            if action.help:
                row["summary"] = _first_line(action.help)
            if isinstance(action, argparse._AppendAction):
                row["repeatable"] = True
            flags.append(row)
        meta = _META.get(name, {})
        cmd: dict[str, Any] = {
            "name": name,
            "summary": helps.get(name, ""),
            "output": {"type": meta.get("output", "json")},
            "flags": flags,
        }
        if meta.get("mutates"):
            cmd["mutates"] = True
        out.append(cmd)
    return out


def schema_for(commands: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tool": "owa-piggy",
        "suite": SUITE,
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "commands": list(commands),
    }


def _emit_json(payload: dict[str, Any]) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def maybe_emit_schema(argv: list[str]) -> int | None:
    """Handle ``schema``, ``schema <command>`` and ``--help --json``.

    Returns an exit code when handled, otherwise None.
    """
    if argv in (["--help", "--json"], ["help", "--json"]):
        return _emit_json(schema_for(command_schema()))
    if not argv or argv[0] != "schema":
        return None
    payload = schema_for(command_schema())
    if len(argv) > 2:
        print("schema accepts at most one command name", file=sys.stderr)
        return 2
    if len(argv) == 2:
        name = argv[1]
        matched = [c for c in payload["commands"] if c["name"] == name]
        if not matched:
            print(f"unknown schema command: {name}", file=sys.stderr)
            return 2
        payload = {**payload, "commands": matched}
    return _emit_json(payload)


# --- agent / error mode helpers (mirror owa_core.modes) -----------------


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def split_mode_flags(argv: list[str]) -> tuple[bool, bool, list[str]]:
    agent = env_truthy("OWA_AGENT")
    err_json = env_truthy("OWA_ERR_JSON")
    filtered: list[str] = []
    for arg in argv:
        if arg == "--agent":
            agent = True
        elif arg == "--err-json":
            err_json = True
        else:
            filtered.append(arg)
    return agent, err_json, filtered


def command_name(argv: list[str]) -> str:
    for arg in argv:
        if arg == "--":
            return ""
        if not arg.startswith("-"):
            return arg
    return ""


def envelope(command: str, data: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "suite": SUITE,
        "tool": "owa-piggy",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
    }
    if command:
        meta["command"] = command
    profile = os.environ.get("OWA_PROFILE", "").strip()
    if profile:
        meta["profile"] = profile
    return {"_owa": meta, "data": data}
