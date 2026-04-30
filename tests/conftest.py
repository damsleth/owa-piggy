"""Shared fixtures for the owa-piggy test suite.

No network. No real tokens. No writes outside tmp_path.
"""
import base64
import json

import pytest


def _b64url(obj):
    """Encode a dict as unpadded base64url-JSON (how JWT segments are stored)."""
    raw = json.dumps(obj, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


@pytest.fixture
def make_jwt():
    """Build a synthetic three-segment JWT. The signature segment is a
    placeholder; owa-piggy never validates it."""
    def _make(payload, header=None, signature='sig'):
        h = header if header is not None else {'alg': 'RS256', 'typ': 'JWT'}
        return f'{_b64url(h)}.{_b64url(payload)}.{signature}'
    return _make


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect the owa-piggy config tree to a path under tmp_path.

    Patches both `ROOT_DIR` (profiles/profiles.conf resolve through it)
    and `CONFIG_PATH` (active-profile legacy-compat path). Returns the
    legacy-style config path so tests that call `save_config(...)`
    directly still write to the expected location - migration then
    relocates it under `profiles/default/` when main() is invoked,
    matching production behavior.
    """
    fake_root = tmp_path / 'owa-piggy'
    fake_path = fake_root / 'config'
    from owa_piggy import config as config_mod
    monkeypatch.setattr(config_mod, 'ROOT_DIR', fake_root)
    monkeypatch.setattr(config_mod, 'CONFIG_PATH', fake_path)
    # setup.py re-exports CONFIG_PATH at module load time, so patch there too.
    from owa_piggy import setup as setup_mod
    monkeypatch.setattr(setup_mod, 'CONFIG_PATH', fake_path, raising=False)
    return fake_path


@pytest.fixture
def clean_env(monkeypatch):
    """Strip any OWA_* env vars so tests start from a known state."""
    for key in ('OWA_REFRESH_TOKEN', 'OWA_TENANT_ID', 'OWA_CLIENT_ID',
                'OWA_DEFAULT_AUDIENCE', 'OWA_RESEED_SCRIPT', 'OWA_PROFILE'):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def frozen_time(monkeypatch):
    """Pin time.time() inside owa_piggy.jwt (used by token_minutes_remaining)
    so remaining-minute assertions are deterministic. Does NOT freeze the
    cache module's time - tests that care about cache-hit thresholds should
    use real-time offsets (now + 3600, etc.)."""
    fixed = 1_700_000_000.0  # 2023-11-14T22:13:20Z
    import owa_piggy.jwt as jwt_mod
    monkeypatch.setattr(jwt_mod.time, 'time', lambda: fixed)
    return fixed
