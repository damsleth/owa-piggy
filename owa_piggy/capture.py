"""Drive Edge via CDP and capture the FOCI refresh token off the wire.

This is the path for tenants whose SPA encrypts the MSAL token cache
(MSAL.js v3+ cacheEncryption=true). The encrypted-cache envelope
in localStorage is `{id, nonce, data}` with the plaintext RT inside
`data` wrapped by an AES-GCM key whose CryptoKey is non-extractable
in IndexedDB. We can't decrypt that from outside the browser context,
so instead we intercept the `/oauth2/v2.0/token` response body in
flight, before MSAL ever encrypts it.

Two flows:

* `capture_signin(alias, email)` - first-time onboarding. Launches
  Edge **visibly** so the user can complete sign-in (Okta password +
  Verify push, AAD MFA, whatever the tenant requires). Auto-closes
  Edge the moment a token response with `refresh_token` lands.

* `capture_silent(alias)` - scheduled reseed. Launches Edge **headless**.
  Forces MSAL to refresh by clearing access-token entries from
  localStorage and reloading the page; MSAL then POSTs to /token with
  its in-memory RT to get a new AT (and a rotated RT, which is what we
  want). No window flashes onscreen.

The localStorage wipe is selective: we keep RT and idToken entries so
MSAL still has something to refresh from. Without the wipe, MSAL serves
the cached AT in memory and never makes a /token call - the entire
point of this dance.
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import config as _config
from .cdp import CdpError, CdpSession, find_tab

# Match either OWA host the user might have in their bookmarks. The SPA
# at outlook.cloud.microsoft is the canonical post-2024 home; office.com
# is the legacy domain that still resolves and triggers the same auth.
OWA_URL = 'https://outlook.cloud.microsoft'

# Substring match for the AAD token endpoint. We deliberately do NOT
# pin to login.microsoftonline.com - some sovereign clouds (USGov, China)
# use different login hosts but the same /oauth2/v2.0/token suffix.
TOKEN_PATH_SUFFIX = '/oauth2/v2.0/token'

# Edge binaries we know about. macOS first since this tool is macOS-first;
# the Linux paths exist for the rare dev who runs the test suite on Linux.
_EDGE_CANDIDATES = (
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/usr/bin/microsoft-edge',
    '/usr/bin/microsoft-edge-stable',
)


def find_edge():
    """Return the path to a Microsoft Edge binary, or None if absent.

    PATH lookup last so a brew-installed `microsoft-edge` shim doesn't
    win over the canonical .app bundle on macOS."""
    for path in _EDGE_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    on_path = shutil.which('microsoft-edge') or shutil.which('msedge')
    return on_path


def find_free_port():
    """Bind to port 0 to let the kernel pick an unused local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def launch_edge(edge_dir, port, *, headless, url, edge_path=None):
    """Launch Edge with a per-profile userdata dir + CDP listening on
    `port`. Returns the Popen handle.

    Headless uses --headless=new (the Chromium replacement for the old
    --headless=true) so the runtime is closer to a real browser - some
    SPAs detect the legacy headless and refuse to load. Visible mode
    parks the window at a sensible size for sign-in interaction."""
    binary = edge_path or find_edge()
    if not binary:
        raise RuntimeError(
            'Microsoft Edge not found. Tried: '
            + ', '.join(_EDGE_CANDIDATES)
            + ' and PATH lookup. Install Edge or set the path manually.'
        )
    args = [
        binary,
        '--disable-gpu',
        '--no-first-run',
        '--no-default-browser-check',
        f'--remote-debugging-port={port}',
        f'--user-data-dir={edge_dir}',
    ]
    if headless:
        args += [
            '--headless=new',
            '--window-position=-32000,-32000',
            '--window-size=1,1',
        ]
    else:
        args += ['--window-position=100,100', '--window-size=900,750']
    args.append(url)
    # Detach stdout/stderr so a slow CDP consumer can't backpressure
    # Edge into a hang; Edge's own crash logs land in the userdata dir.
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _terminate(proc):
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


# --- Pure helpers (unit-tested) --------------------------------------------

