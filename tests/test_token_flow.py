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


def test_rotated_refresh_token_is_written_to_the_named_profile(
    monkeypatch, tmp_path, clean_env
):
    """Every `token` call rotates the RT; if the new one never reaches disk
    the profile is dead within the day. Nothing covered that write."""
    from owa_piggy.config import load_config

    other = tmp_path / 'other' / 'config'
    other.parent.mkdir(parents=True)
    other.write_text('OWA_REFRESH_TOKEN="1.OLD"\nOWA_TENANT_ID="tid"\n')
    monkeypatch.setattr(
        token_flow, 'exchange_token',
        lambda *a, **kw: {'access_token': 'AT', 'refresh_token': '1.NEW'},
    )
    config = {'OWA_REFRESH_TOKEN': '1.OLD', 'OWA_TENANT_ID': 'tid'}

    _, info = token_flow.exchange_fresh(config, 'scope', persist=True,
                                        config_path=other)

    assert info['rotated'] is True
    assert config['OWA_REFRESH_TOKEN'] == '1.NEW'
    assert load_config(other)[0]['OWA_REFRESH_TOKEN'] == '1.NEW'


def test_no_persist_leaves_the_config_file_untouched(
    monkeypatch, tmp_path, clean_env
):
    """`status` probes with persist=False and must not write - a probe that
    rotates the on-disk RT would invalidate the token the caller still holds."""
    path = tmp_path / 'config'
    path.write_text('OWA_REFRESH_TOKEN="1.OLD"\nOWA_TENANT_ID="tid"\n')
    monkeypatch.setattr(
        token_flow, 'exchange_token',
        lambda *a, **kw: {'access_token': 'AT', 'refresh_token': '1.NEW'},
    )
    config = {'OWA_REFRESH_TOKEN': '1.OLD', 'OWA_TENANT_ID': 'tid'}

    _, info = token_flow.exchange_fresh(config, 'scope', persist=False,
                                        config_path=path)

    assert info['rotated'] is True
    assert '1.OLD' in path.read_text()
