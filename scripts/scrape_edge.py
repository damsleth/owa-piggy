#!/usr/bin/env python3
"""scrape_edge.py - extract OWA_REFRESH_TOKEN and OWA_TENANT_ID from a
running Edge (headless or not) via Chrome DevTools Protocol.

Assumes Edge is running with --remote-debugging-port=9222 and has an OWA
tab loaded. Prints KEY=value lines on stdout in the same format that
`owa-piggy --save-config` consumes on stdin.

Usage:
    python3 scrape_edge.py | owa-piggy --save-config

Or with a different port:
    CDP_PORT=9333 python3 scrape_edge.py

Zero external deps - minimal WS client over the stdlib.
"""

import base64
import json
import os
import secrets
import socket
import struct
import sys
import time
import urllib.request

PORT = int(os.environ.get('CDP_PORT', '9222'))
# Match either the OWA tab or a login redirect so we notice when the SPA
# gives up and hands off to AAD.
TAB_URL_SUBSTRING = os.environ.get('CDP_TAB_MATCH', '')
WAIT_SECONDS = int(os.environ.get('CDP_WAIT', '45'))
# Freshness threshold for the cached ID token. The SPA refresh-token hard
# expiry is 24h from issue; returning anything older is guaranteed to fail
# downstream with AADSTS700084. Subtract a 1h safety margin.
STALE_AFTER_SECONDS = 23 * 3600
# Exit codes used by the shell wrapper to decide what to do next.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_REAUTH = 2


# The JS returns one of four shapes so the Python caller can act on each:
#   { rt, tid, age }  -> success, token extracted (age in seconds of cache)
#   { err: str }      -> hard failure, stop and report (exit 1)
#   { reauth: str }   -> session cookies gone, interactive sign-in required
#                        (exit 2 so the shell wrapper can reopen Edge)
#   { retry: str }    -> transient: page loading, MSAL cache not populated,
#                        cached RT stale and waiting for the SPA's silent
#                        refresh. Python keeps polling until deadline.
#
# Staleness check: MSAL.js rotates the ID token and refresh token together on
# every successful exchange, so the ID token's JWT `iat` is a reliable proxy
# for RT freshness. The SPA refresh-token hard-expiry is 24h absolute
# (AADSTS700084) - if `iat` is older than ~23h the cached RT will reproduce
# that error downstream. Report {retry} and let MSAL's silent-refresh take a
# swing; if cookies are also gone the SPA redirects to login and we catch it
# via {reauth}.
#
# Login redirect: when silent refresh fails MSAL navigates to
# login.microsoftonline.com (different origin, no MSAL cache). Detect the
# host early and surface {reauth} so the shell can re-open Edge visibly.
EXPR_TEMPLATE = r"""(() => {
  try {
    const host = location.hostname;
    const onLogin = host.startsWith('login.') ||
                    host.endsWith('.b2clogin.com') ||
                    host === 'account.microsoft.com';
    if (onLogin) {
      return { reauth: 'tab on ' + location.href +
               '; sidecar profile session expired' };
    }
    const onOwa = host.endsWith('cloud.microsoft') ||
                  host.endsWith('office.com') ||
                  host.endsWith('office365.com');
    if (!onOwa) return { retry: 'tab on ' + location.href + ' (waiting for OWA)' };
    if (document.readyState !== 'complete') return { retry: 'document ' + document.readyState };
    let keys;
    try { keys = Object.keys(localStorage); }
    catch (e) {
      return { err: 'localStorage denied on ' + host + ': ' + e.message +
               '. Edge --headless=new cannot complete MS broker-SSO; ' +
               're-run with OWA_RESEED_HEADLESS=0 (visible Edge window).' };
    }
    const find = s => keys.find(k => k.includes(s));
    const rtKey = find('|refreshtoken|'), idKey = find('|idtoken|');
    if (!rtKey || !idKey) return { retry: 'MSAL cache not populated yet' };

    const idEntry = JSON.parse(localStorage[idKey]);
    const rtEntry = JSON.parse(localStorage[rtKey]);

    // Decode the ID token JWT payload to read iat. If decode fails fall
    // through - worst case the downstream exchange surfaces the real error.
    let ageSec = null;
    const idJwt = idEntry.secret;
    if (idJwt && idJwt.split('.').length >= 2) {
      try {
        const b64 = idJwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
        const padded = b64 + '='.repeat((4 - b64.length % 4) % 4);
        const payload = JSON.parse(atob(padded));
        if (payload && payload.iat) {
          ageSec = Math.floor(Date.now() / 1000) - payload.iat;
        }
      } catch (_) { /* ignore */ }
    }
    if (ageSec !== null && ageSec > __STALE__) {
      return { retry: 'MSAL cache stale (ID token issued ' +
               Math.round(ageSec / 3600) + 'h ago, 24h SPA ceiling); ' +
               'waiting for silent refresh' };
    }

    const token = rtEntry.secret || rtEntry.data;
    if (!token || !(token.startsWith('1.') || token.startsWith('0.'))) {
      return { err: 'refresh token not in FOCI shape; log into the sidecar ' +
               'profile manually once so the broker writes a real MSAL entry' };
    }
    return { rt: token, tid: idEntry.realm || idKey.split('|')[5], age: ageSec };
  } catch (e) {
    return { err: 'scrape threw: ' + (e && e.message || e) };
  }
})()"""