def is_token_endpoint(url):
    """True if `url` looks like an AAD v2 token endpoint.

    Accepts any login host (login.microsoftonline.com, login.microsoftonline.us,
    login.partner.microsoftonline.cn, ...) since we only care about the path
    suffix. The path is `/{tenant}/oauth2/v2.0/token` so a substring check on
    the suffix is unambiguous - no other AAD endpoint shares that tail."""
    if not isinstance(url, str):
        return False
    return TOKEN_PATH_SUFFIX in url and 'login.' in url


def decode_id_token_payload(id_token):
    """Decode the JWT payload of an id_token. Returns the claims dict, or
    None on malformed input. Pure - no signature verification (we trust
    AAD over TLS as the source).

    Used to extract `tid` (tenant) and `preferred_username`/`upn` for
    sanity-checking that the captured token belongs to the expected user.
    """
    if not isinstance(id_token, str) or id_token.count('.') < 2:
        return None
    seg = id_token.split('.')[1]
    pad = '=' * (-len(seg) % 4)
    try:
        raw = base64.urlsafe_b64decode(seg + pad)
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None


def email_matches_claims(email, claims):
    """Case-insensitive compare against the claims most likely to hold
    the user's email-shaped identifier.

    AAD puts the email-shaped identifier in different claims depending
    on tenant config: `preferred_username` is the canonical one for v2,
    `upn` shows up for synced AD users, `email` is the OIDC standard
    claim. Federated (Okta) tokens generally surface `preferred_username`.
    Returns True iff any of those matches."""
    if not email or not isinstance(claims, dict):
        return False
    target = email.strip().lower()
    if not target:
        return False
    for key in ('preferred_username', 'upn', 'email'):
        v = claims.get(key)
        if isinstance(v, str) and v.strip().lower() == target:
            return True
    return False


# --- Capture flow ----------------------------------------------------------

def _capture_token_response(session, *, deadline, log=None, tick=None):
    """Block until a /token response with refresh_token lands, then return
    its parsed body dict.

    AAD's token endpoint can be hit several times during a session - the
    auth-code redemption is the one we care about; subsequent silent
    refreshes also work. We accept the first response that has a
    refresh_token in it. Errors-from-AAD responses (e.g. interaction
    required) are ignored; we keep listening until the deadline.

    `tick(elapsed_s)` is called every ~5s while waiting so callers can
    print a heartbeat to stderr; without this, capture_silent looks
    indistinguishable from a hang to anyone watching the terminal.
    """
    if log is None:
        log = lambda *_: None  # noqa: E731
    if tick is None:
        tick = lambda *_: None  # noqa: E731

    pending_request_ids = set()
    started = time.monotonic()
    last_tick = started

    def _on_response_received(params):
        resp = params.get('response', {})
        url = resp.get('url', '')
        if is_token_endpoint(url):
            pending_request_ids.add(params.get('requestId'))
            log(f'observed token response: {url}')
            return True
        return False

    def _on_loading_finished(params):
        return params.get('requestId') in pending_request_ids

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        now = time.monotonic()
        if now - last_tick >= 5.0:
            tick(int(now - started))
            last_tick = now
        # First, see a token-endpoint responseReceived. Use a short
        # window so we can loop and re-check the deadline cleanly.
        try:
            session.wait_event(
                'Network.responseReceived',
                _on_response_received,
                timeout=min(remaining, 5.0),
            )
        except TimeoutError:
            continue

        # Wait for the body to finish so getResponseBody returns the full
        # payload (not just headers). loadingFinished is the signal.
        try:
            finished = session.wait_event(
                'Network.loadingFinished',
                _on_loading_finished,
                timeout=min(deadline - time.monotonic(), 10.0),
            )
        except TimeoutError:
            log('responseReceived without loadingFinished; retrying')
            continue
        request_id = finished['requestId']
        pending_request_ids.discard(request_id)

        try:
            body_msg = session.call(
                'Network.getResponseBody',
                {'requestId': request_id},
                timeout=10.0,
            )
        except CdpError as e:
            log(f'getResponseBody failed for {request_id}: {e}')
            continue

        body = body_msg.get('body', '')
        if body_msg.get('base64Encoded'):
            try:
                body = base64.b64decode(body).decode('utf-8')
            except Exception:
                continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            log('token response not JSON; skipping')
            continue

        if isinstance(parsed, dict) and parsed.get('refresh_token'):
            return parsed
        # Probably an AAD error envelope (interaction_required, etc.).
        # Keep listening - the SPA will retry.
        log(f'token response had no refresh_token; keys={list(parsed)}')

    raise TimeoutError('no /oauth2/v2.0/token response with refresh_token '
                       'observed before deadline')


