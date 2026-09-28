"""Unit tests for the profile dashboard's action helpers and formatters.

The raw-terminal key loop (run_dashboard) needs a real TTY and is not
exercised here - test_cli_smoke covers Ctrl-C restoration and key
dispatch. What IS unit-testable is the dispatch logic in the
_action_* helpers: which registry op a keystroke maps to, the
already-in-that-state short-circuits, and the y/N confirm gate on
delete. The underlying registry ops (enable/disable/set_default/
delete_profile) are tested end-to-end in test_profile_ops; here we
stub them so a test never touches profiles.conf or launchd.
"""

from pathlib import Path

from owa_piggy import profile_tui
from tests.conftest import read_settings, write_doc  # noqa: F401


class FakeState:
    """Stand-in for PickerState. cooked_action just runs the closure -
    there is no terminal to drop in and out of raw mode for."""

    def __init__(self):
        self.idx = 0
        self.message = ""

    def cooked_action(self, fn):
        return fn()


# --- _action_toggle ----------------------------------------------------


def test_toggle_disables_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui, "disable_profile", lambda a: calls.append(("disable", a)))
    monkeypatch.setattr(
        profile_tui, "enable_profile", lambda a: calls.append(("enable", a)) or (True, None)
    )
    msg = profile_tui._action_toggle("work", enabled={"work", "home"})
    assert calls == [("disable", "work")]
    assert msg == "disabled 'work'."


def test_toggle_enables_when_not_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui, "disable_profile", lambda a: calls.append(("disable", a)))
    monkeypatch.setattr(
        profile_tui, "enable_profile", lambda a: calls.append(("enable", a)) or (True, None)
    )
    msg = profile_tui._action_toggle("work", enabled={"home"})
    assert calls == [("enable", "work")]
    assert msg == "enabled 'work'."


def test_toggle_surfaces_enable_failure(monkeypatch):
    monkeypatch.setattr(profile_tui, "enable_profile", lambda a: (False, "boom"))
    msg = profile_tui._action_toggle("work", enabled=set())
    assert msg == "enable failed: boom"


# --- _action_set_default -----------------------------------------------


def test_set_default_short_circuits_when_already_default(monkeypatch):
    called = []
    monkeypatch.setattr(
        profile_tui, "set_default_profile", lambda a: called.append(a) or (True, None)
    )
    msg = profile_tui._action_set_default("work", default="work")
    assert called == []  # no registry write when already default
    assert "already the default" in msg


def test_set_default_delegates_when_changing(monkeypatch):
    called = []
    monkeypatch.setattr(
        profile_tui, "set_default_profile", lambda a: called.append(a) or (True, None)
    )
    msg = profile_tui._action_set_default("work", default="home")
    assert called == ["work"]
    assert msg == "default profile set to 'work'."


def test_set_default_surfaces_failure(monkeypatch):
    monkeypatch.setattr(profile_tui, "set_default_profile", lambda a: (False, "nope"))
    msg = profile_tui._action_set_default("work", default="home")
    assert msg == "set-default failed: nope"


# --- _action_delete ----------------------------------------------------


def test_delete_aborts_when_not_confirmed(monkeypatch):
    deleted = []
    monkeypatch.setattr(profile_tui, "launchd_is_scheduled", lambda a: False)
    monkeypatch.setattr(profile_tui, "profile_dir", lambda a: f"/tmp/{a}")
    monkeypatch.setattr(profile_tui, "_confirm", lambda prompt: False)
    monkeypatch.setattr(
        profile_tui, "delete_profile", lambda *a, **k: deleted.append(a) or (True, None)
    )
    msg = profile_tui._action_delete(FakeState(), "work")
    assert deleted == []  # confirm declined -> never deleted
    assert msg == "delete cancelled."


