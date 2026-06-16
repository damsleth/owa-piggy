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
from owa_piggy import profile_tui


class FakeState:
    """Stand-in for PickerState. cooked_action just runs the closure -
    there is no terminal to drop in and out of raw mode for."""
    def __init__(self):
        self.idx = 0
        self.message = ''

    def cooked_action(self, fn):
        return fn()


# --- _action_toggle ----------------------------------------------------

def test_toggle_disables_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui, 'disable_profile',
                        lambda a: calls.append(('disable', a)))
    monkeypatch.setattr(profile_tui, 'enable_profile',
                        lambda a: calls.append(('enable', a)) or (True, None))
    msg = profile_tui._action_toggle('work', enabled={'work', 'home'})
    assert calls == [('disable', 'work')]
    assert msg == "disabled 'work'."


def test_toggle_enables_when_not_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui, 'disable_profile',
                        lambda a: calls.append(('disable', a)))
    monkeypatch.setattr(profile_tui, 'enable_profile',
                        lambda a: calls.append(('enable', a)) or (True, None))
    msg = profile_tui._action_toggle('work', enabled={'home'})
    assert calls == [('enable', 'work')]
    assert msg == "enabled 'work'."


def test_toggle_surfaces_enable_failure(monkeypatch):
    monkeypatch.setattr(profile_tui, 'enable_profile',
                        lambda a: (False, 'boom'))
    msg = profile_tui._action_toggle('work', enabled=set())
    assert msg == 'enable failed: boom'


# --- _action_set_default -----------------------------------------------

def test_set_default_short_circuits_when_already_default(monkeypatch):
    called = []
    monkeypatch.setattr(profile_tui, 'set_default_profile',
                        lambda a: called.append(a) or (True, None))
    msg = profile_tui._action_set_default('work', default='work')
    assert called == []  # no registry write when already default
    assert 'already the default' in msg


def test_set_default_delegates_when_changing(monkeypatch):
    called = []
    monkeypatch.setattr(profile_tui, 'set_default_profile',
                        lambda a: called.append(a) or (True, None))
    msg = profile_tui._action_set_default('work', default='home')
    assert called == ['work']
    assert msg == "default profile set to 'work'."


def test_set_default_surfaces_failure(monkeypatch):
    monkeypatch.setattr(profile_tui, 'set_default_profile',
                        lambda a: (False, 'nope'))
    msg = profile_tui._action_set_default('work', default='home')
    assert msg == 'set-default failed: nope'


# --- _action_delete ----------------------------------------------------

def test_delete_aborts_when_not_confirmed(monkeypatch):
    deleted = []
    monkeypatch.setattr(profile_tui, 'launchd_is_scheduled', lambda a: False)
    monkeypatch.setattr(profile_tui, 'profile_dir', lambda a: f'/tmp/{a}')
    monkeypatch.setattr(profile_tui, '_confirm', lambda prompt: False)
    monkeypatch.setattr(profile_tui, 'delete_profile',
                        lambda *a, **k: deleted.append(a) or (True, None))
    msg = profile_tui._action_delete(FakeState(), 'work')
    assert deleted == []  # confirm declined -> never deleted
    assert msg == 'delete cancelled.'


def test_delete_delegates_when_confirmed(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui, 'launchd_is_scheduled', lambda a: False)
    monkeypatch.setattr(profile_tui, 'profile_dir', lambda a: f'/tmp/{a}')
    monkeypatch.setattr(profile_tui, '_confirm', lambda prompt: True)

    def fake_delete(alias, *, uninstall_launchd, promote_default):
        calls.append((alias, uninstall_launchd, promote_default))
        return True, None
    monkeypatch.setattr(profile_tui, 'delete_profile', fake_delete)

    msg = profile_tui._action_delete(FakeState(), 'work')
    assert calls == [('work', True, True)]
    assert msg == "deleted 'work'."


def test_delete_surfaces_failure(monkeypatch, capsys):
    monkeypatch.setattr(profile_tui, 'launchd_is_scheduled', lambda a: False)
    monkeypatch.setattr(profile_tui, 'profile_dir', lambda a: f'/tmp/{a}')
    monkeypatch.setattr(profile_tui, '_confirm', lambda prompt: True)
    monkeypatch.setattr(profile_tui, 'delete_profile',
                        lambda *a, **k: (False, 'rmtree failed'))
    # do() calls input() after a failure ("press enter to continue").
    monkeypatch.setattr('builtins.input', lambda *a: '')
    msg = profile_tui._action_delete(FakeState(), 'work')
    assert msg == 'delete cancelled.'  # failed delete is not "deleted"
    assert 'rmtree failed' in capsys.readouterr().err


