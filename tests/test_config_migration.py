"""Edge branches of the one-shot pre-1.3 migration (`config` KV +
`clients.json` -> `config.json`). The happy path lives in test_config.py.
Delete this file together with the migration."""

from __future__ import annotations

import json

from owa_piggy import config as config_mod
from owa_piggy.config import load_config


def test_legacy_doc_is_none_without_legacy_files(tmp_path):
    assert config_mod._legacy_doc(tmp_path) is None


def test_migrates_clients_json_alone(tmp_config, clean_env):
    """No `config` KV: settings stay empty, non-object entries are dropped,
    and only clients.json is renamed to .v1.bak."""
    d = tmp_config.parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "clients.json").write_text(
        json.dumps({"halo": {"refresh_token": "fake-rt-for-tests"}, "junk": "not-an-object"})
    )
    load_config()
    doc = json.loads(tmp_config.read_text())
    assert doc["settings"] == {}
    assert [c["name"] for c in doc["clients"]] == ["halo"]
    assert sorted(p.name for p in d.iterdir() if p.name.endswith(".v1.bak")) == [
        "clients.json.v1.bak"
    ]


def test_migrates_config_kv_alone(tmp_config, clean_env):
    d = tmp_config.parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "config").write_text("OWA_REFRESH_TOKEN='fake-rt-for-tests'\n")
    cfg, _ = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "fake-rt-for-tests"
    assert json.loads(tmp_config.read_text())["clients"] == []
