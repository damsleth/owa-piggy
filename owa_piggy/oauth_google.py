"""Google OAuth: installed-app consent flow + refresh_token -> access_token.

Unlike oauth.py (which piggybacks a first-party MSAL client with no app
registration of our own), owa-piggy owns a real Google OAuth client here, so
this is a normal authorization-code flow: no CDP, no Edge, no cache
scraping. `run_local_consent_flow` is the one-time interactive seed;
`refresh_access_token` is what every later mint/status/reseed call uses.
"""
import http.server
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REDIRECT_HOST = '127.0.0.1'

# Read-only by default - ingestion only ever needs to look at data, never
# change it. Callers wanting broader access pass their own scopes.
DEFAULT_SCOPES = (
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
)

EXCHANGE_TIMEOUT = 15


class ConsentError(RuntimeError):
    pass


def _consent_url(client_id, redirect_uri, scopes, state):
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ' '.join(scopes),
        'access_type': 'offline',
        # Forces Google to reissue a refresh_token even if this client
        # already has a prior grant on file - without it a re-consent
        # silently returns no refresh_token at all.
        'prompt': 'consent',
        'state': state,
    }
    return f'{AUTH_URL}?{urllib.parse.urlencode(params)}'


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.result = qs
        body = (b'<html><body>owa-piggy: signed in, you can close this tab.'
                b'</body></html>')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # ponytail: silence the default stderr access log, not useful here


def run_local_consent_flow(client_id, client_secret, scopes=DEFAULT_SCOPES,
                           timeout=300):
    """Open the Google consent screen, catch the redirect on a loopback
    server, exchange the code for tokens. Returns the token endpoint's
    JSON dict (access_token/refresh_token/expires_in/scope) or raises
    ConsentError.
    """
    server = http.server.HTTPServer((REDIRECT_HOST, 0), _CallbackHandler)
    server.result = None
    server.timeout = timeout
    port = server.server_address[1]
    redirect_uri = f'http://{REDIRECT_HOST}:{port}'
    state = secrets.token_urlsafe(16)

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    webbrowser.open(_consent_url(client_id, redirect_uri, scopes, state))
    thread.join(timeout)
    server.server_close()

    qs = server.result
    if qs is None:
        raise ConsentError(f'no response on {redirect_uri} within {timeout}s')
    if qs.get('state', [None])[0] != state:
        raise ConsentError('state mismatch on OAuth redirect')
    if 'error' in qs:
        raise ConsentError(f"Google denied consent: {qs['error'][0]}")
    code = qs.get('code', [None])[0]
    if not code:
        raise ConsentError('no authorization code in redirect')

    return _exchange(client_id, client_secret, {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
    })


def refresh_access_token(client_id, client_secret, refresh_token):
    """Refresh_token grant. Returns the token endpoint's JSON dict, or
    None on failure (error already printed to stderr, matching
    oauth.exchange_token's contract)."""
    try:
        return _exchange(client_id, client_secret, {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        })
    except ConsentError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return None


def _exchange(client_id, client_secret, grant_fields):
    data = urllib.parse.urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        **grant_fields,
    }).encode('utf-8')
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=EXCHANGE_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            err = json.loads(body)
            raise ConsentError(
                f"{err.get('error', '?')}: {err.get('error_description', '')}"
            ) from None
        except json.JSONDecodeError:
            raise ConsentError(f'HTTP {e.code}: {body[:200]}') from None
    except urllib.error.URLError as e:
        raise ConsentError(str(e.reason)) from None
    except TimeoutError:
        raise ConsentError(f'token exchange timed out after {EXCHANGE_TIMEOUT}s') from None