def _open_session(port):
    """Wait for Edge to expose a page target, then open a CDP session."""
    tab = find_tab(port, timeout=20.0)
    return CdpSession(port, tab['webSocketDebuggerUrl'])


def _verbose():
    return bool(os.environ.get('OWA_CAPTURE_DEBUG'))


def _logger(prefix):
    if not _verbose():
        return lambda *_: None  # noqa: E731
    return lambda msg: print(f'[{prefix}] {msg}', file=sys.stderr)


def _ticker(alias):
    """Heartbeat printer for the wait-for-/token loop. Always on (not
    gated by OWA_CAPTURE_DEBUG) so a watching user sees the operation
    is alive, not hung."""
    return lambda elapsed: print(
        f'[{alias}] still waiting for /oauth2/v2.0/token response '
        f'({elapsed}s elapsed)...', file=sys.stderr)


def _capture_headless_default():
    """Honor OWA_CAPTURE_HEADLESS=0 as the escape hatch for tenants whose
    Conditional Access / device-compliance check fails in headless mode
    (mirrors OWA_RESEED_HEADLESS for the legacy scrape path). Default is
    headless: True so launchd doesn't pop a window onscreen."""
    return os.environ.get('OWA_CAPTURE_HEADLESS', '1').strip() != '0'


def capture_signin(alias, email, *, timeout=300):
    """Visible Edge for first-time setup. Returns a config dict on
    success, or raises RuntimeError with a user-facing message.

    The Edge profile dir is created if missing so subsequent
    `capture_silent` calls reuse the same session cookies. We do not
    pre-fill the email - AAD's home-realm discovery handles federation
    once the user types it in, and login_hint via URL is unreliable
    across MSAL versions.
    """
    log = _logger(f'capture/signin/{alias}')
    # Sign-in can legitimately take minutes (Okta Verify push approval,
    # MFA prompt, etc.). The visible Edge window is the primary signal,
    # but a stderr heartbeat reassures anyone watching the terminal.
    tick = lambda elapsed: print(  # noqa: E731
        f'[{alias}] still waiting for sign-in to complete '
        f'({elapsed}s elapsed)...', file=sys.stderr)
    edge_dir = _config.profile_edge_dir(alias)
    edge_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    port = find_free_port()
    log(f'launching Edge visibly on port {port}, userdata={edge_dir}')
    proc = launch_edge(edge_dir, port, headless=False, url=OWA_URL)
    session = None
    try:
        session = _open_session(port)
        session.call('Network.enable', {})
        deadline = time.monotonic() + timeout
        log(f'awaiting /token response (timeout {timeout}s)...')
        token_response = _capture_token_response(
            session, deadline=deadline, log=log, tick=tick)
    finally:
        if session is not None:
            session.close()
        _terminate(proc)

    return _build_config(token_response, email=email, mode='capture')


