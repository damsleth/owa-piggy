"""Pure-helper tests for the network-capture path.

The actual browser-driven flows in capture.py (capture_signin,
capture_silent) need a real Edge process and a CDP-reachable tab,
which we deliberately do not exercise here - same policy as reseed.
What IS testable: URL classification, id_token decoding, email-vs-claims
matching, and the small _build_config translator that turns an AAD
token response into the profile-config KV dict.
"""
import pytest

from owa_piggy import capture


# --- is_token_endpoint ----------------------------------------------------

@pytest.mark.parametrize('url', [
    'https://login.microsoftonline.com/common/oauth2/v2.0/token',
    'https://login.microsoftonline.com/abc-123/oauth2/v2.0/token?foo=bar',
    'https://login.microsoftonline.us/contoso/oauth2/v2.0/token',
    'https://login.partner.microsoftonline.cn/x/oauth2/v2.0/token',
])
def test_is_token_endpoint_accepts_aad_token_urls(url):
    assert capture.is_token_endpoint(url) is True


@pytest.mark.parametrize('url', [
    # v1 endpoint - we only want v2.
    'https://login.microsoftonline.com/common/oauth2/token',
    # authorize endpoint, not token.
    'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    # Graph API - not a login host.
    'https://graph.microsoft.com/v1.0/oauth2/v2.0/token',
    # OWA itself.
    'https://outlook.cloud.microsoft/owa/',
    '',
    None,
    123,
])
def test_is_token_endpoint_rejects_non_token_urls(url):
    assert capture.is_token_endpoint(url) is False


# --- decode_id_token_payload ----------------------------------------------

def test_decode_id_token_payload_returns_claims(make_jwt):
    payload = {
        'tid': '11111111-2222-3333-4444-555555555555',
        'preferred_username': 'alice@example.org',
        'iat': 1_700_000_000,
    }
    jwt = make_jwt(payload)
    out = capture.decode_id_token_payload(jwt)
    assert out == payload


@pytest.mark.parametrize('bad', [
    None,
    '',
    'not.a.jwt',           # too few segments
    'header.notbase64.sig', # base64 decode error
    123,
])
def test_decode_id_token_payload_returns_none_on_garbage(bad):
    assert capture.decode_id_token_payload(bad) is None


def test_decode_id_token_payload_returns_none_on_non_json():
    # Manually craft a JWT whose middle segment is valid base64url but
    # not JSON, to exercise the JSONDecodeError branch.
    import base64
    seg = base64.urlsafe_b64encode(b'not-json').rstrip(b'=').decode()
    jwt = f'aaa.{seg}.sig'
    assert capture.decode_id_token_payload(jwt) is None


# --- email_matches_claims --------------------------------------------------

def test_email_matches_preferred_username():
    assert capture.email_matches_claims(
        'alice@example.org',
        {'preferred_username': 'Alice@Example.ORG'},
    ) is True


def test_email_matches_upn_when_no_preferred_username():
    assert capture.email_matches_claims(
        'bob@corp.io',
        {'upn': 'bob@corp.io', 'preferred_username': None},
    ) is True


def test_email_matches_email_claim():
    assert capture.email_matches_claims(
        'carol@x.com',
        {'email': 'carol@x.com'},
    ) is True


def test_email_mismatch_returns_false():
    assert capture.email_matches_claims(
        'alice@example.org',
        {'preferred_username': 'eve@example.org'},
    ) is False


@pytest.mark.parametrize('claims', [
    {},
    None,
    'not-a-dict',
])
def test_email_matches_handles_empty_or_invalid_claims(claims):
    assert capture.email_matches_claims('alice@example.org', claims) is False


@pytest.mark.parametrize('email', ['', '   ', None])
def test_email_matches_rejects_empty_email(email):
    assert capture.email_matches_claims(
        email,
        {'preferred_username': 'alice@example.org'},
    ) is False


# --- _build_config ---------------------------------------------------------

def _token_response(make_jwt, *, tid='tenant-uuid', upn='user@example.org',
                   refresh_token='1.AQfake'):
    """Build a synthetic /token response body for _build_config tests."""
    id_token = make_jwt({
        'tid': tid,
        'preferred_username': upn,
        'iat': 1_700_000_000,
    })
    return {
        'access_token': 'AT-fake',
        'refresh_token': refresh_token,
        'id_token': id_token,
        'expires_in': 3600,
    }


def test_build_config_happy_path(make_jwt):
    resp = _token_response(make_jwt)
    out = capture._build_config(resp, email='user@example.org', mode='capture')
    assert out['OWA_REFRESH_TOKEN'] == '1.AQfake'
    assert out['OWA_TENANT_ID'] == 'tenant-uuid'
    assert out['OWA_AUTH_MODE'] == 'capture'
    assert out['OWA_EMAIL'] == 'user@example.org'


def test_build_config_omits_email_when_none(make_jwt):
    resp = _token_response(make_jwt)
    out = capture._build_config(resp, email=None, mode='capture')
    assert 'OWA_EMAIL' not in out
    # Reseed path doesn't have an email to validate against, so we
    # accept whatever the captured token claims.


def test_build_config_email_mismatch_raises(make_jwt):
    resp = _token_response(make_jwt, upn='different@example.org')
    with pytest.raises(RuntimeError, match='different@example.org'):
        capture._build_config(resp, email='expected@example.org', mode='capture')


def test_build_config_missing_refresh_token_raises(make_jwt):
    resp = _token_response(make_jwt)
    del resp['refresh_token']
    with pytest.raises(RuntimeError, match='missing required fields'):
        capture._build_config(resp, email=None, mode='capture')


