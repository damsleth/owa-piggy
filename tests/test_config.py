"""Tests for config file I/O and parse_kv_stream.

Uses the `tmp_config` fixture which patches CONFIG_PATH into tmp_path
across the package.
"""

import os
import stat

import pytest

from owa_piggy import config as config_mod
from owa_piggy.config import load_config, parse_kv_stream, save_config


def test_parse_kv_stream_basic():
    out = parse_kv_stream("OWA_REFRESH_TOKEN=abc\nOWA_TENANT_ID=xyz\n")
    assert out == {"OWA_REFRESH_TOKEN": "abc", "OWA_TENANT_ID": "xyz"}


def test_parse_kv_stream_ignores_blank_and_comments():
    text = """
# a comment
OWA_REFRESH_TOKEN=rt

   # indented comment is kept-as-comment path? currently stripped:
OWA_TENANT_ID=tid
"""
    out = parse_kv_stream(text)
    assert out == {"OWA_REFRESH_TOKEN": "rt", "OWA_TENANT_ID": "tid"}


def test_parse_kv_stream_strips_quotes():
    out = parse_kv_stream("OWA_REFRESH_TOKEN=\"quoted\"\nOWA_TENANT_ID='single'\n")
    assert out == {"OWA_REFRESH_TOKEN": "quoted", "OWA_TENANT_ID": "single"}


def test_parse_kv_stream_value_with_equals():
    out = parse_kv_stream("OWA_REFRESH_TOKEN=abc=def=ghi\n")
    # split on first `=` only
    assert out == {"OWA_REFRESH_TOKEN": "abc=def=ghi"}


def test_parse_kv_stream_crlf():
    out = parse_kv_stream("OWA_REFRESH_TOKEN=abc\r\nOWA_TENANT_ID=xyz\r\n")
    assert out == {"OWA_REFRESH_TOKEN": "abc", "OWA_TENANT_ID": "xyz"}


def test_parse_kv_stream_rejects_unknown_keys():
    out = parse_kv_stream("EVIL=1\nOWA_REFRESH_TOKEN=ok\n")
    assert out == {"OWA_REFRESH_TOKEN": "ok"}


def test_parse_kv_stream_drops_empty_values():
    out = parse_kv_stream("OWA_REFRESH_TOKEN=\nOWA_TENANT_ID=t\n")
    assert out == {"OWA_TENANT_ID": "t"}


def test_load_config_missing_file(tmp_config, clean_env):
    assert not tmp_config.exists()
    cfg, persist = load_config()
    assert cfg == {}
    assert persist is False


def test_save_and_load_round_trip(tmp_config, clean_env):
    save_config({"OWA_REFRESH_TOKEN": "fake-rt-for-tests", "OWA_TENANT_ID": "tid-1"})
    cfg, persist = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "fake-rt-for-tests"
    assert cfg["OWA_TENANT_ID"] == "tid-1"
    assert persist is True


def test_load_config_strips_single_quotes(tmp_config, clean_env):
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.write_text("OWA_REFRESH_TOKEN='fake-rt-for-tests'\nOWA_TENANT_ID='tid-1'\n")
    cfg, persist = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "fake-rt-for-tests"
    assert cfg["OWA_TENANT_ID"] == "tid-1"
    assert persist is True


def test_save_sets_0600_permissions(tmp_config, clean_env):
    save_config({"OWA_REFRESH_TOKEN": "x", "OWA_TENANT_ID": "y"})
    mode = stat.S_IMODE(tmp_config.stat().st_mode)
    assert mode == 0o600


def test_env_overrides_file(tmp_config, monkeypatch, clean_env):
    save_config({"OWA_REFRESH_TOKEN": "from-file", "OWA_TENANT_ID": "tid"})
    monkeypatch.setenv("OWA_REFRESH_TOKEN", "from-env")
    cfg, persist = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "from-env"


def test_env_only_does_not_persist(tmp_config, monkeypatch, clean_env):
    """Regression anchor for commit c07a9ec: env-provided refresh token
    must NOT mark persist=True, even when the config file also has the
    key. Otherwise rotated env-tokens silently clobber the on-disk value."""
    save_config({"OWA_REFRESH_TOKEN": "from-file", "OWA_TENANT_ID": "tid"})
    monkeypatch.setenv("OWA_REFRESH_TOKEN", "from-env")
    _cfg, persist = load_config()
    assert persist is False


