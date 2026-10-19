"""Companion CLI offer after setup: install like owa-piggy was installed, and
never prompt when no human is at the terminal (agents call setup too)."""

import builtins
import io

from owa_piggy import setup


def test_install_cmd_follows_how_owa_piggy_was_installed():
    cmd = setup.companion_install_cmd
    assert cmd("owa-tools", "/opt/homebrew/Cellar/owa-piggy/1.5.0/libexec") == [
        "brew",
        "install",
        "damsleth/tap/owa-tools",
    ]
    assert cmd("owa-tui", "/Users/x/.local/pipx/venvs/owa-piggy") == ["pipx", "install", "owa-tui"]
    assert cmd("owa-tools", "/Users/x/.local/share/uv/tools/owa-piggy") == [
        "uv",
        "tool",
        "install",
        "owa-tools",
    ]


def test_no_prompt_without_a_tty(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("prompted or installed without a TTY")

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr(builtins, "input", boom)
    monkeypatch.setattr(setup.subprocess, "call", boom)
    monkeypatch.setattr(setup.shutil, "which", lambda _b: None)
    setup.offer_companions()


def _human_at_terminal(monkeypatch):
    for stream in (setup.sys.stdin, setup.sys.stdout, setup.sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True, raising=False)


def test_offers_only_missing_companions(monkeypatch):
    calls, prompts = [], []
    _human_at_terminal(monkeypatch)
    monkeypatch.delenv("OWA_PIGGY_NO_COMPANIONS", raising=False)
    monkeypatch.setattr(
        setup.shutil, "which", lambda b: "/bin/uv" if b in ("uv", "owa-tui") else None
    )
    monkeypatch.setattr(builtins, "input", lambda p="": prompts.append(p) or "")
    monkeypatch.setattr(setup.subprocess, "call", lambda c: calls.append(c) or 0)
    monkeypatch.setattr(setup.sys, "prefix", "/somewhere/dev-venv")
    setup.offer_companions()
    assert len(prompts) == 1 and "owa-tools" in prompts[0]  # owa-tui already on PATH
    assert calls == [["uv", "tool", "install", "owa-tools"]]  # Enter takes the Y default


def test_first_run_offer_is_made_once(monkeypatch):
    prompts = []
    _human_at_terminal(monkeypatch)
    monkeypatch.delenv("OWA_PIGGY_NO_COMPANIONS", raising=False)
    monkeypatch.setattr(setup.shutil, "which", lambda b: "/bin/uv" if b == "uv" else None)
    monkeypatch.setattr(builtins, "input", lambda p="": prompts.append(p) or "n")
    setup.offer_companions(once=True)
    setup.offer_companions(once=True)
    assert len(prompts) == 2  # owa-tools + owa-tui on the first run, nothing after


def test_piped_stdout_never_prompts(monkeypatch):
    """`owa-tools` runs `owa-piggy token` with the user's stdin but piped output."""
    _human_at_terminal(monkeypatch)
    monkeypatch.setattr(setup.sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(
        builtins, "input", lambda *_: (_ for _ in ()).throw(AssertionError("prompted"))
    )
    monkeypatch.setattr(setup.shutil, "which", lambda _b: None)
    setup.offer_companions(once=True)