def test_delete_delegates_when_confirmed(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui, "launchd_is_scheduled", lambda a: False)
    monkeypatch.setattr(profile_tui, "profile_dir", lambda a: f"/tmp/{a}")
    monkeypatch.setattr(profile_tui, "_confirm", lambda prompt: True)

    def fake_delete(alias, *, uninstall_launchd, promote_default):
        calls.append((alias, uninstall_launchd, promote_default))
        return True, None

    monkeypatch.setattr(profile_tui, "delete_profile", fake_delete)

    msg = profile_tui._action_delete(FakeState(), "work")
    assert calls == [("work", True, True)]
    assert msg == "deleted 'work'."


def test_delete_surfaces_failure(monkeypatch, capsys):
    monkeypatch.setattr(profile_tui, "launchd_is_scheduled", lambda a: False)
    monkeypatch.setattr(profile_tui, "profile_dir", lambda a: f"/tmp/{a}")
    monkeypatch.setattr(profile_tui, "_confirm", lambda prompt: True)
    monkeypatch.setattr(profile_tui, "delete_profile", lambda *a, **k: (False, "rmtree failed"))
    # do() calls input() after a failure ("press enter to continue").
    monkeypatch.setattr("builtins.input", lambda *a: "")
    msg = profile_tui._action_delete(FakeState(), "work")
    assert msg == "delete cancelled."  # failed delete is not "deleted"
    assert "rmtree failed" in capsys.readouterr().err


# --- _action_open_edge -------------------------------------------------


def test_open_edge_delegates_to_capture(monkeypatch):
    from owa_piggy import capture as capture_mod

    calls = []
    monkeypatch.setattr(
        capture_mod, "open_edge", lambda alias, **kw: calls.append(alias) or ("proc", "/tmp/x")
    )
    msg = profile_tui._action_open_edge("work")
    assert calls == ["work"]
    assert "opened Edge" in msg and "work" in msg


def test_open_edge_surfaces_launch_failure(monkeypatch):
    from owa_piggy import capture as capture_mod

    def _boom(alias, **kw):
        raise RuntimeError("Microsoft Edge not found.")

    monkeypatch.setattr(capture_mod, "open_edge", _boom)
    msg = profile_tui._action_open_edge("work")
    assert "edge launch failed" in msg
    assert "Microsoft Edge not found" in msg


# --- _freshness_cell (dashboard token-health column) -------------------


def _report(state, *, minutes=None, hints=None):
    return {
        "profile": "work",
        "state": state,
        "access_token": {"present": state in ("ok", "warn"), "minutes_remaining": minutes},
        "hints": hints or [],
    }


def test_freshness_cell_unprobed_is_dim_placeholder():
    text, color = profile_tui._freshness_cell(None)
    assert text == "probing..."
    assert color == profile_tui.DIM


def test_freshness_cell_ok_is_green_with_humanized_minutes():
    text, color = profile_tui._freshness_cell(_report("ok", minutes=58))
    assert text == "fresh 58m"
    assert color == profile_tui.GREEN


def test_freshness_cell_warn_is_yellow():
    text, color = profile_tui._freshness_cell(_report("warn", minutes=4))
    assert text == "expiring 4m"
    assert color == profile_tui.YELLOW


def test_freshness_cell_fail_surfaces_first_hint_in_red():
    text, color = profile_tui._freshness_cell(
        _report("fail", hints=["run owa-piggy setup --profile work"])
    )
    assert text == "run owa-piggy setup --profile work"
    assert color == profile_tui.RED


def test_freshness_cell_fail_without_hint_is_generic():
    text, color = profile_tui._freshness_cell(_report("fail"))
    assert text == "needs reseed (r)"
    assert color == profile_tui.RED


def test_freshness_cell_disabled_is_dim():
    text, color = profile_tui._freshness_cell(_report("disabled"))
    assert text == "disabled"
    assert color == profile_tui.DIM


def test_freshness_cell_truncates_long_hint():
    long = "x" * 80
    text, _color = profile_tui._freshness_cell(_report("fail", hints=[long]))
    assert text.endswith("...")
    assert len(text) <= 46


