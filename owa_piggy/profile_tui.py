"""Interactive profile manager for `owa-piggy profiles` on a TTY.

Runs a raw-terminal multi-key state machine that lets the user toggle
enabled status, set the default profile, add/delete profiles, install
or uninstall the launchd agent, and trigger a reseed - without
memorising the subcommand surface.

The picker only mutates state through the shared registry ops in
`profiles.py` so it cannot drift from the plain CLI subcommands. This
module owns terminal rendering and key dispatch; everything else is
borrowed.
"""
import sys

from .cache import clear_cache
from .config import (
    list_profiles,
    load_profiles_conf,
    profile_dir,
    validate_alias,
)
from .launchd import (
    is_installed as launchd_is_installed,
    run_setup_refresh,
)
from .profiles import (
    create_profile,
    delete_profile,
    disable_profile,
    enable_profile,
    set_default_profile,
)
from .reseed import do_reseed, do_reseed_all

# --- ANSI escapes ------------------------------------------------------
# Named for readability; otherwise the picker is mostly punctuation.

CLEAR_SCREEN = '\x1b[2J\x1b[H'
CLEAR_EOL = '\x1b[K'
HIDE_CURSOR = '\x1b[?25l'
SHOW_CURSOR = '\x1b[?25h'
DIM = '\x1b[2m'
GREEN = '\x1b[32m'
CYAN = '\x1b[36m'
RESET = '\x1b[0m'

# Suggested audiences shown in the new-profile prompt. This is a
# usability hint, not a constraint - any KNOWN_AUDIENCES short name or
# https URL is accepted by resolve_audience.
_AUDIENCE_HINTS = ('graph', 'outlook', 'teams', 'azure')


# --- Empty-state and add-profile flows ---------------------------------

def empty_state_setup_flow():
    """Walk a fresh-install user through creating their first profile.

    Asks for alias, email (network-capture mode is the right default
    today - works on Okta-federated tenants too), and a default
    audience, then dispatches into the standard interactive_setup.
    After success, drops into the picker so the user sees what they
    just built.
    """
    print('owa-piggy: no profiles configured yet.')
    print('Let\'s set one up. Press Ctrl-C to abort.\n')
    alias, email, audience = prompt_new_profile_fields()
    if alias is None:
        return 1
    if create_profile(alias, email=email, audience=audience) != 0:
        return 1
    return run_picker()


