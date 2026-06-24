"""Tests for owa_piggy.oauth.

No network. No real tokens. Everything that would touch a socket or AAD
is mocked: socket.getaddrinfo/socket.socket for the Happy Eyeballs
connector, and owa_piggy.oauth._OPENER.open for exchange_token.
"""

import json
import socket
import threading
import time
import urllib.error

import pytest

from owa_piggy import oauth

# --- _interleave_by_family --------------------------------------------------


def _info(family, sockaddr):
    """Build a getaddrinfo-shaped tuple: (family, type, proto, canon, sockaddr)."""
    return (family, socket.SOCK_STREAM, 0, "", sockaddr)


def test_interleave_v6_only():
    infos = [
        _info(socket.AF_INET6, ("::1", 443)),
        _info(socket.AF_INET6, ("::2", 443)),
    ]
    out = oauth._interleave_by_family(infos)
    assert [i[4] for i in out] == [("::1", 443), ("::2", 443)]


def test_interleave_v4_only():
    infos = [
        _info(socket.AF_INET, ("1.1.1.1", 443)),
        _info(socket.AF_INET, ("2.2.2.2", 443)),
    ]
    out = oauth._interleave_by_family(infos)
    assert [i[4] for i in out] == [("1.1.1.1", 443), ("2.2.2.2", 443)]


def test_interleave_uneven_mix():
    infos = [
        _info(socket.AF_INET6, ("::1", 443)),
        _info(socket.AF_INET6, ("::2", 443)),
        _info(socket.AF_INET, ("1.1.1.1", 443)),
    ]
    out = oauth._interleave_by_family(infos)
    # v6, v4, v6 (the extra v6 trails since v4 ran out).
    assert [i[0] for i in out] == [socket.AF_INET6, socket.AF_INET, socket.AF_INET6]
    assert [i[4] for i in out] == [("::1", 443), ("1.1.1.1", 443), ("::2", 443)]


# --- _coerce_timeout --------------------------------------------------------


def test_coerce_timeout_number_passes_through():
    assert oauth._coerce_timeout(5) == 5
    assert oauth._coerce_timeout(2.5) == 2.5


def test_coerce_timeout_sentinel_becomes_none():
    # http.client hands over a non-numeric sentinel; it must coerce to None.
    sentinel = object()
    assert oauth._coerce_timeout(sentinel) is None


# --- happy_eyeballs_connect -------------------------------------------------


class _FakeSocket:
    """Stand-in for socket.socket with controllable connect behaviour.

    Tests patch oauth.socket.socket to a _FakeSocket subclass, so the same
    object is used as the factory AND as the isinstance() type the connector
    checks against - real OSError items still fail that isinstance check.
    """

    def __init__(self, family, type_):
        self.family = family
        self.type = type_
        self.timeout = None
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        self.closed = True