# --- print_plain_status (non-TTY fallback) -----------------------------


def test_print_plain_status_empty(monkeypatch, capsys):
    monkeypatch.setattr(profile_tui, "list_profiles", lambda: [])
    rc = profile_tui.print_plain_status()
    assert rc == 0
    assert "no profiles configured" in capsys.readouterr().out


def test_print_plain_status_renders_freshness_without_escapes(monkeypatch, capsys):
    from owa_piggy import status as status_mod

    monkeypatch.setattr(profile_tui, "list_profiles", lambda: ["work", "home"])
    monkeypatch.setattr(
        profile_tui,
        "load_profiles_conf",
        lambda: {"OWA_DEFAULT_PROFILE": "work", "OWA_PROFILES": ["work", "home"]},
    )
    # Never hit the network: hand back a canned all-profiles report.
    monkeypatch.setattr(
        status_mod,
        "status_all_report",
        lambda **kw: {
            "profiles": [
                _report("ok", minutes=58),
                {
                    "profile": "home",
                    "state": "fail",
                    "access_token": {"present": False, "minutes_remaining": None},
                    "hints": ["run owa-piggy setup --profile home"],
                },
            ],
            "summary": {"ok": 1, "warn": 0, "fail": 1},
        },
    )
    rc = profile_tui.print_plain_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "* work" in out and "fresh 58m" in out
    assert "home" in out and "run owa-piggy setup --profile home" in out
    assert "\x1b[" not in out  # plain mode carries no ANSI escapes


# --- run_dashboard non-TTY routing -------------------------------------


def test_run_dashboard_falls_back_to_plain_when_not_a_tty(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(profile_tui, "print_plain_status", lambda **kw: calls.append(kw) or 0)
    rc = profile_tui.run_dashboard(audience="graph")
    assert rc == 0
    assert calls == [{"audience": "graph", "scope": None, "sharepoint_tenant": None}]


# --- capture-mode column + 'h' pin -------------------------------------


def test_headless_cell_marks_unpinned_with_a_tilde():
    auto, _ = profile_tui._headless_cell({})
    pinned, _ = profile_tui._headless_cell(
        {"OWA_CAPTURE_HEADLESS": "1", "OWA_CAPTURE_HEADLESS_AT": ""}
    )
    visible, color = profile_tui._headless_cell(
        {"OWA_CAPTURE_HEADLESS": "0", "OWA_CAPTURE_HEADLESS_AT": ""}
    )
    assert auto == "~headless"
    assert pinned == "headless"
    assert (visible, color) == ("visible", profile_tui.YELLOW)


def test_toggle_headless_pins_the_opposite_mode(tmp_path, monkeypatch):
    """'h' flips the mode and writes an empty stamp, which is how reseed
    tells a deliberate choice from its own 24h preference."""
    from owa_piggy import config as config_mod

    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("OWA_CAPTURE_HEADLESS", raising=False)
    path = config_mod.profile_config_path("work")
    write_doc(path, {"OWA_REFRESH_TOKEN": "fake-rt-for-tests"})

    msg = profile_tui._action_toggle_headless("work")
    assert "visible" in msg
    cfg = profile_tui._profile_config("work")
    assert cfg["OWA_CAPTURE_HEADLESS"] == "0"
    assert cfg["OWA_CAPTURE_HEADLESS_AT"] == ""
    # Unrelated keys survive the in-place update.
    assert cfg["OWA_REFRESH_TOKEN"] == "fake-rt-for-tests"

    msg = profile_tui._action_toggle_headless("work")
    assert "headless" in msg
    assert profile_tui._profile_config("work")["OWA_CAPTURE_HEADLESS"] == "1"


def test_toggle_headless_refuses_when_env_overrides(monkeypatch):
    monkeypatch.setenv("OWA_CAPTURE_HEADLESS", "0")
    msg = profile_tui._action_toggle_headless("work")
    assert "environment" in msg


def _edit_setup(tmp_path, monkeypatch, rewrite):
    """Point the dashboard's `c` action at a fake editor that applies
    `rewrite(text) -> text` to the file it is handed."""
    from owa_piggy import config as config_mod

    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path)
    path = write_doc(
        config_mod.profile_config_path("work"),
        {"OWA_REFRESH_TOKEN": "fake-rt", "OWA_EMAIL": "a@x"},
    )

    def fake_call(argv):
        target = Path(argv[1])
        assert target != path  # never the live file
        target.write_text(rewrite(target.read_text()))
        return 0

    monkeypatch.setattr(profile_tui.subprocess, "call", fake_call)
    state = type("S", (), {"cooked_action": staticmethod(lambda fn: fn())})()
    return path, state


