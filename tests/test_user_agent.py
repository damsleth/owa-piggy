"""UA spoofing — flag plumbing from CLI → setup → capture → reseed."""

from owa_piggy import capture
from owa_piggy import reseed as reseed_mod
from owa_piggy import setup as setup_mod
from owa_piggy.capture import launch_edge


def test_launch_edge_appends_user_agent_flag(monkeypatch, tmp_path):
    seen = {}

    class _FakeProc:
        pass

    def _fake_popen(args, **kwargs):
        seen["args"] = args
        return _FakeProc()

    monkeypatch.setattr(capture, "find_edge", lambda: "/usr/bin/edge")
    monkeypatch.setattr(capture.subprocess, "Popen", _fake_popen)
    launch_edge(tmp_path, 9999, headless=False, url="https://x", user_agent="UA/Spoof-1.0")
    ua_flag = "--user-agent=UA/Spoof-1.0"
    assert ua_flag in seen["args"]
    # UA flag must come before the URL positional so Edge sees it on first nav.
    assert seen["args"].index(ua_flag) < seen["args"].index("https://x")


def test_launch_edge_omits_ua_flag_when_unset(monkeypatch, tmp_path):
    seen = {}

    def _fake_popen(args, **kwargs):
        seen["args"] = args

        class _P:
            pass

        return _P()

    monkeypatch.setattr(capture, "find_edge", lambda: "/usr/bin/edge")
    monkeypatch.setattr(capture.subprocess, "Popen", _fake_popen)
    launch_edge(tmp_path, 9999, headless=True, url="https://x")
    assert not any(a.startswith("--user-agent=") for a in seen["args"])


def test_interactive_setup_persists_user_agent_through_paste(tmp_config, monkeypatch):
    """Paste/stdin path: --user-agent saves OWA_USER_AGENT alongside RT+TID."""
    import io
    import sys

    monkeypatch.setattr(
        sys, "stdin", io.StringIO("OWA_REFRESH_TOKEN=1.AQ_pasted\nOWA_TENANT_ID=tid-paste\n")
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    cfg = {}
    ok = setup_mod.interactive_setup(cfg, alias="paste-ua", user_agent="UA/Paste-1.0")
    assert ok is True
    assert cfg["OWA_USER_AGENT"] == "UA/Paste-1.0"
    assert cfg["OWA_REFRESH_TOKEN"] == "1.AQ_pasted"


def test_reseed_capture_passes_persisted_ua_to_capture(monkeypatch, tmp_config):
    """Per-profile OWA_USER_AGENT flows into capture_silent on reseed."""
    from owa_piggy.config import set_active_profile

    set_active_profile("uap")
    monkeypatch.setattr(reseed_mod, "clear_cache", lambda: None)
    monkeypatch.setattr(reseed_mod, "iso_utc_now", lambda: "2026-05-22T00:00:00Z")
    monkeypatch.setattr(reseed_mod, "save_config", lambda cfg: None)

    seen = {}

    def _fake_silent(alias, *, timeout=None, headless=None, user_agent=None, capture_url=None):
        seen["user_agent"] = user_agent
        seen["capture_url"] = capture_url
        return "ok", {"OWA_REFRESH_TOKEN": "1.AQ_x", "OWA_TENANT_ID": "tid-x"}

    monkeypatch.setattr(capture, "capture_silent", _fake_silent)

    cfg = {"OWA_AUTH_MODE": "capture", "OWA_USER_AGENT": "UA/Persisted"}
    rc = reseed_mod._do_reseed_capture("uap", cfg)
    assert rc == 0
    assert seen["user_agent"] == "UA/Persisted"


def test_reseed_capture_env_overrides_persisted_ua(monkeypatch, tmp_config):
    from owa_piggy.config import set_active_profile

    set_active_profile("uap2")
    monkeypatch.setattr(reseed_mod, "clear_cache", lambda: None)
    monkeypatch.setattr(reseed_mod, "iso_utc_now", lambda: "2026-05-22T00:00:00Z")
    monkeypatch.setattr(reseed_mod, "save_config", lambda cfg: None)
    monkeypatch.setenv("OWA_USER_AGENT", "UA/Env-Wins")

    seen = {}

    def _fake_silent(alias, *, timeout=None, headless=None, user_agent=None, capture_url=None):
        seen["user_agent"] = user_agent
        seen["capture_url"] = capture_url
        return "ok", {"OWA_REFRESH_TOKEN": "1.AQ_x", "OWA_TENANT_ID": "tid-x"}

    monkeypatch.setattr(capture, "capture_silent", _fake_silent)

    cfg = {"OWA_AUTH_MODE": "capture", "OWA_USER_AGENT": "UA/Persisted"}
    rc = reseed_mod._do_reseed_capture("uap2", cfg)
    assert rc == 0
    assert seen["user_agent"] == "UA/Env-Wins"


def test_launch_edge_binds_debugging_to_loopback(monkeypatch, tmp_path):
    seen = {}

    class _P:
        pass

    def _fake_popen(args, **kwargs):
        seen["args"] = args
        return _P()

    monkeypatch.setattr(capture, "find_edge", lambda: "/usr/bin/edge")
    monkeypatch.setattr(capture.subprocess, "Popen", _fake_popen)

    launch_edge(tmp_path, 9999, headless=True, url="https://x")

    assert "--remote-debugging-address=127.0.0.1" in seen["args"]