def prompt_new_profile_fields(default_alias=''):
    """Prompt for (alias, email, audience). Returns (None, None, None)
    on abort.

    Uses cooked-mode `input()` so this is safe to call from anywhere -
    callers that are mid-raw-mode must restore cooked first (the picker
    does this via `PickerState.cooked_action`).
    """
    while True:
        try:
            raw = input(
                f'profile name (alias)'
                f'{f" [{default_alias}]" if default_alias else ""}: '
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None, None
        alias = raw or default_alias
        if not alias:
            print('  alias required.')
            continue
        ok, err = validate_alias(alias)
        if not ok:
            print(f'  {err}')
            continue
        if alias in list_profiles():
            print(f'  profile {alias!r} already exists.')
            continue
        break
    while True:
        try:
            email = input('email address (used by Edge sign-in capture): ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None, None
        if email and '@' in email:
            break
        print('  please enter an email address (e.g. you@example.com).')
    print(f'default audience for this profile [{"/".join(_AUDIENCE_HINTS)}, '
          f'or full https URL] (default: graph):')
    try:
        aud_raw = input('  audience: ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None, None, None
    audience = aud_raw or 'graph'
    return alias, email, audience


# --- Picker state ------------------------------------------------------

class PickerState:
    """Owns the picker's mutable state plus the raw/cooked toggle.

    Lifting the closure-captured locals onto an explicit object lets the
    action functions live at module level (testable, no per-keystroke
    closure allocation) while still sharing terminal mode and cursor
    position with the loop.
    """

    def __init__(self, fd, old_termios):
        self.fd = fd
        self.old = old_termios
        self.idx = 0
        self.message = ''

    def go_raw(self):
        import tty
        tty.setraw(self.fd)

    def restore(self):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def cooked_action(self, fn):
        """Run fn() outside raw mode (so input() / print() work normally),
        then restore raw mode. Returns whatever fn returns.
        """
        self.restore()
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        try:
            return fn()
        finally:
            sys.stdout.write(HIDE_CURSOR)
            sys.stdout.flush()
            self.go_raw()


def _confirm(prompt):
    """y/N confirmation in cooked mode. Default no."""
    try:
        ans = input(f'{prompt} [y/N]: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ('y', 'yes')


# --- Action functions --------------------------------------------------
# Each takes the shared PickerState and (where applicable) the currently
# highlighted profile alias, performs a registry mutation or shells out,
# and returns the status-line message to display on next redraw.

def _action_toggle(current, enabled):
    if current in enabled:
        disable_profile(current)
        return f'disabled {current!r}.'
    ok, err = enable_profile(current)
    return f'enabled {current!r}.' if ok else f'enable failed: {err}'


def _action_set_default(current, default):
    if current == default:
        return f'{current!r} is already the default.'
    ok, err = set_default_profile(current)
    return (f'default profile set to {current!r}.' if ok
            else f'set-default failed: {err}')


def _action_add(state):
    def do():
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        alias, email, audience = prompt_new_profile_fields()
        if alias is None:
            return None
        rc = create_profile(alias, email=email, audience=audience)
        return alias if rc == 0 else None

    new_alias = state.cooked_action(do)
    profiles = list_profiles()
    if new_alias and new_alias in profiles:
        state.idx = profiles.index(new_alias)
        return f'added profile {new_alias!r}.'
    return 'add cancelled or failed.'


def _action_delete(state, current):
    def do():
        print()
        print(f'About to delete profile {current!r}:')
        print(f'  - removes {profile_dir(current)}')
        print('  - unregisters from profiles.conf')
        if launchd_is_installed(current):
            print('  - uninstalls launchd agent')
        if not _confirm(f'delete {current!r}?'):
            return False
        ok, err = delete_profile(
            current,
            uninstall_launchd=True,
            promote_default=True,
        )
        if not ok:
            print(f'ERROR: {err}', file=sys.stderr)
            input('press enter to continue...')
            return False
        return True

    deleted = state.cooked_action(do)
    return f'deleted {current!r}.' if deleted else 'delete cancelled.'


def _action_install(state, current):
    def do():
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        rc = run_setup_refresh(current, install=True)
        if rc == 0:
            print(f'\nlaunchd agent installed for {current!r}.')
        input('press enter to continue...')
        return rc

    rc = state.cooked_action(do)
    return (f'launchd installed for {current!r}.' if rc == 0
            else f'launchd install failed for {current!r}.')


def _action_uninstall(state, current):
    if not launchd_is_installed(current):
        return f'no launchd agent installed for {current!r}.'

    def do():
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        rc = run_setup_refresh(current, install=False)
        if rc == 0:
            print(f'\nlaunchd agent uninstalled for {current!r}.')
        input('press enter to continue...')
        return rc

    rc = state.cooked_action(do)
    return (f'launchd uninstalled for {current!r}.' if rc == 0
            else f'launchd uninstall failed for {current!r}.')


def _action_reseed(state, current):
    def do():
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        print(f'Reseeding {current!r}...\n')
        clear_cache()
        rc = do_reseed(current)
        print()
        input('press enter to continue...')
        return rc

    rc = state.cooked_action(do)
    return (f'reseed succeeded for {current!r}.' if rc == 0
            else f'reseed failed for {current!r}.')


def _action_reseed_all(state):
    """Shift-r: reseed every configured profile sequentially.

    Surfaces the same capability as `owa-piggy reseed --all` from inside
    the picker so the routine "Monday morning, all RTs are stale" flow
    doesn't require dropping out to the shell.
    """
    def do():
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        print('Reseeding all profiles...\n')
        rc = do_reseed_all()
        print()
        input('press enter to continue...')
        return rc

    rc = state.cooked_action(do)
    return ('reseed --all succeeded.' if rc == 0
            else 'reseed --all failed (see above).')


# --- Picker loop -------------------------------------------------------

def run_picker():
    """Multi-key profile manager.

    Keys:
      up/down or j/k - move cursor
      space          - toggle enabled (registered in OWA_PROFILES)
      enter          - set highlighted profile as default
      a              - add a new profile (drops into setup)
      d              - delete profile (with confirm)
      l              - install launchd agent for highlighted profile
      u              - uninstall launchd agent for highlighted profile
      r              - reseed highlighted profile (drops out, re-enters)
      R              - reseed every profile (= `owa-piggy reseed --all`)
      q / esc        - quit

    Falls back to a plain printed list when termios is unavailable.
    """
    try:
        import termios
    except ImportError:
        # Non-POSIX: degrade gracefully to a plain listing. tty (used by
        # PickerState.go_raw) ships alongside termios on every platform
        # that has either, so probing one is enough.
        return _print_plain_list()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    state = PickerState(fd, old)

    def load_state():
        profiles = list_profiles()
        reg = load_profiles_conf()
        return profiles, reg['OWA_DEFAULT_PROFILE'], set(reg['OWA_PROFILES'])

    def clamp_cursor(profiles):
        if not profiles:
            state.idx = 0
        elif state.idx >= len(profiles):
            state.idx = len(profiles) - 1
        elif state.idx < 0:
            state.idx = 0

    def draw():
        profiles, default, enabled = load_state()
        clamp_cursor(profiles)
        # Full-screen redraw: cheaper to reason about than a partial diff,
        # and the screen is tiny.
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.write('owa-piggy profiles\r\n')
        sys.stdout.write(
            f'  {DIM}'
            'up/down  navigate  ·  space toggle  ·  enter set default\r\n'
            '  a add  ·  d delete  ·  l install launchd  ·  u uninstall  ·  r reseed  ·  R reseed all  ·  q quit'
            f'{RESET}\r\n\r\n'
        )
        if not profiles:
            sys.stdout.write('  (no profiles - press "a" to add one, q to quit)\r\n')
        else:
            for i, alias in enumerate(profiles):
                cursor = '>' if i == state.idx else ' '
                if alias == default:
                    state_marker = f'{GREEN}*{RESET}'
                elif alias in enabled:
                    state_marker = f'{GREEN}x{RESET}'
                else:
                    state_marker = f'{DIM} {RESET}'
                launchd_marker = f' {CYAN}(L){RESET}' if launchd_is_installed(alias) else ''
                sys.stdout.write(
                    f' {cursor} [{state_marker}] {alias}{launchd_marker}{CLEAR_EOL}\r\n'
                )
        sys.stdout.write('\r\n')
        if state.message:
            sys.stdout.write(f'  {state.message}{CLEAR_EOL}\r\n')
        else:
            sys.stdout.write(f'{CLEAR_EOL}\r\n')
        sys.stdout.flush()

    try:
        state.go_raw()
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()
        # Cursor starts on the current default if any, else top.
        profiles, default, _ = load_state()
        if default in profiles:
            state.idx = profiles.index(default)
        draw()
        while True:
            ch = sys.stdin.read(1)
            profiles, default, enabled = load_state()
            clamp_cursor(profiles)
            current = profiles[state.idx] if profiles else None

            if ch == '\x03':
                raise KeyboardInterrupt
            if ch in ('q', 'Q'):
                break
            if ch == '\x1b':
                seq = sys.stdin.read(1)
                if seq == '[':
                    arrow = sys.stdin.read(1)
                    if arrow == 'A':
                        state.idx = max(0, state.idx - 1)
                    elif arrow == 'B':
                        state.idx = min(max(0, len(profiles) - 1), state.idx + 1)
                    state.message = ''
                    draw()
                    continue
                # Bare ESC = quit.
                break
            if ch == 'k':
                state.idx = max(0, state.idx - 1)
                state.message = ''
                draw()
                continue
            if ch == 'j':
                state.idx = min(max(0, len(profiles) - 1), state.idx + 1)
                state.message = ''
                draw()
                continue

            if ch == 'a':
                state.message = _action_add(state) or ''
                draw()
                continue

            if ch == 'R':
                state.message = _action_reseed_all(state) or ''
                draw()
                continue

            if not current:
                # All remaining keys need a selected profile.
                state.message = 'no profile selected.'
                draw()
                continue

            if ch == ' ':
                state.message = _action_toggle(current, enabled) or ''
                draw()
                continue

            if ch in ('\r', '\n'):
                state.message = _action_set_default(current, default) or ''
                draw()
                continue

            if ch == 'd':
                state.message = _action_delete(state, current) or ''
                draw()
                continue

            if ch == 'l':
                state.message = _action_install(state, current) or ''
                draw()
                continue

            if ch == 'u':
                state.message = _action_uninstall(state, current) or ''
                draw()
                continue

            if ch == 'r':
                state.message = _action_reseed(state, current) or ''
                draw()
                continue

            # Unknown key - just clear any stale message.
            state.message = ''
            draw()
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        state.restore()
    # Move cursor to the bottom of the picker on exit so the next shell
    # prompt does not overwrite the last frame.
    print()
    return 0


def _print_plain_list():
    """Plain printed listing - the non-TTY / no-termios fallback."""
    profiles = list_profiles()
    reg = load_profiles_conf()
    default = reg['OWA_DEFAULT_PROFILE']
    enabled = set(reg['OWA_PROFILES'])
    for alias in profiles:
        marker = '*' if alias == default else ('x' if alias in enabled else ' ')
        print(f' {marker} {alias}')
    return 0
