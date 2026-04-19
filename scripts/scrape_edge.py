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
TAB_URL_SUBSTRING = os.environ.get('CDP_TAB_MATCH', 'outlook')
WAIT_SECONDS = int(os.environ.get('CDP_WAIT', '30'))


EXPR = r"""(() => {
  const find = s => Object.keys(localStorage).find(k => k.includes(s));
  if (!find('|refreshtoken|') || !find('|idtoken|')) {
    return { err: 'no MSAL cache entries in localStorage; log in to OWA first' };
  }
  const parse = s => JSON.parse(localStorage[find(s)]);
  const rt = parse('|refreshtoken|'), it = parse('|idtoken|');
  const token = rt.secret || rt.data;
  if (!token || !(token.startsWith('1.') || token.startsWith('0.'))) {
    return { err: 'refresh token not in FOCI shape; seed from Edge with broker SSO active' };
  }
  return { rt: token, tid: it.realm || find('|idtoken|').split('|')[5] };
})()"""


def find_tab():
    """Poll the CDP /json endpoint until a matching tab appears."""
    deadline = time.time() + WAIT_SECONDS
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://localhost:{PORT}/json', timeout=2) as r:
                tabs = json.loads(r.read())
            for t in tabs:
                if TAB_URL_SUBSTRING in t.get('url', '').lower() and t.get('type') == 'page':
                    return t
            last_err = f'no tab url matched "{TAB_URL_SUBSTRING}" (saw {len(tabs)} tabs)'
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


def main():
    try:
        tab = find_tab()
    except TimeoutError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        print(f'  Is Edge running with --remote-debugging-port={PORT}?', file=sys.stderr)
        return 1

    ws_url = tab['webSocketDebuggerUrl']
    # Extract path component (CDP gives ws://localhost:9222/devtools/page/<id>)
    path = '/' + ws_url.split('/', 3)[3]

    s = ws_handshake(path)
    try:
        ws_send_text(s, json.dumps({
            'id': 1,
            'method': 'Runtime.evaluate',
            'params': {
                'expression': EXPR,
                'returnByValue': True,
                'awaitPromise': False,
            },
        }))
        while True:
            msg = json.loads(ws_recv_text(s))
            if msg.get('id') == 1:
                break
    finally:
        s.close()

    result = msg.get('result', {}).get('result', {})
    if result.get('type') != 'object' or 'value' not in result:
        print(f'ERROR: unexpected CDP response: {msg}', file=sys.stderr)
        return 1
    v = result['value']
    if v.get('err'):
        print(f'ERROR: {v["err"]}', file=sys.stderr)
        return 1
    if not v.get('rt') or not v.get('tid'):
        print(f'ERROR: missing rt/tid in result: {v}', file=sys.stderr)
        return 1

    print(f'OWA_REFRESH_TOKEN={v["rt"]}')
    print(f'OWA_TENANT_ID={v["tid"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
