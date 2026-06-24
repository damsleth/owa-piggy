"""Frame-level round-trip tests for the WebSocket framing in cdp.py.

cdp.py is the canonical CDP/WebSocket client. scripts/scrape_edge.py
carries a hand-copied twin of this framing because it must run as a
standalone script outside the package (see the docstring in cdp.py and
the "keep in sync" marker in scrape_edge.py). These tests pin the
canonical encode/decode behavior so a drift in the framing - the three
RFC 6455 length encodings, masking, ping/pong - is caught here even
though we never drive a real Edge.

We round-trip through socket.socketpair() with the send happening on a
background thread so a large payload can't deadlock against an unread
receive buffer.
"""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest

from owa_piggy import cdp


def _roundtrip(payload, opcode=0x1):
    """Send one masked frame on a socketpair and decode it on the other
    end via the real _recv_frame. Returns the decoded text."""
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    try:
        t = threading.Thread(target=cdp._send_frame, args=(a, opcode, payload))
        t.start()
        result = cdp._recv_frame(b)
        t.join(5)
        return result
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize(
    "size",
    [
        0,  # empty payload
        5,  # tiny
        125,  # last value of the 7-bit length
        126,  # first value needing the 16-bit length
        65535,  # last value of the 16-bit length
        65536,  # first value needing the 64-bit length
        70000,  # comfortably into the 64-bit length path
    ],
)
def test_frame_roundtrip_all_length_encodings(size):
    payload = "x" * size
    assert _roundtrip(payload) == payload


def test_frame_roundtrip_unicode():
    # Multi-byte UTF-8 must survive masking + length accounting (length is
    # in bytes, not codepoints).
    payload = "héllo — ✓ 𝟙"
    assert _roundtrip(payload) == payload


def test_recv_frame_answers_ping_then_returns_text():
    """A ping (0x9) must be answered with a pong inline and skipped, so the
    caller still gets the following text frame."""
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    try:

        def sender():
            cdp._send_frame(a, 0x9, b"pingdata")  # ping
            cdp._send_frame(a, 0x1, "after-ping")  # text

        t = threading.Thread(target=sender)
        t.start()
        # _recv_frame should swallow the ping (replying with a pong on b's
        # peer, i.e. back to a) and return the text frame.
        assert cdp._recv_frame(b) == "after-ping"
        t.join(5)
        # The pong (opcode 0xA) the receiver sent should be readable on a.
        pong = cdp._recv_frame(a)
        assert pong == "pingdata"  # _recv_frame decodes any non-control body
    finally:
        a.close()
        b.close()


def test_recv_frame_raises_on_close():
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    try:
        # 0x8 = close frame.
        threading.Thread(target=cdp._send_frame, args=(a, 0x8, b"")).start()
        with pytest.raises(ConnectionError):
            cdp._recv_frame(b)
    finally:
        a.close()
        b.close()


def test_standalone_scraper_declares_matching_cdp_parity_version():
    spec = importlib.util.spec_from_file_location(
        "scrape_edge_parity",
        Path("scripts/scrape_edge.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.CDP_HELPER_PARITY_VERSION == cdp.CDP_HELPER_PARITY_VERSION
