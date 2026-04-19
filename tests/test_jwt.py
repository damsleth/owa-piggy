"""Tests for JWT decode and remaining-minutes helpers."""
import pytest

from owa_piggy.jwt import decode_jwt, decode_jwt_segment, token_minutes_remaining


def test_decode_segment_round_trip(make_jwt):
    token = make_jwt({'exp': 9999, 'aud': 'https://graph.microsoft.com'})
    header_b64, payload_b64, _sig = token.split('.')
    header = decode_jwt_segment(header_b64)
    payload = decode_jwt_segment(payload_b64)
    assert header == {'alg': 'RS256', 'typ': 'JWT'}
    assert payload == {'exp': 9999, 'aud': 'https://graph.microsoft.com'}


def test_decode_segment_accepts_unpadded():
    import base64
    # {"a":1} base64url-encoded has length 8 unpadded.
    raw = b'{"a":1}'
    encoded = base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    assert decode_jwt_segment(encoded) == {'a': 1}


def test_token_minutes_remaining_future(frozen_time, make_jwt):
    token = make_jwt({'exp': int(frozen_time) + 3600})
    assert token_minutes_remaining(token) == 60


def test_token_minutes_remaining_past(frozen_time, make_jwt):
    token = make_jwt({'exp': int(frozen_time) - 60})
    remaining = token_minutes_remaining(token)
    assert remaining is not None
    assert remaining <= 0


def test_token_minutes_remaining_missing_exp(frozen_time, make_jwt):
    """Regression anchor: missing `exp` defaults to 0, which becomes a
    large negative number relative to now - NOT a KeyError."""
    token = make_jwt({'aud': 'x'})
    remaining = token_minutes_remaining(token)
    assert remaining is not None
    assert remaining < 0


@pytest.mark.parametrize('bad', [
    '',
    'not-a-jwt',
    'a.b',                  # only two segments
    'not.base64!!!.sig',    # middle segment not base64
])
def test_token_minutes_remaining_handles_malformed(bad):
    assert token_minutes_remaining(bad) is None


def test_decode_jwt_formats_header_and_payload(make_jwt):
    token = make_jwt({'exp': 123, 'scp': 'Mail.Read'})
    out = decode_jwt(token)
    assert '=== Header ===' in out
    assert '=== Payload ===' in out
    assert 'Mail.Read' in out
    assert 'RS256' in out


def test_decode_jwt_handles_malformed_middle(capsys):
    out = decode_jwt('valid.not_base64!!!.sig')
    # Header segment is malformed too, but let's at least not traceback.
    assert 'Error decoding' in capsys.readouterr().err
    assert isinstance(out, str)