def test_file_only_persists(tmp_config, clean_env):
    save_config({"OWA_REFRESH_TOKEN": "rt", "OWA_TENANT_ID": "tid"})
    _cfg, persist = load_config()
    assert persist is True


def test_env_only_no_file_does_not_persist(tmp_config, monkeypatch, clean_env):
    monkeypatch.setenv("OWA_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("OWA_TENANT_ID", "tid")
    cfg, persist = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "rt"
    assert persist is False


def test_save_creates_parent_dir(tmp_path, monkeypatch, clean_env):
    fake = tmp_path / "deep" / "nested" / "config"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", fake)
    save_config({"OWA_REFRESH_TOKEN": "x", "OWA_TENANT_ID": "y"})
    assert fake.exists()
    # Parent mkdir uses mode=0o700 (exist_ok=True suppresses chmod on existing).
    parent_mode = stat.S_IMODE(fake.parent.stat().st_mode)
    assert parent_mode & 0o077 == 0


def test_permission_audit_reports_open_known_paths(tmp_config, clean_env):
    from owa_piggy.config import (
        audit_private_permissions,
        profile_dir,
        profile_edge_dir,
    )

    profile_dir("work").mkdir(parents=True)
    profile_edge_dir("work").mkdir()
    (profile_dir("work") / "config").write_text("OWA_REFRESH_TOKEN=x\n")
    profile_dir("work").chmod(0o755)
    profile_edge_dir("work").chmod(0o755)

    findings = audit_private_permissions()

    labels = {f["label"] for f in findings}
    assert "profile work directory" in labels
    assert "profile work Edge sidecar directory" in labels


def test_repair_private_permissions_chmods_known_paths(tmp_config, clean_env):
    from owa_piggy.config import profile_dir, repair_private_permissions

    profile_dir("work").mkdir(parents=True)
    cfg = profile_dir("work") / "config"
    cfg.write_text("OWA_REFRESH_TOKEN=x\n")
    profile_dir("work").chmod(0o755)
    cfg.chmod(0o644)

    repaired = repair_private_permissions()

    assert any(r["label"] == "profile work directory" for r in repaired)
    assert stat.S_IMODE(profile_dir("work").stat().st_mode) == 0o700
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_save_atomic_no_stray_tmpfile(tmp_config, clean_env):
    save_config({"OWA_REFRESH_TOKEN": "x", "OWA_TENANT_ID": "y"})
    siblings = list(tmp_config.parent.iterdir())
    # Only the final config file; no leftover `.config.*.tmp` shrapnel.
    assert [p.name for p in siblings] == [tmp_config.name]


def test_iso_utc_now_format():
    from owa_piggy.config import iso_utc_now

    value = iso_utc_now()
    # Shape: 2026-04-19T10:15:00Z
    assert len(value) == 20
    assert value.endswith("Z")
    assert value[4] == "-" and value[7] == "-" and value[10] == "T"


# --- atomic_write crash-safety ----------------------------------------


def test_atomic_write_failed_replace_leaves_target_intact(tmp_path, monkeypatch):
    """If os.replace dies mid-write, the original file must survive
    byte-for-byte and no `.<name>.*.tmp` shrapnel may be left behind.
    Covers the except/unlink branch in atomic_write."""
    from owa_piggy.config import atomic_write

    target = tmp_path / "config"
    original = 'OWA_REFRESH_TOKEN="1.FAKE-rt"\n'
    target.write_text(original)

    def boom(*_args, **_kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(config_mod.os, "replace", boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        atomic_write(target, 'OWA_REFRESH_TOKEN="2.FAKE-rt-rotated"\n')

    # Target untouched.
    assert target.read_text() == original
    # No leftover temp shrapnel.
    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name.startswith(".config.") and p.name.endswith(".tmp")
    ]
    assert leftovers == []


# --- umask defence -----------------------------------------------------


def test_save_config_mode_survives_loose_umask(tmp_config, clean_env):
    """A loose process umask must not loosen the config file mode: chmod
    runs before any payload is written, so the result is exactly 0o600."""
    old_umask = os.umask(0o022)
    try:
        save_config({"OWA_REFRESH_TOKEN": "1.FAKE-rt", "OWA_TENANT_ID": "fake-tenant"})
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(tmp_config.stat().st_mode) == 0o600


# --- validate_alias table ----------------------------------------------


@pytest.mark.parametrize(
    "alias, expected_ok",
    [
        (".", False),
        ("..", False),
        ("../x", False),
        ("a/b", False),
        ("", False),
        ("café", False),
        ("work", True),
        ("a.b-c_d", True),
    ],
)
def test_validate_alias_table(alias, expected_ok):
    from owa_piggy.config import validate_alias

    ok, err = validate_alias(alias)
    assert ok is expected_ok
    if not ok:
        assert err  # rejected aliases carry a message


@pytest.mark.parametrize("alias", ["work", "a.b-c_d"])
def test_accepted_alias_cannot_escape_root_dir(tmp_config, alias):
    """No accepted alias, joined into profile_dir, may resolve outside
    ROOT_DIR - the validation is what keeps the path tree sealed."""
    from owa_piggy.config import profile_dir, validate_alias

    ok, _ = validate_alias(alias)
    assert ok
    resolved = profile_dir(alias).resolve()
    root = config_mod.ROOT_DIR.resolve()
    assert resolved.is_relative_to(root)


# --- load_config persist matrix ----------------------------------------


def test_load_config_file_only_persists(tmp_config, clean_env):
    save_config({"OWA_REFRESH_TOKEN": "1.FAKE-rt-file", "OWA_TENANT_ID": "fake-tenant"})
    cfg, persist = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "1.FAKE-rt-file"
    assert persist is True


def test_load_config_env_only_does_not_persist(tmp_config, monkeypatch, clean_env):
    monkeypatch.setenv("OWA_REFRESH_TOKEN", "1.FAKE-rt-env")
    cfg, persist = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "1.FAKE-rt-env"
    assert persist is False


def test_load_config_both_set_env_wins_no_persist(tmp_config, monkeypatch, clean_env):
    save_config({"OWA_REFRESH_TOKEN": "1.FAKE-rt-file", "OWA_TENANT_ID": "fake-tenant"})
    monkeypatch.setenv("OWA_REFRESH_TOKEN", "1.FAKE-rt-env")
    cfg, persist = load_config()
    assert cfg["OWA_REFRESH_TOKEN"] == "1.FAKE-rt-env"
    assert persist is False


def test_persist_flag_drives_writeback(tmp_config, clean_env):
    """The persist flag is the signal callers use to decide whether a
    rotated token gets written back. File-only -> True means the rotated
    token is saved; env-driven -> False means we must NOT clobber disk.
    Exercised directly via save_config gated on persist."""
    # File-only case: persist True, rotated token written back.
    save_config({"OWA_REFRESH_TOKEN": "1.FAKE-rt-old", "OWA_TENANT_ID": "fake-tenant"})
    cfg, persist = load_config()
    assert persist is True
    if persist:
        cfg["OWA_REFRESH_TOKEN"] = "1.FAKE-rt-new"
        save_config(cfg)
    cfg2, _ = load_config()
    assert cfg2["OWA_REFRESH_TOKEN"] == "1.FAKE-rt-new"


# --- parse_kv_stream drops unknown keys --------------------------------


def test_parse_kv_stream_keeps_only_known_nonempty():
    text = (
        "OWA_REFRESH_TOKEN=1.FAKE-rt\n"
        "OWA_EMAIL=user@example.com\n"
        "JUNK_KEY=garbage\n"
        "OWA_TENANT_ID=\n"  # known but empty -> dropped
        "ANOTHER=thing\n"
    )
    out = parse_kv_stream(text)
    assert out == {"OWA_REFRESH_TOKEN": "1.FAKE-rt", "OWA_EMAIL": "user@example.com"}


# --- save_config preserves comments / unknown lines --------------------


def test_save_config_preserves_comments_and_unknown_lines(tmp_config, clean_env):
    """Rewriting one known key must leave comments and unrecognized lines
    intact, and update the known key in place (covers the line-preserve
    branch)."""
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.write_text('# a hand-written comment\nFOO=bar\nOWA_TENANT_ID="old-tenant"\n')
    save_config({"OWA_TENANT_ID": "fake-tenant"})
    text = tmp_config.read_text()
    assert "# a hand-written comment" in text
    assert "FOO=bar" in text
    assert 'OWA_TENANT_ID="fake-tenant"' in text
    assert "old-tenant" not in text


def test_save_config_appends_new_keys(tmp_config, clean_env):
    """A key not already present in the file is appended (covers the
    new-key-append branch)."""
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.write_text('OWA_TENANT_ID="fake-tenant"\n')
    save_config({"OWA_TENANT_ID": "fake-tenant", "OWA_REFRESH_TOKEN": "1.FAKE-rt"})
    cfg, _ = load_config()
    assert cfg["OWA_TENANT_ID"] == "fake-tenant"
    assert cfg["OWA_REFRESH_TOKEN"] == "1.FAKE-rt"


# --- remaining registry / resolve branches -----------------------------


def test_load_profiles_conf_ignores_unknown_keys(tmp_config):
    """An unrecognized key in profiles.conf is skipped (forward-compat),
    covering the loop's no-match continue branch."""
    from owa_piggy.config import load_profiles_conf, profiles_conf_path

    path = profiles_conf_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'OWA_DEFAULT_PROFILE="work"\nOWA_PROFILES="work home"\nOWA_FUTURE_KEY="ignored"\n'
    )
    reg = load_profiles_conf()
    assert reg["OWA_DEFAULT_PROFILE"] == "work"
    assert reg["OWA_PROFILES"] == ["work", "home"]
    assert reg["OWA_SCHEDULED"] == []


def test_save_profiles_conf_accepts_string_values(tmp_config):
    """OWA_PROFILES / OWA_SCHEDULED passed as space-joined strings are
    split into lists on write (covers both isinstance-str branches)."""
    from owa_piggy.config import load_profiles_conf, save_profiles_conf

    save_profiles_conf(
        {
            "OWA_DEFAULT_PROFILE": "work",
            "OWA_PROFILES": "work home",
            "OWA_SCHEDULED": "work",
        }
    )
    reg = load_profiles_conf()
    assert reg["OWA_PROFILES"] == ["work", "home"]
    assert reg["OWA_SCHEDULED"] == ["work"]


def test_schedule_profile_rejects_invalid_alias(tmp_config):
    """schedule_profile validates the alias and raises on a bad one."""
    from owa_piggy.config import schedule_profile

    with pytest.raises(ValueError):
        schedule_profile("../escape")


def test_resolve_profile_env_not_found(tmp_config, monkeypatch, clean_env):
    """OWA_PROFILE pointing at a non-existent profile, with other profiles
    present, returns an error (covers the env-not-found branch)."""
    from owa_piggy.config import profile_dir, resolve_profile

    profile_dir("work").mkdir(parents=True)
    monkeypatch.setenv("OWA_PROFILE", "ghost")
    alias, err = resolve_profile()
    assert alias == ""
    assert "ghost" in err


def test_repair_private_permissions_skips_correct_mode(tmp_config, clean_env):
    """A path that already has the correct mode is not re-chmodded and not
    reported (covers the actual == expected continue branch)."""
    from owa_piggy.config import profile_dir, repair_private_permissions

    profile_dir("work").mkdir(parents=True)
    cfg = profile_dir("work") / "config"
    cfg.write_text("OWA_REFRESH_TOKEN=1.FAKE-rt\n")
    # Set everything to its expected private mode up front.
    config_mod.ROOT_DIR.chmod(0o700)
    config_mod.profiles_dir().chmod(0o700)
    profile_dir("work").chmod(0o700)
    cfg.chmod(0o600)

    repaired = repair_private_permissions()
    assert all(r["label"] != "profile work config" for r in repaired)
    assert all(r["label"] != "profile work directory" for r in repaired)
