"""Minimal stdlib Chrome DevTools Protocol client.

Used by capture.py to drive Edge for both first-time setup (visible
sign-in, capture /token response) and silent reseed (headless, force a
silent refresh, capture /token response). Same primitives as the
inline WS code in scripts/scrape_edge.py - kept as a separate module
because that script stays self-contained for its own runtime
(invoked via `python3 scripts/scrape_edge.py` outside the package).
This is the canonical copy: the framing (length encodings, masking,
ping/pong) is regression-tested in tests/test_cdp.py, and the twin in
scrape_edge.py carries a "keep in sync" marker pointing back here.

CdpSession multiplexes one WebSocket connection between request/response
calls (call) and continuous event listening (wait_event). Buffered
events are not lost while a `call` is in flight, so a network event
that fires before we even ask for getResponseBody still gets delivered
to whoever is waiting for it.
"""

from __future__ import annotations

import base64
import contextlib
import json
import secrets
import socket
import struct
import time
import urllib.request
from collections.abc import Callable
from typing import Any

CDP_HELPER_PARITY_VERSION = 1


def find_tab(port: int, timeout: float = 15.0) -> dict[str, Any]:
    """Poll http://localhost:<port>/json until at least one page-type
    target appears, then return that target's metadata dict.

    Edge needs a moment after launch before its CDP HTTP endpoint is
    ready; we retry every 200ms. Any non-page targets (service workers,
    extension backgrounds) are ignored - we want the user-facing tab.
    """
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/json", timeout=2) as r:
                tabs: list[dict[str, Any]] = json.loads(r.read())
            pages = [t for t in tabs if t.get("type") == "page"]
            if pages:
                return pages[0]
            last_err = f"no page targets yet (saw {len(tabs)} total)"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.2)
    raise TimeoutError(f"CDP tab not ready on port {port}: {last_err}")


def _ws_handshake(host: str, port: int, path: str) -> socket.socket:
    """Open a raw WebSocket to ws://host:port<path>. Returns the socket
    after the 101 Switching Protocols response is consumed."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    s = socket.create_connection((host, port))
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("WS handshake: connection closed")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise ConnectionError(f"WS handshake failed: {status!r}")
    return s


def _send_frame(s: socket.socket, opcode: int, payload: str | bytes) -> None:
    """Send one masked frame (client -> server, RFC 6455)."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    mask = secrets.token_bytes(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    L = len(data)
    hdr = bytes([0x80 | (opcode & 0x0F)])
    if L < 126:
        hdr += bytes([0x80 | L])
    elif L < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack(">H", L)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack(">Q", L)
    s.sendall(hdr + mask + masked)


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("WS: connection closed mid-frame")
        buf += chunk
    return buf


def _recv_frame(s: socket.socket) -> str:
    """Receive one frame, handling fragmentation and control frames.
    Returns the full text payload as a str, or raises if the server
    closes the connection. Pings are answered inline with a pong."""
    parts: list[bytes] = []
    while True:
        b1, b2 = _recv_exact(s, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        L = b2 & 0x7F
        if L == 126:
            L = struct.unpack(">H", _recv_exact(s, 2))[0]
        elif L == 127:
            L = struct.unpack(">Q", _recv_exact(s, 8))[0]
        if masked:
            mask = _recv_exact(s, 4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(_recv_exact(s, L)))
        else:
            payload = _recv_exact(s, L)
        if opcode == 0x9:  # ping -> pong
            _send_frame(s, 0xA, payload)
            continue
        if opcode == 0x8:  # close
            raise ConnectionError("WS: server sent close")
        parts.append(payload)
        if fin:
            break
    return b"".join(parts).decode("utf-8")


class CdpSession:
    """One WebSocket multiplexed between call() and wait_event().

    Every CDP message has either an `id` (response to one of our calls)
    or a `method` (server-pushed event). We buffer events that arrive
    while a `call` is reading replies so wait_event() can deliver them
    in order afterwards.
    """

    def __init__(self, port: int, ws_url: str) -> None:
        path = "/" + ws_url.split("/", 3)[3]
        self._sock = _ws_handshake("localhost", port, path)
        self._next_id = 0
        self._buffered: list[dict[str, Any]] = []

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Send a CDP command, return its `result` dict.

        Raises TimeoutError if no matching reply arrives within `timeout`.
        Events received in the meantime are buffered for wait_event."""
        self._next_id += 1
        msg_id = self._next_id
        _send_frame(
            self._sock,
            0x1,
            json.dumps(
                {
                    "id": msg_id,
                    "method": method,
                    "params": params or {},
                }
            ),
        )
        self._sock.settimeout(timeout)
        try:
            while True:
                msg: dict[str, Any] = json.loads(_recv_frame(self._sock))
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        raise CdpError(method, msg["error"])
                    result: dict[str, Any] = msg.get("result", {})
                    return result
                if "method" in msg:
                    self._buffered.append(msg)
        finally:
            self._sock.settimeout(None)

    def wait_event(
        self,
        method_name: str,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Block until a matching event arrives. Returns the event's
        `params` dict.

        `predicate(params) -> bool` filters; pass None to take the first
        event of the given method. Raises TimeoutError on the deadline.
        Stray responses (id-bearing messages with no waiter) are dropped.
        """
        if predicate is None:
            predicate = lambda *_: True  # noqa: E731

        # Drain buffered events first so a fast event isn't missed.
        for i, buffered in enumerate(self._buffered):
            if buffered.get("method") == method_name and predicate(buffered.get("params", {})):
                self._buffered.pop(i)
                params: dict[str, Any] = buffered["params"]
                return params

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            self._sock.settimeout(max(0.1, remaining))
            try:
                msg: dict[str, Any] = json.loads(_recv_frame(self._sock))
            except socket.timeout:
                continue
            finally:
                self._sock.settimeout(None)
            if "id" in msg:
                continue
            if msg.get("method") == method_name and predicate(msg.get("params", {})):
                event_params: dict[str, Any] = msg["params"]
                return event_params
            # Any other event - keep for a later wait_event with a
            # different filter (e.g. loadingFinished after responseReceived).
            self._buffered.append(msg)
        raise TimeoutError(f"no {method_name} event matching predicate within {timeout}s")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sock.close()


class CdpError(RuntimeError):
    """Raised when a CDP method returns an error envelope."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        super().__init__(f"CDP {method} failed: {error}")
        self.method = method
        self.error = error