EXPR = EXPR_TEMPLATE.replace('__STALE__', str(STALE_AFTER_SECONDS))


def find_tab():
    """Pick the primary page target from CDP /json. We deliberately accept
    any `type=page` target rather than filtering on URL, because the tab
    legitimately moves between origins during the auth dance (outlook ->
    login.microsoftonline.com -> outlook). The WebSocket target id stays
    constant across same-tab navigations, so we grab whichever page target
    exists and let the JS decide what's going on via location.hostname."""
    deadline = time.time() + WAIT_SECONDS
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://localhost:{PORT}/json', timeout=2) as r:
                tabs = json.loads(r.read())
            pages = [t for t in tabs if t.get('type') == 'page']
            if TAB_URL_SUBSTRING:
                matched = [t for t in pages if TAB_URL_SUBSTRING in t.get('url', '').lower()]
                if matched:
                    return matched[0]
            elif pages:
                return pages[0]
            last_err = f'no page-type target yet (saw {len(tabs)} targets)'
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5)
    raise TimeoutError(f'CDP tab not ready: {last_err}')


def ws_handshake(path):
    """Open a WS connection to localhost:PORT at `path`. Returns the raw socket."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    s = socket.create_connection(('localhost', PORT))
    req = (
        f'GET {path} HTTP/1.1\r\n'
        f'Host: localhost:{PORT}\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Key: {key}\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n'
    )
    s.sendall(req.encode())
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError('WS handshake: connection closed')
        buf += chunk
    status_line = buf.split(b'\r\n', 1)[0]
    if b' 101 ' not in status_line:
        raise ConnectionError(f'WS handshake: {status_line!r}')
    return s


def _ws_send_frame(s, opcode, payload):
    """Send a single masked frame with the given opcode (client -> server)."""
    data = payload.encode('utf-8') if isinstance(payload, str) else payload
    mask = secrets.token_bytes(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    L = len(data)
    hdr = bytes([0x80 | (opcode & 0x0f)])  # FIN + opcode
    if L < 126:
        hdr += bytes([0x80 | L])
    elif L < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack('>H', L)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack('>Q', L)
    s.sendall(hdr + mask + masked)


def ws_send_text(s, payload):
    _ws_send_frame(s, 0x1, payload)


def ws_send_pong(s, payload=b''):
    _ws_send_frame(s, 0xA, payload)


def ws_recv_text(s):
    """Receive a single text frame (server -> client, unmasked)."""
    def recv_n(n):
        buf = b''
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('WS: connection closed mid-frame')
            buf += chunk
        return buf

    # Handle control frames (ping) and reassemble fragmented text frames.
    parts = []
    while True:
        b1, b2 = recv_n(2)
        fin = b1 & 0x80
        opcode = b1 & 0x0f
        masked = b2 & 0x80
        L = b2 & 0x7f
        if L == 126:
            L = struct.unpack('>H', recv_n(2))[0]
        elif L == 127:
            L = struct.unpack('>Q', recv_n(8))[0]
        if masked:
            mask = recv_n(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(recv_n(L)))
        else:
            payload = recv_n(L)
        if opcode == 0x9:  # ping - echo body back as pong (RFC 6455 §5.5.3)
            ws_send_pong(s, payload)
            continue
        if opcode == 0x8:  # close
            raise ConnectionError('WS: server sent close')
        parts.append(payload)
        if fin:
            break
    return b''.join(parts).decode('utf-8')


def cdp_call(path, method, params=None):
    """Fire one CDP command and wait for the matched reply. Reopens the WS
    per call so we never have to manage message IDs across retries - the
    handshake cost is negligible against the 0.5s poll interval."""
    s = ws_handshake(path)
    try:
        ws_send_text(s, json.dumps({
            'id': 1,
            'method': method,
            'params': params or {},
        }))
        while True:
            msg = json.loads(ws_recv_text(s))
            if msg.get('id') == 1:
                return msg
    finally:
        s.close()


def eval_once(path):
    """Run EXPR in the page and return the parsed JS return value."""
    msg = cdp_call(path, 'Runtime.evaluate', {
        'expression': EXPR,
        'returnByValue': True,
        'awaitPromise': False,
    })
    result = msg.get('result', {}).get('result', {})
    if result.get('type') != 'object' or 'value' not in result:
        return {'err': f'unexpected CDP response: {msg}'}
    return result['value']


def reload_page(path):
    """Force a hard reload to kickstart MSAL's state machine when a stale
    cache has the SPA wedged. Ignore errors - if the reload call fails the
    outer loop will just keep polling and eventually time out."""
    try:
        cdp_call(path, 'Page.reload', {'ignoreCache': True})
    except Exception:
        pass


def main():
    try:
        tab = find_tab()
    except TimeoutError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        print(f'  Is Edge running with --remote-debugging-port={PORT}?', file=sys.stderr)
        return EXIT_ERROR

    ws_url = tab['webSocketDebuggerUrl']
    # Extract path component (CDP gives ws://localhost:9222/devtools/page/<id>)
    path = '/' + ws_url.split('/', 3)[3]

    # Poll until the SPA lands on OWA with a fresh MSAL cache, OR until it
    # redirects to login (session expired), OR we hit a hard error. For
    # {retry}, keep polling; if we see the "stale cache" retry reason for
    # longer than STALE_RELOAD_AFTER seconds, kick MSAL with a Page.reload to
    # force it off a wedged state.
    STALE_RELOAD_AFTER = 8  # seconds
    deadline = time.time() + WAIT_SECONDS
    last_retry = 'no attempts yet'
    stale_since = None
    reloaded = False

    while time.time() < deadline:
        v = eval_once(path)

        if v.get('err'):
            print(f'ERROR: {v["err"]}', file=sys.stderr)
            return EXIT_ERROR

        if v.get('reauth'):
            print(f'REAUTH: {v["reauth"]}', file=sys.stderr)
            return EXIT_NEEDS_REAUTH

        if v.get('rt') and v.get('tid'):
            age_raw = v.get('age')
            try:
                age_min = int(age_raw) // 60 if age_raw is not None else None
            except (TypeError, ValueError):
                age_min = None
            age_hint = f' (cache age ~{age_min}min)' if age_min is not None else ''
            print(f'# scraped fresh token{age_hint}', file=sys.stderr)
            print(f'OWA_REFRESH_TOKEN={v["rt"]}')
            print(f'OWA_TENANT_ID={v["tid"]}')
            return EXIT_OK

        if v.get('retry'):
            last_retry = v['retry']
            # Track how long we have been seeing a stale-cache retry. The
            # SPA's silent refresh is normally a few seconds; if it hasn't
            # moved after STALE_RELOAD_AFTER the SPA is likely wedged - a
            # hard reload kicks MSAL back into its auth state machine.
            if 'stale' in last_retry.lower():
                if stale_since is None:
                    stale_since = time.time()
                elif not reloaded and time.time() - stale_since > STALE_RELOAD_AFTER:
                    print(f'# cache still stale after {STALE_RELOAD_AFTER}s, '
                          f'forcing page reload', file=sys.stderr)
                    reload_page(path)
                    reloaded = True
                    stale_since = None
            else:
                stale_since = None
            time.sleep(0.5)
            continue

        print(f'ERROR: unexpected result shape: {v}', file=sys.stderr)
        return EXIT_ERROR

    # Timeout. If the last state we saw was a stale cache, the session likely
    # needs interactive sign-in - escalate to exit 2 so the shell wrapper can
    # reopen Edge for the user instead of just reporting the timeout.
    if 'stale' in last_retry.lower():
        print(f'REAUTH: cache remained stale past {WAIT_SECONDS}s; '
              f'silent refresh failed, interactive sign-in needed',
              file=sys.stderr)
        return EXIT_NEEDS_REAUTH

    print(f'ERROR: timed out waiting for OWA SPA; last status: {last_retry}',
          file=sys.stderr)
    return EXIT_ERROR


if __name__ == '__main__':
    sys.exit(main())