def capture_silent(alias, *, timeout=30):
    """Headless Edge for scheduled reseed. Returns (status, config_dict):

      ('ok', dict)        success - dict has OWA_REFRESH_TOKEN/OWA_TENANT_ID
      ('reauth', None)    sidecar session cookies expired; user must
                          re-run `setup --email` interactively
      ('error', None)     other failure (no token observed, Edge launch
                          failed, etc.) - log line written to stderr

    Default is fully headless. Set OWA_CAPTURE_HEADLESS=0 to fall back
    to offscreen-but-not-headless when a tenant's CA / device-compliance
    check rejects truly headless Edge (mirrors OWA_RESEED_HEADLESS for
    the legacy scrape path). Even in the OWA_CAPTURE_HEADLESS=0 mode no
    onscreen window appears - the window is parked at -32000,-32000 -
    so launchd users still don't get a flashing browser.
    """
    log = _logger(f'capture/silent/{alias}')
    tick = _ticker(alias)
    edge_dir = _config.profile_edge_dir(alias)
    if not edge_dir.is_dir():
        log(f'no Edge profile dir at {edge_dir}; cannot reseed silently')
        return 'reauth', None
    headless = _capture_headless_default()
    port = find_free_port()
    log(f'launching Edge {"headless" if headless else "offscreen"} on port {port}')
    try:
        proc = launch_edge(edge_dir, port, headless=headless, url=OWA_URL)
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 'error', None

    session = None
    try:
        session = _open_session(port)
        session.call('Page.enable', {})
        session.call('Network.enable', {})

        # Give MSAL a moment to hydrate from localStorage and decide
        # whether to redirect to login. 1.5s is empirically enough on a
        # warm cache; the tighter timeout below catches stalls anyway.
        time.sleep(1.5)
        loc = session.call('Runtime.evaluate', {
            'expression': 'location.hostname',
            'returnByValue': True,
        })
        host = (loc.get('result', {}) or {}).get('result', {}).get('value', '')
        log(f'post-load host: {host}')
        if (host.startswith('login.')
                or host.endswith('.b2clogin.com')
                or host == 'account.microsoft.com'):
            return 'reauth', None

        # Wipe cached access tokens so MSAL has to round-trip /token.
        # The RT and idToken entries are kept so MSAL knows who the
        # session belongs to. In encrypted-cache mode the values are
        # opaque AES-GCM blobs but the keys are still readable, so the
        # substring filter on key names works regardless.
        wipe = session.call('Runtime.evaluate', {
            'expression': '''(() => {
                const ks = Object.keys(localStorage);
                let n = 0;
                for (const k of ks) {
                    if (k.includes('|accesstoken|')) {
                        localStorage.removeItem(k);
                        n++;
                    }
                }
                return n;
            })()''',
            'returnByValue': True,
        })
        wiped = (wipe.get('result', {}) or {}).get('result', {}).get('value', 0)
        log(f'wiped {wiped} accesstoken cache entries')

        session.call('Page.reload', {'ignoreCache': True})

        deadline = time.monotonic() + timeout
        log(f'awaiting /token response (timeout {timeout}s)...')
        token_response = _capture_token_response(
            session, deadline=deadline, log=log, tick=tick)
    except TimeoutError as e:
        print(f'[{alias}] timed out after {timeout}s waiting for '
              f'/oauth2/v2.0/token. Tenant may require non-headless '
              f'Edge - try OWA_CAPTURE_HEADLESS=0.', file=sys.stderr)
        log(f'timeout: {e}')
        return 'error', None
    except (ConnectionError, CdpError, OSError) as e:
        log(f'CDP failure: {e}')
        return 'error', None
    finally:
        if session is not None:
            session.close()
        _terminate(proc)

    return 'ok', _build_config(token_response, email=None, mode='capture')


def _build_config(token_response, *, email, mode):
    """Translate an AAD /token JSON response into the KV dict the
    profile config expects. Validates email-vs-claims if `email` is set.

    `mode` is stamped into OWA_AUTH_MODE so reseed knows which path to
    take next time around. We do not stamp the email itself on silent
    refresh - the user already proved identity at setup time."""
    rt = token_response.get('refresh_token')
    id_token = token_response.get('id_token')
    if not rt or not id_token:
        raise RuntimeError(
            f'token response missing required fields '
            f'(have {sorted(token_response.keys())})'
        )
    claims = decode_id_token_payload(id_token)
    if not claims:
        raise RuntimeError('captured id_token failed to decode')
    tid = claims.get('tid')
    if not tid:
        raise RuntimeError(
            'captured id_token has no tid claim; cannot determine tenant'
        )
    if email and not email_matches_claims(email, claims):
        observed = (claims.get('preferred_username')
                    or claims.get('upn') or claims.get('email') or '?')
        raise RuntimeError(
            f'captured token belongs to {observed!r}, not {email!r}. '
            f'Did the wrong account sign in? Wipe the profile and retry.'
        )
    out = {
        'OWA_REFRESH_TOKEN': rt,
        'OWA_TENANT_ID': tid,
        'OWA_AUTH_MODE': mode,
    }
    if email:
        out['OWA_EMAIL'] = email
    return out
