"""The one HTTP call: refresh_token -> access_token at AAD.

Do not change CLIENT_ID, ORIGIN, or the Content-Type header without a
very clear reason. Those values make AAD accept the request; changing
them silently breaks the tool.
"""

import contextlib
import http.client
import io
import itertools
import json
import queue
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "9199bf20-a13f-4107-85dc-02114787ef48"
ORIGIN = "https://outlook.cloud.microsoft"

# Hard cap on the AAD token exchange, used both as the per-connection-attempt
# timeout in the Happy Eyeballs connector and as the read timeout afterwards.
# urllib's default is no timeout, so a stalled handshake would block forever;
# `status` runs one exchange per profile serially, so a single bad network
# window turns into minutes. Surfaced as TimeoutError/URLError below.
EXCHANGE_TIMEOUT = 15

# AAD's cross-origin check (AADSTS9002327) ties an SPA refresh-token grant
# to an Origin registered on that client's app registration. The default
# Outlook origin works for the default Teams Web client (9199bf20); the
# Teams web app (5e3ce6c0) is registered against a Teams origin instead.
# Callers can override per-profile via OWA_ORIGIN; this map supplies the
# right default when only OWA_CLIENT_ID is set.
KNOWN_CLIENT_ORIGINS = {
    "9199bf20-a13f-4107-85dc-02114787ef48": "https://outlook.cloud.microsoft",
    "5e3ce6c0-2b1f-4285-8d4b-75ee78787346": "https://teams.microsoft.com",
}


def origin_for_client(client_id, override=None):
    """Resolve the Origin header for a token exchange. An explicit
    override (OWA_ORIGIN) wins; otherwise fall back to the per-client
    default, then the global Outlook origin."""
    if override:
        return override
    return KNOWN_CLIENT_ORIGINS.get(client_id, ORIGIN)


# --- Happy Eyeballs ---------------------------------------------------------
# login.microsoftonline.com resolves to a long list of IPv6 addresses first,
# then IPv4. Python's socket.create_connection tries them strictly in order,
# so on a host with a broken/blackholed IPv6 default route every IPv6 attempt
# blocks until the OS TCP-connect timeout (~75s) before falling through to a
# working IPv4 address. With one exchange per profile that turns `status` into
# a multi-minute hang. curl and browsers avoid this by racing the families in
# parallel (RFC 8305 "Happy Eyeballs"); we do the same here.


def _interleave_by_family(infos):
    """Reorder getaddrinfo results to alternate IPv6/IPv4 so a dead family
    can't monopolise the front of the attempt list."""
    v6 = [i for i in infos if i[0] == socket.AF_INET6]
    v4 = [i for i in infos if i[0] == socket.AF_INET]
    out = []
    for a, b in itertools.zip_longest(v6, v4):
        if a:
            out.append(a)
        if b:
            out.append(b)
    return out


def _coerce_timeout(timeout):
    """http.client may hand us its _GLOBAL_DEFAULT_TIMEOUT sentinel; settimeout
    only accepts a number or None."""
    return timeout if isinstance(timeout, (int, float)) else None


