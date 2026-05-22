"""owa-piggy implementation of the hugr suite CLI contract.

Mirrors yaams/conventions.py and ledger/conventions.py - they will
collapse into a shared hugr-conventions package later. See
https://github.com/damsleth/hugr/blob/main/CONVENTIONS.md.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_TRANSIENT = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_PARTIAL = 5


_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+")
_TOKEN_FIELD_RE = re.compile(
  r'(?i)"(access_token|refresh_token|id_token|client_secret|api_key|secret)"\s*:\s*"[^"]*"'
)
_BODY_FIELD_RE = re.compile(
  r'(?i)"(body|content|text|html_body|plain_body)"\s*:\s*"[^"]*"'
)


def redact(text):
  if text is None:
    return ""
  if not isinstance(text, str):
    text = str(text)
  text = _JWT_RE.sub("<redacted-jwt>", text)
  text = _BEARER_RE.sub("Bearer <redacted>", text)
  text = _TOKEN_FIELD_RE.sub(lambda m: f'"{m.group(1)}":"<redacted>"', text)
  text = _BODY_FIELD_RE.sub(lambda m: f'"{m.group(1)}":"<redacted>"', text)
  return text


TOOL_NAME = "owa-piggy"


def _version() -> str:
  try:
    from owa_piggy import __version__
    return __version__
  except Exception:
    return "0.0.0"


def action_envelope(
  *,
  command,
  ok,
  stats=None,
  warnings=None,
  error=None,
  duration_ms=None,
):
  return {
    "tool": TOOL_NAME,
    "version": _version(),
    "command": command,
    "ok": bool(ok),
    "duration_ms": float(duration_ms) if duration_ms is not None else 0.0,
    "stats": dict(stats or {}),
    "warnings": list(warnings or []),
    "error": dict(error) if error else None,
  }


def emit_action(envelope, stream=None):
  stream = stream if stream is not None else sys.stdout
  stream.write(json.dumps(envelope, ensure_ascii=False) + "\n")
  stream.flush()


def data_error(*, command, code, message, hint=None):
  err = {"code": code, "message": message}
  if hint:
    err["hint"] = hint
  return {
    "tool": TOOL_NAME,
    "version": _version(),
    "command": command,
    "ok": False,
    "error": err,
  }


def emit_data_error(envelope, stream=None):
  stream = stream if stream is not None else sys.stdout
  stream.write(json.dumps(envelope, ensure_ascii=False) + "\n")
  stream.flush()


@dataclass
class DoctorFinding:
  id: str
  severity: str
  message: str
  hint: str | None = None

  def to_dict(self):
    out = {"id": self.id, "severity": self.severity, "message": self.message}
    if self.hint:
      out["hint"] = self.hint
    return out


@dataclass
class DoctorPayload:
  tool: str = TOOL_NAME
  config_path: str | None = None
  data_path: str | None = None
  auth: dict | None = None
  models: dict | None = None
  findings: list = field(default_factory=list)

  def to_dict(self):
    out = {"tool": self.tool, "version": _version()}
    if self.config_path is not None:
      out["config_path"] = self.config_path
    if self.data_path is not None:
      out["data_path"] = self.data_path
    if self.auth is not None:
      out["auth"] = self.auth
    if self.models is not None:
      out["models"] = self.models
    out["findings"] = [f.to_dict() for f in self.findings]
    return out

  def exit_code(self):
    severities = {f.severity for f in self.findings}
    if "error" in severities:
      return EXIT_USER_ERROR
    return EXIT_OK
