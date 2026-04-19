"""Tests for config file I/O and parse_kv_stream.

Uses the `tmp_config` fixture which patches CONFIG_PATH into tmp_path
across the package.
"""
import stat

from owa_piggy import config as config_mod
from owa_piggy.config import load_config, parse_kv_stream, save_config


def test_parse_kv_stream_basic():
    out = parse_kv_stream('OWA_REFRESH_TOKEN=abc\nOWA_TENANT_ID=xyz\n')
    assert out == {'OWA_REFRESH_TOKEN': 'abc', 'OWA_TENANT_ID': 'xyz'}


def test_parse_kv_stream_ignores_blank_and_comments():
    text = """
# a comment
OWA_REFRESH_TOKEN=rt

   # indented comment is kept-as-comment path? currently stripped:
OWA_TENANT_ID=tid
"""
    out = parse_kv_stream(text)
    assert out == {'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'}


def test_parse_kv_stream_strips_quotes():
    out = parse_kv_stream('OWA_REFRESH_TOKEN="quoted"\nOWA_TENANT_ID=\'single\'\n')
    assert out == {'OWA_REFRESH_TOKEN': 'quoted', 'OWA_TENANT_ID': 'single'}


def test_parse_kv_stream_value_with_equals():
    out = parse_kv_stream('OWA_REFRESH_TOKEN=abc=def=ghi\n')
    # split on first `=` only
    assert out == {'OWA_REFRESH_TOKEN': 'abc=def=ghi'}


def test_parse_kv_stream_crlf():
    out = parse_kv_stream('OWA_REFRESH_TOKEN=abc\r\nOWA_TENANT_ID=xyz\r\n')
    assert out == {'OWA_REFRESH_TOKEN': 'abc', 'OWA_TENANT_ID': 'xyz'}


def test_parse_kv_stream_rejects_unknown_keys():
    out = parse_kv_stream('EVIL=1\nOWA_REFRESH_TOKEN=ok\n')
    assert out == {'OWA_REFRESH_TOKEN': 'ok'}


def test_parse_kv_stream_drops_empty_values():
    out = parse_kv_stream('OWA_REFRESH_TOKEN=\nOWA_TENANT_ID=t\n')
    assert out == {'OWA_TENANT_ID': 't'}


def test_load_config_missing_file(tmp_config, clean_env):
    assert not tmp_config.exists()
    cfg, persist = load_config()
    assert cfg == {}
    assert persist is False


def test_save_and_load_round_trip(tmp_config, clean_env):
    save_config({'OWA_REFRESH_TOKEN': 'fake-rt-for-tests', 'OWA_TENANT_ID': 'tid-1'})
    cfg, persist = load_config()
    assert cfg['OWA_REFRESH_TOKEN'] == 'fake-rt-for-tests'
    assert cfg['OWA_TENANT_ID'] == 'tid-1'
    assert persist is True


def test_save_sets_0600_permissions(tmp_config, clean_env):
    save_config({'OWA_REFRESH_TOKEN': 'x', 'OWA_TENANT_ID': 'y'})
    mode = stat.S_IMODE(tmp_config.stat().st_mode)
    assert mode == 0o600


def test_env_overrides_file(tmp_config, monkeypatch, clean_env):
    save_config({'OWA_REFRESH_TOKEN': 'from-file', 'OWA_TENANT_ID': 'tid'})
    monkeypatch.setenv('OWA_REFRESH_TOKEN', 'from-env')
    cfg, persist = load_config()
    assert cfg['OWA_REFRESH_TOKEN'] == 'from-env'


def test_env_only_does_not_persist(tmp_config, monkeypatch, clean_env):
    """Regression anchor for commit c07a9ec: env-provided refresh token
    must NOT mark persist=True, even when the config file also has the
    key. Otherwise rotated env-tokens silently clobber the on-disk value."""
    save_config({'OWA_REFRESH_TOKEN': 'from-file', 'OWA_TENANT_ID': 'tid'})
    monkeypatch.setenv('OWA_REFRESH_TOKEN', 'from-env')
    _cfg, persist = load_config()
    assert persist is False


def test_file_only_persists(tmp_config, clean_env):
    save_config({'OWA_REFRESH_TOKEN': 'rt', 'OWA_TENANT_ID': 'tid'})
    _cfg, persist = load_config()
    assert persist is True


def test_env_only_no_file_does_not_persist(tmp_config, monkeypatch, clean_env):
    monkeypatch.setenv('OWA_REFRESH_TOKEN', 'rt')
    monkeypatch.setenv('OWA_TENANT_ID', 'tid')
    cfg, persist = load_config()
    assert cfg['OWA_REFRESH_TOKEN'] == 'rt'
    assert persist is False


def test_save_creates_parent_dir(tmp_path, monkeypatch, clean_env):
    fake = tmp_path / 'deep' / 'nested' / 'config'
    monkeypatch.setattr(config_mod, 'CONFIG_PATH', fake)
    save_config({'OWA_REFRESH_TOKEN': 'x', 'OWA_TENANT_ID': 'y'})
    assert fake.exists()
    # Parent mkdir uses mode=0o700 (exist_ok=True suppresses chmod on existing).
    parent_mode = stat.S_IMODE(fake.parent.stat().st_mode)
    assert parent_mode & 0o077 == 0


def test_save_atomic_no_stray_tmpfile(tmp_config, clean_env):
    save_config({'OWA_REFRESH_TOKEN': 'x', 'OWA_TENANT_ID': 'y'})
    siblings = list(tmp_config.parent.iterdir())
    # Only the final config file; no leftover `.config.*.tmp` shrapnel.
    assert [p.name for p in siblings] == [tmp_config.name]


def test_iso_utc_now_format():
    from owa_piggy.config import iso_utc_now
    value = iso_utc_now()
    # Shape: 2026-04-19T10:15:00Z
    assert len(value) == 20
    assert value.endswith('Z')
    assert value[4] == '-' and value[7] == '-' and value[10] == 'T'
