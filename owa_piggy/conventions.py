"""owa-piggy's CLI wire contract.

The wire contract (action/error envelopes, the doctor payload shape,
the 0-5 exit-code taxonomy) is defined here. owa-piggy keeps
this self-contained rather than depending on a separate package, so it
installs cleanly with no third-party runtime dependency and stays
independently shippable.

The auth broker has no long-running streaming actions, so the NDJSON
``stream_*`` helpers are intentionally omitted here.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

from . import __version__

__all__ = [
    "EXIT_OK",
    "EXIT_USER_ERROR",
    "EXIT_TRANSIENT",
    "EXIT_AUTH",
    "EXIT_NOT_FOUND",
    "EXIT_PARTIAL",
    "TOOL_NAME",
    "action_envelope",
    "emit_action",
    "DoctorFinding",
    "DoctorPayload",
]

TOOL_NAME = "owa-piggy"


# --- Exit codes ------------------------------------------------------------

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_TRANSIENT = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_PARTIAL = 5  # 2, 4 and 5 are unused here; they complete the owa-* suite taxonomy


# --- internals -------------------------------------------------------------


def _writeln(obj: Mapping[str, Any], stream: TextIO | None) -> None:
    stream = stream if stream is not None else sys.stdout
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


# --- Action envelope -------------------------------------------------------


def action_envelope(
    *,
    command: str,
    ok: bool,
    stats: Mapping[str, Any] | None = None,
    warnings: Iterable[str] | None = None,
    error: Mapping[str, Any] | None = None,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "tool": TOOL_NAME,
        "version": __version__,
        "command": command,
        "ok": bool(ok),
        "duration_ms": float(duration_ms) if duration_ms is not None else 0.0,
        "stats": dict(stats or {}),
        "warnings": list(warnings or []),
        "error": dict(error) if error else None,
    }


def emit_action(envelope: Mapping[str, Any], stream: TextIO | None = None) -> None:
    _writeln(envelope, stream)


# --- Doctor payload --------------------------------------------------------


@dataclass
class DoctorFinding:
    id: str
    severity: str
    message: str
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "severity": self.severity,
            "message": self.message,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass
class DoctorPayload:
    tool: str = TOOL_NAME
    version: str = __version__
    config_path: str | None = None
    auth: dict[str, Any] | None = None
    findings: list[DoctorFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.tool, "version": self.version}
        if self.config_path is not None:
            out["config_path"] = self.config_path
        if self.auth is not None:
            out["auth"] = self.auth
        out["findings"] = [f.to_dict() for f in self.findings]
        return out

    def exit_code(self) -> int:
        severities = {f.severity for f in self.findings}
        if "error" in severities:
            return EXIT_USER_ERROR
        return EXIT_OK
