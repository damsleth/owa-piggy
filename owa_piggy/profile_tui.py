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
    is_scheduled as launchd_is_scheduled,
)
from .launchd import (
    schedule as launchd_schedule,
)
from .launchd import (
    unschedule as launchd_unschedule,
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
YELLOW = '\x1b[33m'
RED = '\x1b[31m'
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
    After success, drops into the dashboard so the user sees what they
    just built (and its token health).
    """
    print('owa-piggy: no profiles configured yet.')
    print('Let\'s set one up. Press Ctrl-C to abort.\n')
    alias, email, audience = prompt_new_profile_fields()
    if alias is None:
        return 1
    if create_profile(alias, email=email, audience=audience) != 0:
        return 1
    return run_dashboard()


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
    # Empty email => legacy paste flow (faster, works on plain MSAL
    # tenants). Set email => network-capture flow (required for
    # encrypted-MSAL / Okta-federated tenants). The free-form prompt
    # lets the user choose without making them remember `--email`.
    while True:
        try:
            email = input(
                'email address for Edge sign-in capture '
                '(blank = legacy paste flow): '
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None, None
        if not email:
            email = None
            break
        if '@' in email:
            break
        print('  enter an email address (e.g. you@example.com), '
              'or leave blank for the paste flow.')
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
        if launchd_is_scheduled(current):
            print('  - removes from launchd schedule')
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
        rc = launchd_schedule(current)
        if rc == 0:
            print(f'\n{current!r} added to launchd schedule.')
        input('press enter to continue...')
        return rc

    rc = state.cooked_action(do)
    return (f'{current!r} scheduled.' if rc == 0
            else f'scheduling {current!r} failed.')


def _action_uninstall(state, current):
    if not launchd_is_scheduled(current):
        return f'{current!r} is not scheduled.'

    def do():
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        rc = launchd_unschedule(current)
        if rc == 0:
            print(f'\n{current!r} removed from launchd schedule.')
        input('press enter to continue...')
        return rc

    rc = state.cooked_action(do)
    return (f'{current!r} unscheduled.' if rc == 0
            else f'unscheduling {current!r} failed.')


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


def _action_open_edge(current):
    """Open a normal Edge window against <current>'s sidecar userdata dir
    and leave it running. No cooked-mode drop: open_edge is detached and
    returns immediately, so there's nothing to wait on - we stay in the
    picker and just report what happened on the status line.
    """
    from .capture import open_edge
    try:
        open_edge(current)
    except RuntimeError as e:
        return f'edge launch failed for {current!r}: {e}'
    return f'opened Edge for {current!r}; sign in, CLOSE Edge, then reseed (r).'


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


def print_plain_list():
    """Plain printed listing of profiles, marking default with '*' and
    enabled-but-not-default with 'x'.

    Used by cli.`_do_profiles_list` for the non-TTY / pipe / redirect
    case when called without a freshness probe. For the dashboard's own
    fallback (alias + token freshness) see `print_plain_status`.
    """
    profiles = list_profiles()
    reg = load_profiles_conf()
    default = reg['OWA_DEFAULT_PROFILE']
    enabled = set(reg['OWA_PROFILES'])
    scheduled = set(reg.get('OWA_SCHEDULED', []))
    for alias in profiles:
        marker = '*' if alias == default else ('x' if alias in enabled else ' ')
        sched = ' (S)' if alias in scheduled else ''
        print(f' {marker} {alias}{sched}')
    return 0


# --- Dashboard (token health) ------------------------------------------

def _freshness_cell(report):
    """Map one status report to a (text, ansi_color) pair for the dashboard.

    Pure - no I/O. `report` is one entry from
    `status.status_all_report()['profiles']`, or None when the profile
    hasn't been probed yet. The text carries no escapes so the non-TTY
    fallback can print it verbatim; the caller wraps it in `color`.

    The cell tracks the access-token `state`: `ok` -> green "fresh <Nm>"
    (humanized minutes_remaining), `warn` -> yellow "expiring <Nm>",
    `fail` -> red (the first actionable hint, e.g. "run owa-piggy setup
    --profile X"), `disabled` -> dim. Unprobed -> dim "probing...".
    """
    if report is None:
        return 'probing...', DIM

    from .status import _humanize_minutes

    st = report.get('state', 'fail')
    if st == 'disabled':
        return 'disabled', DIM

    at = report.get('access_token') or {}
    mins = at.get('minutes_remaining')

    if st == 'ok':
        text = f'fresh {_humanize_minutes(mins)}' if mins is not None else 'fresh'
        return text, GREEN
    if st == 'warn':
        text = f'expiring {_humanize_minutes(mins)}' if mins is not None else 'expiring'
        return text, YELLOW

    # fail: surface the first hint so the user knows the fix, truncated so
    # one broken profile can't blow out the row width.
    hints = report.get('hints') or []
    label = hints[0] if hints else 'needs reseed (r)'
    if len(label) > 44:
        label = label[:43] + '...'
    return label, RED


def print_plain_status(audience=None, scope=None, sharepoint_tenant=None):
    """Non-TTY fallback for `owa-piggy tui`: one line per profile with its
    token freshness, no escapes.

    Used when termios is unavailable or stdin/stdout isn't a TTY (pipes,
    CI, redirects). Shares `_freshness_cell` with the interactive dashboard
    so the two output paths cannot drift.
    """
    profiles = list_profiles()
    if not profiles:
        print('no profiles configured. Run: owa-piggy setup --profile <alias>')
        return 0
    from . import status as status_mod
    data = status_mod.status_all_report(
        audience=audience, scope=scope, sharepoint_tenant=sharepoint_tenant)
    reg = load_profiles_conf()
    default = reg['OWA_DEFAULT_PROFILE']
    reports = {r['profile']: r for r in data['profiles']}
    width = max((len(a) for a in profiles), default=0)
    for alias in profiles:
        marker = '*' if alias == default else ' '
        text, _color = _freshness_cell(reports.get(alias))
        print(f' {marker} {alias.ljust(width)}  {text}')
    return 0


def run_dashboard(audience=None, scope=None, sharepoint_tenant=None):
    """Interactive token-health dashboard for `owa-piggy tui`.

    Also the screen bare `owa-piggy profiles` opens on a TTY. Combines the
    profile list, markers, and single-key registry actions with a
    per-profile token-freshness column driven by a live
    `status.status_all_report` probe. Keys:

      up/down or j/k   navigate
      space            toggle enabled (registered in OWA_PROFILES)
      enter            set highlighted profile default
      a                add a new profile
      d                delete profile
      l / u            add / remove from launchd schedule
      r                reseed highlighted profile        (re-probes)
      R                reseed every profile               (re-probes)
      e                open Edge for highlighted profile's sidecar session
      g                refresh token health (re-probe)
      q / esc          quit

    Probing is network-bound (one live AAD exchange per profile, run
    concurrently by `_probe_all`), so the screen paints a "probing..."
    skeleton first, then redraws with results. Actions that change token
    state (reseed, toggle, add) trigger a re-probe; registry-only actions
    (default, schedule) just redraw. Falls back to `print_plain_status`
    when termios is unavailable or stdin/stdout isn't a TTY.
    """
    try:
        import termios
    except ImportError:
        return print_plain_status(audience=audience, scope=scope,
                                  sharepoint_tenant=sharepoint_tenant)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return print_plain_status(audience=audience, scope=scope,
                                  sharepoint_tenant=sharepoint_tenant)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    state = PickerState(fd, old)
    # alias -> status report, populated by reprobe(). Stored on state so the
    # cooked-mode action helpers can't see a stale closure.
    state.reports = {}

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

    def refresh():
        from . import status as status_mod
        data = status_mod.status_all_report(
            audience=audience, scope=scope, sharepoint_tenant=sharepoint_tenant)
        state.reports = {r['profile']: r for r in data['profiles']}

    def draw():
        profiles, default, enabled = load_state()
        clamp_cursor(profiles)
        scheduled = set(load_profiles_conf().get('OWA_SCHEDULED', []))
        launchd_state = {alias: alias in scheduled for alias in profiles}
        width = max((len(a) for a in profiles), default=0)
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.write('owa-piggy dashboard\r\n')
        sys.stdout.write(
            f'  {DIM}'
            'up/down  navigate  ·  space toggle  ·  enter set default  ·  g refresh\r\n'
            '  a add  ·  d delete  ·  l schedule  ·  u unschedule  ·  r reseed  ·  R reseed all  ·  e edge  ·  q quit'  # noqa: E501  (single-line key legend)
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
                launchd_marker = f' {CYAN}(S){RESET}' if launchd_state[alias] else ''
                text, color = _freshness_cell(state.reports.get(alias))
                cell = f'{color}{text}{RESET}'
                sys.stdout.write(
                    f' {cursor} [{state_marker}] {alias.ljust(width)}{launchd_marker}'
                    f'  {cell}{CLEAR_EOL}\r\n'
                )
        sys.stdout.write('\r\n')
        if state.message:
            sys.stdout.write(f'  {state.message}{CLEAR_EOL}\r\n')
        else:
            sys.stdout.write(f'{CLEAR_EOL}\r\n')
        sys.stdout.flush()

    def reprobe():
        # Clear cached reports so every row shows "probing..." during the
        # blocking network call, then repopulate and redraw.
        state.reports = {}
        draw()
        refresh()
        draw()

    try:
        state.go_raw()
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()
        profiles, default, _ = load_state()
        if default in profiles:
            state.idx = profiles.index(default)
        reprobe()
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

            if ch in ('g', 'G'):
                state.message = ''
                reprobe()
                continue

            if ch == 'a':
                state.message = _action_add(state) or ''
                reprobe()
                continue

            if ch == 'R':
                state.message = _action_reseed_all(state) or ''
                reprobe()
                continue

            if not current:
                # All remaining keys need a selected profile.
                state.message = 'no profile selected.'
                draw()
                continue

            if ch == ' ':
                state.message = _action_toggle(current, enabled) or ''
                reprobe()
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
                reprobe()
                continue

            if ch == 'e':
                state.message = _action_open_edge(current) or ''
                draw()
                continue

            # Unknown key - just clear any stale message.
            state.message = ''
            draw()
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        state.restore()
    # Move cursor to the bottom on exit so the next shell prompt does not
    # overwrite the last frame.
    print()
    return 0
    return 0
