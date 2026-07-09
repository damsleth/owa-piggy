"""Tests for exchange_fresh's provider dispatch (msal vs google)."""
from owa_piggy import token_flow


def test_exchange_fresh_msal_path_unaffected(monkeypatch):
    monkeypatch.setattr(
        token_flow, 'exchange_token',
        lambda rt, tid, cid, scope, **kw: {'access_token': 'AT'},
    )
    config = {'OWA_REFRESH_TOKEN': '1.AQ', 'OWA_TENANT_ID': 'tid'}

    result, info = token_flow.exchange_fresh(config, 'scope', persist=False)

    assert result == {'access_token': 'AT'}
    assert info['tid_present'] is True
    assert info['rt_shape_ok'] is True


def test_exchange_fresh_google_skips_tenant_and_foci_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        token_flow, 'google_exchange_token',
        lambda cid, secret, rt: calls.append((cid, secret, rt)) or {'access_token': 'GAT'},
    )
    config = {
        'OWA_PROVIDER': 'google',
        'OWA_REFRESH_TOKEN': 'opaque-google-rt',
        'OWA_CLIENT_ID': 'gcid',
        'OWA_CLIENT_SECRET': 'gsecret',
    }

    result, info = token_flow.exchange_fresh(config, 'ignored-scope', persist=False)

    assert result == {'access_token': 'GAT'}
    assert calls == [('gcid', 'gsecret', 'opaque-google-rt')]
    assert info['tid_present'] is True
    assert info['rt_shape_ok'] is True


def test_exchange_fresh_google_missing_rt_short_circuits(monkeypatch):
    called = []
    monkeypatch.setattr(
        token_flow, 'google_exchange_token',
        lambda *a: called.append(a),
    )
    config = {'OWA_PROVIDER': 'google', 'OWA_CLIENT_ID': 'gcid'}

    result, info = token_flow.exchange_fresh(config, 'scope', persist=False)

    assert result is None
    assert called == []
    assert info['rt_present'] is False


def test_exchange_fresh_google_failure_has_no_aad_error(monkeypatch):
    monkeypatch.setattr(token_flow, 'google_exchange_token', lambda *a: None)
    config = {
        'OWA_PROVIDER': 'google',
        'OWA_REFRESH_TOKEN': 'rt',
        'OWA_CLIENT_ID': 'gcid',
    }

    result, info = token_flow.exchange_fresh(config, 'scope', persist=False)

    assert result is None
    assert info['aad_error'] is None