def happy_eyeballs_connect(host, port, timeout):
    """Connect to host:port by racing every resolved address concurrently and
    returning the first socket that connects. Remaining attempts are reaped in
    the background. Raises the last connection error if all attempts fail."""
    infos = _interleave_by_family(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
    if not infos:
        raise OSError(f"getaddrinfo returned no addresses for {host}:{port}")

    read_timeout = _coerce_timeout(timeout)
    per_attempt = EXCHANGE_TIMEOUT if read_timeout is None else max(1.0, read_timeout)
    results = queue.Queue()

    def attempt(family, sockaddr):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(per_attempt)
            sock.connect(sockaddr)
        except OSError as exc:
            sock.close()
            results.put(exc)
            return
        results.put(sock)

    for family, _, _, _, sockaddr in infos:
        threading.Thread(target=attempt, args=(family, sockaddr), daemon=True).start()

    winner = None
    last_err = None
    consumed = 0
    while consumed < len(infos):
        item = results.get()
        consumed += 1
        if isinstance(item, socket.socket):
            winner = item
            break
        last_err = item

    if winner is None:
        raise last_err or OSError(f"could not connect to {host}:{port}")

    # Drain and close any sockets from attempts that connect after the winner,
    # so a late-but-successful IPv6 race doesn't leak a descriptor.
    remaining = len(infos) - consumed
    if remaining:

        def reap(n):
            for _ in range(n):
                item = results.get()
                if isinstance(item, socket.socket):
                    item.close()

        threading.Thread(target=reap, args=(remaining,), daemon=True).start()

    winner.settimeout(read_timeout)
    return winner


class _HappyEyeballsHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects via happy_eyeballs_connect, then performs
    the normal TLS handshake (cert + hostname verification preserved via the
    handler's SSL context)."""

    def connect(self):
        self.sock = happy_eyeballs_connect(self.host, self.port, self.timeout)
        if getattr(self, "_tunnel_host", None):
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _HappyEyeballsHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HappyEyeballsHTTPSConnection, req, context=self._context)


# Built once and reused: a default opener whose only deviation from the stock
# one is the Happy Eyeballs HTTPS connection.
_OPENER = urllib.request.build_opener(_HappyEyeballsHTTPSHandler())


# exchange_token reports AAD errors and hints by printing them. Callers like
# `status` want to capture that text and surface it as a value instead of
# leaking it to stdout. Routing through a thread-local sink (rather than
# swapping the global sys.stderr) lets several profiles be probed concurrently
# without clobbering each other's capture buffer, and leaves exchange_token's
# signature untouched so test mocks keep working.
_err_sink = threading.local()


def _err_stream():
    return getattr(_err_sink, "stream", None) or sys.stderr


@contextlib.contextmanager
def capture_errors():
    """Redirect exchange_token's ERROR/hint output to a buffer for the calling
    thread only. Yields the buffer; restores the previous sink on exit."""
    buf = io.StringIO()
    prev = getattr(_err_sink, "stream", None)
    _err_sink.stream = buf
    try:
        yield buf
    finally:
        _err_sink.stream = prev


def exchange_token(refresh_token, tenant_id, client_id, scope, origin=None):
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": scope,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # SPA clients require Origin to satisfy AAD's cross-origin check (AADSTS9002327)
            "Origin": origin_for_client(client_id, origin),
        },
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=EXCHANGE_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(err_body)
            code = err.get("error", "")
            desc = err.get("error_description", "").split("\r\n")[0]
            print(f"ERROR: {code}: {desc}", file=_err_stream())
            # AADSTS700084 is the 24h SPA hard-expiry: the refresh token has
            # hit its absolute lifetime ceiling (not the sliding window) and
            # cannot be extended by any amount of hourly rotation. The only
            # remedy is a fresh token from a live browser session. Point the
            # user at the automated reseed path so they are not left parsing
            # AAD error codes to figure out what to do next.
            if "AADSTS700084" in err_body:
                print(
                    "hint: refresh token has hit its 24h SPA hard-expiry. "
                    "Run `owa-piggy reseed` to fetch a fresh token "
                    "headlessly from the Edge sidecar profile.",
                    file=_err_stream(),
                )
            elif "AADSTS70043" in err_body:
                # The tenant-side Conditional Access sign-in-frequency cap
                # (typically 7 days). Same recovery path as 700084, but the
                # error code is different so we surface it explicitly.
                print(
                    "hint: refresh token expired by Conditional Access "
                    "sign-in-frequency policy. Run `owa-piggy reseed` to "
                    "fetch a fresh token headlessly from the Edge sidecar "
                    "profile (Edge must still have a live tenant session).",
                    file=_err_stream(),
                )
        except Exception:
            print(f"ERROR: HTTP {e.code}: {err_body[:200]}", file=_err_stream())
        return None
    except TimeoutError:
        # A read timeout surfaces as a bare socket.timeout/TimeoutError, not
        # wrapped in URLError, so catch it explicitly or it escapes and aborts
        # the whole `status` run on the first slow profile.
        print(f"ERROR: token exchange timed out after {EXCHANGE_TIMEOUT}s", file=_err_stream())
        return None
    except urllib.error.URLError as e:
        print(f"ERROR: {e.reason}", file=_err_stream())
        return None