# --- _action_open_edge -------------------------------------------------

def test_open_edge_delegates_to_capture(monkeypatch):
    from owa_piggy import capture as capture_mod
    calls = []
    monkeypatch.setattr(capture_mod, 'open_edge',
                        lambda alias, **kw: calls.append(alias) or ('proc', '/tmp/x'))
    msg = profile_tui._action_open_edge('work')
    assert calls == ['work']
    assert 'opened Edge' in msg and 'work' in msg


def test_open_edge_surfaces_launch_failure(monkeypatch):
    from owa_piggy import capture as capture_mod

    def _boom(alias, **kw):
        raise RuntimeError('Microsoft Edge not found.')

    monkeypatch.setattr(capture_mod, 'open_edge', _boom)
    msg = profile_tui._action_open_edge('work')
    assert 'edge launch failed' in msg
    assert 'Microsoft Edge not found' in msg


# --- _freshness_cell (dashboard token-health column) -------------------

def _report(state, *, minutes=None, hints=None):
    return {
        'profile': 'work',
        'state': state,
        'access_token': {'present': state in ('ok', 'warn'),
                         'minutes_remaining': minutes},
        'hints': hints or [],
    }


def test_freshness_cell_unprobed_is_dim_placeholder():
    text, color = profile_tui._freshness_cell(None)
    assert text == 'probing...'
    assert color == profile_tui.DIM


def test_freshness_cell_ok_is_green_with_humanized_minutes():
    text, color = profile_tui._freshness_cell(_report('ok', minutes=58))
    assert text == 'fresh 58m'
    assert color == profile_tui.GREEN


def test_freshness_cell_warn_is_yellow():
    text, color = profile_tui._freshness_cell(_report('warn', minutes=4))
    assert text == 'expiring 4m'
    assert color == profile_tui.YELLOW


def test_freshness_cell_fail_surfaces_first_hint_in_red():
    text, color = profile_tui._freshness_cell(
        _report('fail', hints=['run owa-piggy setup --profile work']))
    assert text == 'run owa-piggy setup --profile work'
    assert color == profile_tui.RED


def test_freshness_cell_fail_without_hint_is_generic():
    text, color = profile_tui._freshness_cell(_report('fail'))
    assert text == 'needs reseed (r)'
    assert color == profile_tui.RED


def test_freshness_cell_disabled_is_dim():
    text, color = profile_tui._freshness_cell(_report('disabled'))
    assert text == 'disabled'
    assert color == profile_tui.DIM


def test_freshness_cell_truncates_long_hint():
    long = 'x' * 80
    text, _color = profile_tui._freshness_cell(_report('fail', hints=[long]))
    assert text.endswith('...')
    assert len(text) <= 46


# --- print_plain_status (non-TTY fallback) -----------------------------

def test_print_plain_status_empty(monkeypatch, capsys):
    monkeypatch.setattr(profile_tui, 'list_profiles', lambda: [])
    rc = profile_tui.print_plain_status()
    assert rc == 0
    assert 'no profiles configured' in capsys.readouterr().out


def test_print_plain_status_renders_freshness_without_escapes(monkeypatch, capsys):
    from owa_piggy import status as status_mod
    monkeypatch.setattr(profile_tui, 'list_profiles', lambda: ['work', 'home'])
    monkeypatch.setattr(profile_tui, 'load_profiles_conf',
                        lambda: {'OWA_DEFAULT_PROFILE': 'work',
                                 'OWA_PROFILES': ['work', 'home']})
    # Never hit the network: hand back a canned all-profiles report.
    monkeypatch.setattr(status_mod, 'status_all_report',
                        lambda **kw: {'profiles': [
                            _report('ok', minutes=58),
                            {'profile': 'home', 'state': 'fail',
                             'access_token': {'present': False, 'minutes_remaining': None},
                             'hints': ['run owa-piggy setup --profile home']},
                        ], 'summary': {'ok': 1, 'warn': 0, 'fail': 1}})
    rc = profile_tui.print_plain_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert '* work' in out and 'fresh 58m' in out
    assert 'home' in out and 'run owa-piggy setup --profile home' in out
    assert '\x1b[' not in out  # plain mode carries no ANSI escapes


# --- run_dashboard non-TTY routing -------------------------------------

def test_run_dashboard_falls_back_to_plain_when_not_a_tty(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(profile_tui, 'print_plain_status',
                        lambda **kw: calls.append(kw) or 0)
    rc = profile_tui.run_dashboard(audience='graph')
    assert rc == 0
    assert calls == [{'audience': 'graph', 'scope': None, 'sharepoint_tenant': None}]