def test_happy_eyeballs_no_addresses(monkeypatch):
    monkeypatch.setattr(oauth.socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(OSError, match="getaddrinfo returned no addresses"):
        oauth.happy_eyeballs_connect("host", 443, 5)


def test_happy_eyeballs_all_fail(monkeypatch):
    infos = [
        _info(socket.AF_INET6, ("::1", 443)),
        _info(socket.AF_INET, ("1.1.1.1", 443)),
    ]
    monkeypatch.setattr(oauth.socket, "getaddrinfo", lambda *a, **k: infos)

    class _Failing(_FakeSocket):
        def connect(self, sockaddr):
            raise OSError(f"refused {sockaddr}")

    monkeypatch.setattr(oauth.socket, "socket", _Failing)

    with pytest.raises(OSError, match="refused"):
        oauth.happy_eyeballs_connect("host", 443, 5)


def test_happy_eyeballs_first_wins_late_winner_closed(monkeypatch):
    # Two addresses race. The first connects fast; the second connects after a
    # staggered delay so it lands in the reap path and must be closed.
    infos = [
        _info(socket.AF_INET6, ("::1", 443)),
        _info(socket.AF_INET, ("1.1.1.1", 443)),
    ]
    monkeypatch.setattr(oauth.socket, "getaddrinfo", lambda *a, **k: infos)

    closed_event = threading.Event()
    late_sockets = []

    class _Staggered(_FakeSocket):
        def connect(self, sockaddr):
            if sockaddr == ("::1", 443):
                # Fast winner.
                return
            # Slow late winner: connects successfully but after the winner.
            late_sockets.append(self)
            time.sleep(0.1)

        def close(self):
            super().close()
            closed_event.set()

    monkeypatch.setattr(oauth.socket, "socket", _Staggered)

    sock = oauth.happy_eyeballs_connect("host", 443, 5)
    assert isinstance(sock, _Staggered)
    assert sock.family == socket.AF_INET6
    assert not sock.closed
    # The late winner gets reaped and closed in a background thread.
    assert closed_event.wait(timeout=2.0)
    assert len(late_sockets) == 1
    assert late_sockets[0].closed


def test_happy_eyeballs_default_timeout_used_for_per_attempt(monkeypatch):
    # When read timeout coerces to None, per_attempt falls back to EXCHANGE_TIMEOUT.
    infos = [_info(socket.AF_INET, ("1.1.1.1", 443))]
    monkeypatch.setattr(oauth.socket, "getaddrinfo", lambda *a, **k: infos)

    seen = {}

    class _Ok(_FakeSocket):
        def connect(self, sockaddr):
            seen["per_attempt"] = self.timeout

    monkeypatch.setattr(oauth.socket, "socket", _Ok)

    sentinel = object()  # non-numeric -> read_timeout None
    sock = oauth.happy_eyeballs_connect("host", 443, sentinel)
    assert seen["per_attempt"] == oauth.EXCHANGE_TIMEOUT
    # Read timeout applied to the winner is the coerced None.
    assert sock.timeout is None


def test_happy_eyeballs_reap_handles_late_failure(monkeypatch):
    # Three addresses: first wins fast, one late attempt connects (gets reaped
    # and closed), one late attempt fails (reaped as an error, not closed).
    # This exercises both branches of the reap loop's isinstance check.
    infos = [
        _info(socket.AF_INET6, ("::1", 443)),
        _info(socket.AF_INET6, ("::2", 443)),
        _info(socket.AF_INET, ("1.1.1.1", 443)),
    ]
    monkeypatch.setattr(oauth.socket, "getaddrinfo", lambda *a, **k: infos)

    closed_event = threading.Event()
    failed_event = threading.Event()

    class _Mixed(_FakeSocket):
        def connect(self, sockaddr):
            if sockaddr == ("::1", 443):
                return  # fast winner
            if sockaddr == ("::2", 443):
                time.sleep(0.1)  # late success -> reaped + closed
                return
            time.sleep(0.1)  # late failure -> reaped, not closed
            failed_event.set()
            raise OSError("late refused")

        def close(self):
            super().close()
            closed_event.set()

    monkeypatch.setattr(oauth.socket, "socket", _Mixed)

    sock = oauth.happy_eyeballs_connect("host", 443, 5)
    assert isinstance(sock, _Mixed)
    assert closed_event.wait(timeout=2.0)
    assert failed_event.wait(timeout=2.0)


# --- capture_errors / _err_stream -------------------------------------------


def test_err_stream_defaults_to_stderr():
    # With no capture active in this thread, the sink is real stderr.
    import sys

    assert oauth._err_stream() is sys.stderr


def test_capture_errors_captures_and_restores():
    with oauth.capture_errors() as buf:
        print("hello", file=oauth._err_stream())
    assert "hello" in buf.getvalue()
    # Restored: no longer routes to the buffer.
    print("after", file=oauth._err_stream())
    assert "after" not in buf.getvalue()


def test_capture_errors_thread_locality():
    # One thread captures; another writes outside any capture. No bleed.
    inner_buf_holder = {}
    started = threading.Event()
    release = threading.Event()

    def inner():
        with oauth.capture_errors() as buf:
            inner_buf_holder["buf"] = buf
            started.set()
            release.wait(timeout=2.0)
            print("inner-write", file=oauth._err_stream())

    t = threading.Thread(target=inner)
    t.start()
    assert started.wait(timeout=2.0)

    # The outer thread writes while the inner thread's capture is active.
    with oauth.capture_errors() as outer_buf:
        print("outer-write", file=oauth._err_stream())

    release.set()
    t.join(timeout=2.0)

    inner_buf = inner_buf_holder["buf"]
    assert "inner-write" in inner_buf.getvalue()
    assert "outer-write" not in inner_buf.getvalue()
    assert "outer-write" in outer_buf.getvalue()
    assert "inner-write" not in outer_buf.getvalue()


# --- origin_for_client ------------------------------------------------------


def test_origin_for_client_override_wins():
    out = oauth.origin_for_client(oauth.CLIENT_ID, override="https://override.example")
    assert out == "https://override.example"


def test_origin_for_client_known_client():
    out = oauth.origin_for_client("5e3ce6c0-2b1f-4285-8d4b-75ee78787346")
    assert out == "https://teams.microsoft.com"


def test_origin_for_client_unknown_falls_back():
    out = oauth.origin_for_client("unknown-client-id")
    assert out == oauth.ORIGIN


# --- exchange_token ---------------------------------------------------------


class _FakeResp:
    """Context-manager response stand-in for _OPENER.open."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _http_error(body):
    return urllib.error.HTTPError(
        url="https://login.microsoftonline.com/fake-tenant/oauth2/v2.0/token",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=__import__("io").BytesIO(body.encode("utf-8")),
    )


def test_exchange_token_success(monkeypatch):
    body = json.dumps({"access_token": "fake-access-token", "expires_in": 3600})

    monkeypatch.setattr(oauth._OPENER, "open", lambda req, timeout=None: _FakeResp(body.encode()))

    out = oauth.exchange_token("1.FAKE-rt", "fake-tenant", oauth.CLIENT_ID, "scope")
    assert out == {"access_token": "fake-access-token", "expires_in": 3600}


def test_exchange_token_aadsts700084(monkeypatch):
    body = json.dumps(
        {
            "error": "invalid_grant",
            "error_description": "AADSTS700084: token expired\r\nTrace: x",
        }
    )
    monkeypatch.setattr(
        oauth._OPENER, "open", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(body))
    )

    with oauth.capture_errors() as buf:
        out = oauth.exchange_token("1.FAKE-rt", "fake-tenant", oauth.CLIENT_ID, "scope")
    assert out is None
    text = buf.getvalue()
    assert "ERROR: invalid_grant: AADSTS700084: token expired" in text
    assert "24h SPA hard-expiry" in text
    assert "owa-piggy reseed" in text


def test_exchange_token_aadsts70043(monkeypatch):
    body = json.dumps(
        {
            "error": "invalid_grant",
            "error_description": "AADSTS70043: sign-in frequency\r\nTrace: y",
        }
    )
    monkeypatch.setattr(
        oauth._OPENER, "open", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(body))
    )

    with oauth.capture_errors() as buf:
        out = oauth.exchange_token("1.FAKE-rt", "fake-tenant", oauth.CLIENT_ID, "scope")
    assert out is None
    text = buf.getvalue()
    assert "ERROR: invalid_grant: AADSTS70043: sign-in frequency" in text
    assert "Conditional Access" in text
    assert "owa-piggy reseed" in text


def test_exchange_token_generic_code(monkeypatch):
    body = json.dumps(
        {
            "error": "invalid_request",
            "error_description": "AADSTS90014: missing parameter\r\nTrace: z",
        }
    )
    monkeypatch.setattr(
        oauth._OPENER, "open", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(body))
    )

    with oauth.capture_errors() as buf:
        out = oauth.exchange_token("1.FAKE-rt", "fake-tenant", oauth.CLIENT_ID, "scope")
    assert out is None
    text = buf.getvalue()
    assert "ERROR: invalid_request: AADSTS90014: missing parameter" in text
    # No reseed hint for a generic code.
    assert "owa-piggy reseed" not in text


def test_exchange_token_unparseable_body(monkeypatch):
    # Body is not JSON: the except-Exception branch prints the truncated body.
    body = "not json at all"
    monkeypatch.setattr(
        oauth._OPENER, "open", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(body))
    )

    with oauth.capture_errors() as buf:
        out = oauth.exchange_token("1.FAKE-rt", "fake-tenant", oauth.CLIENT_ID, "scope")
    assert out is None
    text = buf.getvalue()
    assert "ERROR: HTTP 400: not json at all" in text


def test_exchange_token_timeout(monkeypatch):
    def _raise(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(oauth._OPENER, "open", _raise)

    with oauth.capture_errors() as buf:
        out = oauth.exchange_token("1.FAKE-rt", "fake-tenant", oauth.CLIENT_ID, "scope")
    assert out is None
    assert f"timed out after {oauth.EXCHANGE_TIMEOUT}s" in buf.getvalue()


def test_exchange_token_urlerror(monkeypatch):
    def _raise(req, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(oauth._OPENER, "open", _raise)

    with oauth.capture_errors() as buf:
        out = oauth.exchange_token("1.FAKE-rt", "fake-tenant", oauth.CLIENT_ID, "scope")
    assert out is None
    assert "name resolution failed" in buf.getvalue()