def test_edit_config_merges_a_valid_edit(tmp_path, monkeypatch):
    path, state = _edit_setup(tmp_path, monkeypatch, lambda t: t.replace("a@x", "b@x"))
    assert "edited" in profile_tui._action_edit_config(state, "work")
    assert read_settings(path) == {"OWA_REFRESH_TOKEN": "fake-rt", "OWA_EMAIL": "b@x"}
    assert not (path.parent / ".config.edit.json").exists()


def test_edit_config_discards_invalid_json(tmp_path, monkeypatch):
    path, state = _edit_setup(tmp_path, monkeypatch, lambda t: t + ",")
    monkeypatch.setattr("builtins.input", lambda _prompt: "d")
    assert "discarded" in profile_tui._action_edit_config(state, "work")
    assert read_settings(path)["OWA_REFRESH_TOKEN"] == "fake-rt"


# --- client columns ---------------------------------------------------

_NOW = 1_800_000_000.0  # fixed clock for the age checks
_AAD = {"OWA_REFRESH_TOKEN": "fake-rt"}


def _iso(ts):
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def test_client_columns_known_first_then_unknown_in_use():
    records = {
        "a": [{"name": "teams", "client_id": profile_tui.clients_mod.TEAMS_WEB_CLIENT_ID}],
        "b": [{"name": "abcdef12-3456", "client_id": "abcdef12-3456-7890-abcd-ef1234567890"}],
    }
    cols = profile_tui._client_columns(records)
    assert [label for label, _ in cols][:7] == [
        "owa",
        "teams",
        "ado",
        "halo",
        "swodp",
        "kova",
        "pim",
    ]
    assert cols[7] == ("abcdef", "abcdef12-3456-7890-abcd-ef1234567890")


def test_client_cell_states():
    cell = profile_tui._client_cell
    teams = profile_tui.clients_mod.TEAMS_WEB_CLIENT_ID
    fresh = {
        "name": "teams",
        "client_id": teams,
        "refresh_token": "t",
        "rt_issued_at": _iso(_NOW - 60),
    }
    stale = {**fresh, "rt_issued_at": _iso(_NOW - 2 * 86400)}
    assert cell(None, None, _AAD, _NOW)[:2] == ("●", profile_tui.GREEN)
    assert cell(teams, None, _AAD, _NOW)[0] == "·"
    assert cell(teams, fresh, _AAD, _NOW)[:2] == ("●", profile_tui.GREEN)
    assert cell(teams, stale, _AAD, _NOW)[:2] == ("●", profile_tui.YELLOW)
    assert cell(teams, {**fresh, "enabled": False}, _AAD, _NOW)[0] == "○"
    assert cell(teams, {"name": "teams", "client_id": teams}, _AAD, _NOW)[1] == profile_tui.YELLOW
    assert cell("swodp", {"name": "swodp"}, _AAD, _NOW)[:2] == ("●", profile_tui.GREEN)
    assert cell("kova", {"name": "kova"}, _AAD, _NOW)[1] == profile_tui.YELLOW
    # Halo's token has no 24h SPA cap: an old stamp is still healthy.
    halo = {"name": "halo", "refresh_token": "h", "rt_issued_at": _iso(_NOW - 9 * 86400)}
    assert cell("halo", halo, _AAD, _NOW)[1] == profile_tui.GREEN
    assert cell(teams, fresh, {"OWA_PROVIDER": "google"}, _NOW)[0] == " "


