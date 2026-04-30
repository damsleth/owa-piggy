"""Tests for reseed helpers."""

from owa_piggy import capture as capture_mod
from owa_piggy import reseed as reseed_mod


def test_capture_reseed_clears_cache(monkeypatch, tmp_config, clean_env):
    """Capture-mode reseed must clear the per-profile AT cache even when
    called directly (the --all path bypasses cli._cmd_reseed's pre-clear)."""
    from owa_piggy.config import set_active_profile

    set_active_profile('work')
    cleared = []
    saved = {}

    monkeypatch.setattr(reseed_mod, 'clear_cache', lambda: cleared.append(True))
    monkeypatch.setattr(
        capture_mod,
        'capture_silent',
        lambda alias: ('ok', {
            'OWA_REFRESH_TOKEN': '1.AQ_fake-rotated',
            'OWA_TENANT_ID': 'tid-1',
        }),
    )
    monkeypatch.setattr(reseed_mod, 'iso_utc_now', lambda: '2026-04-30T12:00:00Z')
    monkeypatch.setattr(reseed_mod, 'save_config', lambda config: saved.update(config))

    rc = reseed_mod._do_reseed_capture('work', {'OWA_AUTH_MODE': 'capture'})
    assert rc == 0
    assert cleared == [True]
    assert saved['OWA_REFRESH_TOKEN'] == '1.AQ_fake-rotated'
    assert saved['OWA_TENANT_ID'] == 'tid-1'
    assert saved['OWA_RT_ISSUED_AT'] == '2026-04-30T12:00:00Z'
