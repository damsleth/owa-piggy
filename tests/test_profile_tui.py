"""Unit tests for the profile picker's action helpers.

The raw-terminal key loop (run_picker) needs a real TTY and is not
exercised here - test_cli_smoke covers Ctrl-C restoration. What IS
testable, and was previously not, is the dispatch logic in the
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
    monkeypatch.setattr(profile_tui, 'launchd_is_installed', lambda a: False)
    monkeypatch.setattr(profile_tui, 'profile_dir', lambda a: f'/tmp/{a}')
    monkeypatch.setattr(profile_tui, '_confirm', lambda prompt: False)
    monkeypatch.setattr(profile_tui, 'delete_profile',
                        lambda *a, **k: deleted.append(a) or (True, None))
    msg = profile_tui._action_delete(FakeState(), 'work')
    assert deleted == []  # confirm declined -> never deleted
    assert msg == 'delete cancelled.'


def test_delete_delegates_when_confirmed(monkeypatch):
    calls = []
    monkeypatch.setattr(profile_tui, 'launchd_is_installed', lambda a: False)
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
    monkeypatch.setattr(profile_tui, 'launchd_is_installed', lambda a: False)
    monkeypatch.setattr(profile_tui, 'profile_dir', lambda a: f'/tmp/{a}')
    monkeypatch.setattr(profile_tui, '_confirm', lambda prompt: True)
    monkeypatch.setattr(profile_tui, 'delete_profile',
                        lambda *a, **k: (False, 'rmtree failed'))
    # do() calls input() after a failure ("press enter to continue").
    monkeypatch.setattr('builtins.input', lambda *a: '')
    msg = profile_tui._action_delete(FakeState(), 'work')
    assert msg == 'delete cancelled.'  # failed delete is not "deleted"
    assert 'rmtree failed' in capsys.readouterr().err
