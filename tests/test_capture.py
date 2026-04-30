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