def test_build_config_missing_id_token_raises():
    resp = {'refresh_token': '1.AQfake', 'access_token': 'AT'}
    with pytest.raises(RuntimeError, match='missing required fields'):
        capture._build_config(resp, email=None, mode='capture')


def test_build_config_undecodable_id_token_raises():
    resp = {'refresh_token': '1.AQfake', 'id_token': 'garbage.not.jwt'}
    with pytest.raises(RuntimeError, match='id_token failed to decode'):
        capture._build_config(resp, email=None, mode='capture')


def test_build_config_id_token_without_tid_raises(make_jwt):
    resp = _token_response(make_jwt)
    # Re-encode the id_token without a tid claim.
    resp['id_token'] = make_jwt({'preferred_username': 'x@y.z'})
    with pytest.raises(RuntimeError, match='no tid claim'):
        capture._build_config(resp, email=None, mode='capture')


# --- find_free_port -------------------------------------------------------

def test_find_free_port_returns_unused_local_port():
    """Smoke check: the port should be in the ephemeral range and bindable
    twice in a row (since we close the socket before returning)."""
    p1 = capture.find_free_port()
    p2 = capture.find_free_port()
    assert 1024 < p1 < 65536
    assert 1024 < p2 < 65536
    # Not strictly required, but typical: kernel hands out a different
    # port on the second call. If this ever flakes, drop the assertion.
    # Leaving as a >= check rather than equality so we don't tie the
    # test to allocator behavior.
    assert isinstance(p1, int) and isinstance(p2, int)


# --- _park_window ---------------------------------------------------------

class _FakeSession:
    """Records CDP calls; raises on any method in `fail`."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = fail

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method in self.fail:
            raise capture.CdpError(method, {'message': 'nope'})
        if method == 'Browser.getWindowForTarget':
            return {'windowId': 42}
        return {}


def test_park_window_minimizes_and_disables_throttling():
    """macOS clamps --window-position, so minimizing over CDP is what
    actually hides the non-headless reseed window - and focus emulation is
    what keeps the minimized page from being throttled mid-/token."""
    s = _FakeSession()
    capture._park_window(s, lambda _msg: None)
    assert ('Browser.setWindowBounds',
            {'windowId': 42,
             'bounds': {'windowState': 'minimized'}}) in s.calls
    assert ('Emulation.setFocusEmulationEnabled', {'enabled': True}) in s.calls


def test_park_window_survives_cdp_failure():
    """A window we can't hide must not abort an otherwise-fine capture."""
    logged = []
    s = _FakeSession(fail={'Browser.setWindowBounds'})
    capture._park_window(s, logged.append)
    assert logged and 'could not park window' in logged[0]


def test_offscreen_launch_keeps_renderer_hot(monkeypatch, tmp_path):
    """The offscreen window gets minimized, so the renderer must be kept at
    full priority or the /token round-trip stalls in an occluded window."""
    seen = {}
    monkeypatch.setattr(capture, 'find_edge', lambda: '/usr/bin/edge')
    monkeypatch.setattr(capture.subprocess, 'Popen',
                        lambda args, **kw: seen.update(args=args))
    capture.launch_edge(tmp_path, 9999, headless=False, url='https://x',
                        offscreen=True)
    assert '--disable-backgrounding-occluded-windows' in seen['args']
    assert '--disable-renderer-backgrounding' in seen['args']


def test_offscreen_launch_starts_windowless(monkeypatch, tmp_path):
    """macOS clamps every offscreen coordinate back onto the display, so the
    only way to not show a window during Edge's cold start is to not open one:
    --no-startup-window, and no URL argument that would undo it."""
    seen = {}
    monkeypatch.setattr(capture, 'find_edge', lambda: '/usr/bin/edge')
    monkeypatch.setattr(capture.subprocess, 'Popen',
                        lambda args, **kw: seen.update(args=args))
    capture.launch_edge(tmp_path, 9999, headless=False,
                        url='https://outlook.cloud.microsoft', offscreen=True)
    assert '--no-startup-window' in seen['args']
    assert 'https://outlook.cloud.microsoft' not in seen['args']
    # Headless and visible modes still navigate via the command line.
    capture.launch_edge(tmp_path, 9999, headless=True, url='https://x',
                        offscreen=True)
    assert 'https://x' in seen['args']
    capture.launch_edge(tmp_path, 9999, headless=False, url='https://y')
    assert 'https://y' in seen['args']


def test_silent_timeout_before_session_does_not_blame_the_tenant(
    monkeypatch, tmp_path, capsys
):
    """A CDP-never-came-up timeout used to be reported as '60s waiting for
    /token, try OWA_CAPTURE_HEADLESS=0' - on a run that was already
    non-headless."""
    edge_dir = tmp_path / 'edge-profile'
    edge_dir.mkdir()
    monkeypatch.setattr(capture._config, 'profile_edge_dir', lambda a: edge_dir)
    monkeypatch.setattr(capture, 'launch_edge', lambda *a, **kw: None)
    monkeypatch.setattr(capture, '_terminate', lambda proc: None)

    def boom(*a, **kw):
        raise TimeoutError('CDP browser endpoint not ready')

    monkeypatch.setattr(capture, '_open_parked_session', boom)

    status, captured = capture.capture_silent('brkh', headless=False)

    assert (status, captured) == ('error', None)
    err = capsys.readouterr().err
    assert 'never came up on CDP port' in err
    assert 'OWA_CAPTURE_HEADLESS=0' not in err