def test_cells_row_aligns_glyphs_under_labels():
    cols = [("owa", None), ("teams", "t")]
    row = profile_tui._cells_row(cols, [], _AAD, _NOW)
    plain = (
        row.replace(profile_tui.GREEN, "")
        .replace(profile_tui.DIM, "")
        .replace(profile_tui.RESET, "")
    )
    assert plain == "●   ·    "
    assert len(plain) == len(profile_tui._cells_header(cols))
    assert profile_tui.REVERSE in profile_tui._cells_row(cols, [], _AAD, _NOW, selected=1)


def _client_profile(tmp_path, monkeypatch):
    from owa_piggy import config as config_mod

    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path)
    teams = profile_tui.clients_mod.TEAMS_WEB_CLIENT_ID
    write_doc(
        config_mod.profile_config_path("work"),
        _AAD,
        [
            {
                "name": "teams",
                "client_id": teams,
                "enabled": True,
                "refresh_token": "t",
                "capture_url": "https://teams.microsoft.com/",
            }
        ],
    )
    record = profile_tui.clients_mod.load_records("work")[0]
    state = type("S", (), {"cooked_action": staticmethod(lambda fn: fn())})()
    return teams, record, state


def test_client_toggle_round_trip_keeps_token(tmp_path, monkeypatch):
    teams, record, _ = _client_profile(tmp_path, monkeypatch)
    msg = profile_tui._client_toggle("work", "teams", teams, record)
    assert "disabled" in msg and "owa-teams" in msg
    record = profile_tui.clients_mod.load_records("work")[0]
    assert record["enabled"] is False and record["refresh_token"] == "t"
    assert "enabled" in profile_tui._client_toggle("work", "teams", teams, record)
    assert "profile's own token" in profile_tui._client_toggle("work", "owa", None, None)
    assert "a adds" in profile_tui._client_toggle("work", "ado", "x", None)


def test_client_remove_needs_confirmation(tmp_path, monkeypatch):
    teams, record, state = _client_profile(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert "cancelled" in profile_tui._client_remove(state, "work", "teams", teams, record)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    assert "removed" in profile_tui._client_remove(state, "work", "teams", teams, record)
    assert profile_tui.clients_mod.load_records("work") == []


def test_client_add_asks_for_org_url_with_suggestion(tmp_path, monkeypatch):
    from owa_piggy import cli

    _client_profile(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(cli, "add_client", lambda alias, key, url: seen.update(url=url) or 0)
    monkeypatch.setattr("builtins.input", lambda _p: "")
    others = {"other": [{"name": "halo", "capture_url": "https://acme.haloitsm.com"}], "work": []}
    state = type("S", (), {"cooked_action": staticmethod(lambda fn: fn())})()
    msg = profile_tui._client_add(state, "work", "halo", "halo", None, others)
    assert "added" in msg and seen["url"] == "https://acme.haloitsm.com"


def test_client_edit_and_capture(tmp_path, monkeypatch):
    from owa_piggy import capture

    teams, record, state = _client_profile(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        capture, "capture_bound_clients", lambda a, only: calls.append(only) or (1, [])
    )
    answers = iter(["https://teams.cloud.microsoft/", "y"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert "captured" in profile_tui._client_edit(state, "work", "teams", teams, record)
    assert (
        profile_tui.clients_mod.load_records("work")[0]["capture_url"]
        == "https://teams.cloud.microsoft"
    )
    monkeypatch.setattr("builtins.input", lambda _p: "")
    assert "captured" in profile_tui._client_capture(state, "work", "teams", teams, record)
    assert calls == [[teams], [teams]]
    assert "declared only" in profile_tui._client_capture(
        state, "work", "swodp", "swodp", {"name": "swodp"}
    )
