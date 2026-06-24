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

from . import __version__

SCHEMA_VERSION = 1
SUITE = "owa-piggy"

# Commands whose output is non-interactive and stdout-clean, so --agent /
# --err-json can safely capture and wrap them. Interactive or UI-launching
# commands (setup, edge, reseed, debug, install-owa-tools) run unwrapped.
MACHINE_COMMANDS = frozenset({"token", "status", "version", "profiles"})

_TRUTHY = {"1", "true", "yes", "on"}


# --- schema builders (mirror owa_core.schema) ---------------------------


def flag(name, *, value=None, summary="", required=False, repeatable=False):
    row = {"name": name}
    if value is not None:
        row["value"] = value
    if summary:
        row["summary"] = summary
    if required:
        row["required"] = True
    if repeatable:
        row["repeatable"] = True
    return row


def command(
    name,
    summary="",
    *,
    output="json",
    flags=None,
    mutates=False,
    destructive=False,
    confirmation=False,
    idempotent=None,
):
    row = {
        "name": name,
        "summary": summary,
        "output": {"type": output},
        "flags": list(flags or []),
    }
    if mutates:
        row["mutates"] = True
    if destructive:
        row["destructive"] = True
    if confirmation:
        row["confirmation"] = {"flag": "--yes"}
    if idempotent is not None:
        row["idempotent"] = bool(idempotent)
    return row


def schema_for(commands):
    return {
        "tool": "owa-piggy",
        "suite": SUITE,
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "commands": list(commands),
    }


def _emit_json(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def maybe_emit_schema(argv, *, commands):
    """Handle ``schema``, ``schema <command>`` and ``--help --json``.

    Returns an exit code when handled, otherwise None.
    """
    if argv in (["--help", "--json"], ["help", "--json"]):
        return _emit_json(schema_for(commands))
    if not argv or argv[0] != "schema":
        return None
    payload = schema_for(commands)
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


def env_truthy(name):
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def split_mode_flags(argv):
    agent = env_truthy("OWA_AGENT")
    err_json = env_truthy("OWA_ERR_JSON")
    filtered = []
    for arg in argv:
        if arg == "--agent":
            agent = True
        elif arg == "--err-json":
            err_json = True
        else:
            filtered.append(arg)
    return agent, err_json, filtered


def command_name(argv):
    for arg in argv:
        if arg == "--":
            return ""
        if not arg.startswith("-"):
            return arg
    return ""


def envelope(command, data):
    meta = {
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


_PROFILE = flag(
    "--profile", value="<alias>", summary="Target a specific profile (also via OWA_PROFILE)"
)
_AUDIENCE = flag(
    "--audience", value="<name>", summary="Named FOCI audience (see `owa-piggy audiences`)"
)
_SCOPE = flag("--scope", value="<scope>", summary="Override scope explicitly")
_JSON = flag("--json", summary="Emit JSON")

COMMAND_SCHEMA = [
    command(
        "token",
        "Print an access token (default command)",
        flags=[
            _PROFILE,
            _AUDIENCE,
            _SCOPE,
            flag("--json", summary="Print the full token response as JSON"),
            flag("--env", summary="Print ACCESS_TOKEN= / EXPIRES_IN= lines"),
        ],
    ),
    command(
        "status",
        "Compact health summary for one or all profiles",
        flags=[
            _PROFILE,
            _AUDIENCE,
            _SCOPE,
            flag("--json", summary="Print health as JSON without token values"),
        ],
    ),
    command(
        "debug", "Dump full setup diagnostics for one profile", flags=[_PROFILE, _AUDIENCE, _SCOPE]
    ),
    command(
        "decode",
        "Print the JWT header and payload of the current token",
        output="text",
        flags=[_PROFILE, _AUDIENCE, _SCOPE],
    ),
    command(
        "remaining",
        "Print minutes remaining on the current token",
        output="text",
        flags=[_PROFILE, _AUDIENCE, _SCOPE],
    ),
    command(
        "setup",
        "Interactive first-time setup; creates the profile if new",
        mutates=True,
        flags=[
            _PROFILE,
            flag(
                "--email",
                value="<addr>",
                summary="Use the Edge network-capture flow (encrypted-MSAL/Okta tenants)",
            ),
        ],
    ),
    command(
        "reseed",
        "Fetch a fresh refresh token headlessly via the Edge sidecar",
        mutates=True,
        flags=[
            _PROFILE,
            flag("--all", summary="Reseed every configured profile"),
            flag("--json", summary="Emit an action envelope on stdout"),
        ],
    ),
    command(
        "edge", "Open a normal Edge window using a profile's sidecar session", flags=[_PROFILE]
    ),
    command(
        "tui",
        "Interactive token-health dashboard (profiles + freshness)",
        output="text",
        mutates=True,
        flags=[_PROFILE, _AUDIENCE, _SCOPE],
    ),
    command("audiences", "List all known FOCI-accessible audiences", output="text"),
    command("version", "Print version information", flags=[_JSON]),
    command(
        "profiles",
        "List / manage profiles (subcommands: list, new, set-default, delete)",
        mutates=True,
        flags=[_JSON],
    ),
    command(
        "install-owa-tools", "Install the companion owa-tools suite via Homebrew", mutates=True
    ),
]
