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

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, TypeVar

from . import clients as clients_mod
from .cache import clear_cache
from .config import (
    DEVOPS_CLIENT_ID,
    ConfigCorruptError,
    atomic_write,
    classify_profile_type,
    list_profiles,
    load_config,
    load_profiles_conf,
    merge_edit,
    parse_iso_utc,
    profile_config_path,
    profile_dir,
    read_doc,
    save_config,
    update_doc,
    validate_alias,
    validate_doc,
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
from .reseed import _headless_pinned, _headless_pref, do_reseed, do_reseed_all

# --- ANSI escapes ------------------------------------------------------
# Named for readability; otherwise the picker is mostly punctuation.

CLEAR_SCREEN = "\x1b[2J\x1b[H"
CLEAR_EOL = "\x1b[K"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
RESET = "\x1b[0m"
REVERSE = "\x1b[7m"

# Suggested audiences shown in the new-profile prompt. This is a
# usability hint, not a constraint - any KNOWN_AUDIENCES short name or
# https URL is accepted by resolve_audience.
_AUDIENCE_HINTS = ("graph", "outlook", "teams", "azure")

_T = TypeVar("_T")


# --- Empty-state and add-profile flows ---------------------------------


def empty_state_setup_flow() -> int:
    """Walk a fresh-install user through creating their first profile.

    Asks for alias, email (network-capture mode is the right default
    today - works on Okta-federated tenants too), and a default
    audience, then dispatches into the standard interactive_setup.
    After success, drops into the dashboard so the user sees what they
    just built (and its token health).
    """
    print("owa-piggy: no profiles configured yet.")
    print("Let's set one up. Press Ctrl-C to abort.\n")
    alias, email, audience = prompt_new_profile_fields()
    if alias is None:
        return 1
    if create_profile(alias, email=email, audience=audience, ask_email=False) != 0:
        return 1
    return run_dashboard()


def prompt_new_profile_fields(
    default_alias: str = "",
) -> tuple[str, str | None, str] | tuple[None, None, None]:
    """Prompt for (alias, email, audience). Returns (None, None, None)
    on abort.

    Uses cooked-mode `input()` so this is safe to call from anywhere -
    callers that are mid-raw-mode must restore cooked first (the picker
    does this via `PickerState.cooked_action`).
    """
    while True:
        try:
            raw = input(
                f"profile name (alias){f' [{default_alias}]' if default_alias else ''}: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None, None
        alias = raw or default_alias
        if not alias:
            print("  alias required.")
            continue
        ok, err = validate_alias(alias)
        if not ok:
            print(f"  {err}")
            continue
        if alias in list_profiles():
            print(f"  profile {alias!r} already exists.")
            continue
        break
    # Empty email => legacy paste flow (faster, works on plain MSAL
    # tenants). Set email => network-capture flow (required for
    # encrypted-MSAL / Okta-federated tenants). The free-form prompt
    # lets the user choose without making them remember `--email`.
    email: str | None
    while True:
        try:
            email = input(
                "email address for Edge sign-in capture (blank = legacy paste flow): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None, None
        if not email:
            email = None
            break
        if "@" in email:
            break
        print("  enter an email address (e.g. you@example.com), or leave blank for the paste flow.")
    print(
        f"default audience for this profile [{'/'.join(_AUDIENCE_HINTS)}, "
        f"or full https URL] (default: graph):"
    )
    try:
        aud_raw = input("  audience: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None, None, None
    audience = aud_raw or "graph"
    return alias, email, audience


# --- Picker state ------------------------------------------------------


class PickerState:
    """Owns the picker's mutable state plus the raw/cooked toggle.

    Lifting the closure-captured locals onto an explicit object lets the
    action functions live at module level (testable, no per-keystroke
    closure allocation) while still sharing terminal mode and cursor
    position with the loop.
    """

    def __init__(self, fd: int, old_termios: list[Any]) -> None:
        self.fd = fd
        self.old = old_termios
        self.idx = 0
        # Cell cursor: 0 = the profile itself, n = client column n-1.
        self.col = 0
        self.message = ""
        # alias -> status report, populated by the dashboard's reprobe().
        self.reports: dict[str, Any] = {}

    def go_raw(self) -> None:
        import tty

        tty.setraw(self.fd)

    def restore(self) -> None:
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def cooked_action(self, fn: Callable[[], _T]) -> _T:
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


def _confirm(prompt: str) -> bool:
    """y/N confirmation in cooked mode. Default no."""
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


# --- Action functions --------------------------------------------------
# Each takes the shared PickerState and (where applicable) the currently
# highlighted profile alias, performs a registry mutation or shells out,
# and returns the status-line message to display on next redraw.


def _action_toggle(current: str, enabled: set[str]) -> str:
    if current in enabled:
        disable_profile(current)
        return f"disabled {current!r}."
    ok, err = enable_profile(current)
    return f"enabled {current!r}." if ok else f"enable failed: {err}"


def _action_set_default(current: str, default: str) -> str:
    if current == default:
        return f"{current!r} is already the default."
    ok, err = set_default_profile(current)
    return f"default profile set to {current!r}." if ok else f"set-default failed: {err}"


def _action_add(state: PickerState) -> str:
    def do() -> str | None:
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        alias, email, audience = prompt_new_profile_fields()
        if alias is None:
            return None
        rc = create_profile(alias, email=email, audience=audience, ask_email=False)
        return alias if rc == 0 else None

    new_alias = state.cooked_action(do)
    profiles = list_profiles()
    if new_alias and new_alias in profiles:
        state.idx = profiles.index(new_alias)
        return f"added profile {new_alias!r}."
    return "add cancelled or failed."


def _action_delete(state: PickerState, current: str) -> str:
    def do() -> bool:
        print()
        print(f"About to delete profile {current!r}:")
        print(f"  - removes {profile_dir(current)}")
        print("  - unregisters from profiles.conf")
        if launchd_is_scheduled(current):
            print("  - removes from launchd schedule")
        if not _confirm(f"delete {current!r}?"):
            return False
        ok, err = delete_profile(
            current,
            uninstall_launchd=True,
            promote_default=True,
        )
        if not ok:
            print(f"ERROR: {err}", file=sys.stderr)
            input("press enter to continue...")
            return False
        return True

    deleted = state.cooked_action(do)
    return f"deleted {current!r}." if deleted else "delete cancelled."


def _action_install(state: PickerState, current: str) -> str:
    def do() -> int:
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        rc = launchd_schedule(current)
        if rc == 0:
            print(f"\n{current!r} added to launchd schedule.")
        input("press enter to continue...")
        return rc

    rc = state.cooked_action(do)
    return f"{current!r} scheduled." if rc == 0 else f"scheduling {current!r} failed."


def _action_uninstall(state: PickerState, current: str) -> str:
    if not launchd_is_scheduled(current):
        return f"{current!r} is not scheduled."

    def do() -> int:
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        rc = launchd_unschedule(current)
        if rc == 0:
            print(f"\n{current!r} removed from launchd schedule.")
        input("press enter to continue...")
        return rc

    rc = state.cooked_action(do)
    return f"{current!r} unscheduled." if rc == 0 else f"unscheduling {current!r} failed."


def _action_reseed(state: PickerState, current: str) -> str:
    def do() -> int:
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        print(f"Reseeding {current!r}...\n")
        clear_cache()
        rc = do_reseed(current)
        print()
        input("press enter to continue...")
        return rc

    rc = state.cooked_action(do)
    return f"reseed succeeded for {current!r}." if rc == 0 else f"reseed failed for {current!r}."


def _action_open_edge(current: str) -> str:
    """Open a normal Edge window against <current>'s sidecar userdata dir
    and leave it running. No cooked-mode drop: open_edge is detached and
    returns immediately, so there's nothing to wait on - we stay in the
    picker and just report what happened on the status line.
    """
    from .capture import open_edge

    try:
        open_edge(current)
    except RuntimeError as e:
        return f"edge launch failed for {current!r}: {e}"
    return f"opened Edge for {current!r}; sign in, CLOSE Edge, then reseed (r)."


def _profile_config(alias: str) -> dict[str, str]:
    """This profile's config file as a dict, for the settings the dashboard
    shows and edits.

    load_config rather than parse_kv_stream: the latter allowlists the
    token-path keys only and drops empty values, both of which would hide
    the capture-mode keys (OWA_CAPTURE_HEADLESS_AT is deliberately empty
    when the mode is pinned). Nothing here writes the dict back, so the
    four env overrides load_config merges in are harmless.
    """
    config, _ = load_config(profile_config_path(alias))
    return config


def _headless_cell(config: dict[str, str]) -> tuple[str, str]:
    """(text, color) for the capture-mode column.

    'visible' is yellow because it is the mode that puts an Edge window on
    the user's screen, which is the thing they come to this column to
    find. A leading '~' means nobody pinned it: the mode is the default or
    the fallback's 24h auto-preference, so it can change on its own.
    """
    mode = "headless" if _headless_pref(config) else "visible"
    text = mode if _headless_pinned(config) else f"~{mode}"
    return text, DIM if mode == "headless" else YELLOW


def _action_toggle_headless(current: str) -> str:
    """Pin <current>'s capture mode to the opposite of what it is now.

    Writes OWA_CAPTURE_HEADLESS with an empty OWA_CAPTURE_HEADLESS_AT,
    which is how `reseed._headless_pinned` tells a deliberate choice from
    the fallback's self-expiring preference. save_config updates the two
    keys in place, so nothing else in the file is touched.
    """
    config = _profile_config(current)
    if os.environ.get("OWA_CAPTURE_HEADLESS", "").strip():
        return "OWA_CAPTURE_HEADLESS is set in the environment; unset it to pin per profile."
    headless = not _headless_pref(config)
    save_config(
        {
            "OWA_CAPTURE_HEADLESS": "1" if headless else "0",
            "OWA_CAPTURE_HEADLESS_AT": "",
            # Re-pinning is a fresh start: a headless failure streak from
            # the old pin must not keep overriding the new one.
            "OWA_HEADLESS_FAILS": "",
            "OWA_HEADLESS_FAILS_AT": "",
        },
        profile_config_path(current),
    )
    mode = "headless (no window)" if headless else "visible (offscreen window)"
    return f"{current!r} capture pinned to {mode}."


def _action_edit_config(state: PickerState, current: str) -> str:
    """Open <current>'s config.json in $EDITOR for anything the dashboard
    has no key for.

    The user edits a private copy, never the live file: tokens rotate while
    the editor is open, and a broken hand edit must not be able to replace
    the file that holds every token. On save the copy is validated (bad
    JSON -> re-edit or discard) and merged in with `merge_edit`, which
    applies only the keys and client records the user actually changed."""
    path = profile_config_path(current)

    def do() -> str:
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        try:
            base = read_doc(path, strict=True)
        except ConfigCorruptError as e:
            return f"config is unreadable, fix it by hand: {e}"
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        tmp = path.parent / ".config.edit.json"
        atomic_write(tmp, json.dumps(base, indent=2) + "\n")
        try:
            while True:
                try:
                    rc = subprocess.call([editor, str(tmp)])
                except OSError as e:
                    return f"could not run {editor!r}: {e}"
                if rc != 0:
                    return f"editor exited {rc}; nothing saved."
                try:
                    edited = validate_doc(json.loads(tmp.read_text()))
                except (json.JSONDecodeError, ConfigCorruptError) as e:
                    print(f"\ninvalid config: {e}", file=sys.stderr)
                    if input("[e]dit again or [d]iscard? ").strip().lower().startswith("e"):
                        continue
                    return "edit discarded; config unchanged."
                if edited == base:
                    return "no changes."

                def apply(live: dict[str, Any], e: dict[str, Any] = edited) -> None:
                    live.update(merge_edit(base, e, live))

                update_doc(path, apply)
                return f"edited {current!r} config."
        finally:
            tmp.unlink(missing_ok=True)

    return state.cooked_action(do)


# --- Client actions (cell cursor on a client column) --------------------


def _client_toggle(current: str, label: str, key: str | None, record: dict[str, Any] | None) -> str:
    if key is None:
        return "owa is the profile's own token; toggle the profile on the first column."
    if record is None:
        return f"{label} is not on {current!r}; a adds it."
    enabled = record.get("enabled", True) is False
    clients_mod.set_client_enabled(current, key, enabled)
    if enabled:
        return f"{label} enabled on {current!r}."
    tail = " owa-teams chats will fail on this profile." if label == "teams" else ""
    return f"{label} disabled on {current!r} (token kept).{tail}"


def _client_add(
    state: PickerState,
    current: str,
    label: str,
    key: str | None,
    record: dict[str, Any] | None,
    records_by_alias: dict[str, list[dict[str, Any]]],
) -> str:
    if key is None:
        return "owa is the profile's own token; nothing to add."
    if record is not None:
        if record.get("enabled", True) is False:
            return f"{label} is disabled; space enables it."
        return f"{label} is already on {current!r} (e edits, r captures)."

    def do() -> int:
        from .cli import add_client

        url = None
        if key in _URL_CLIENTS or key not in {k for _, k in KNOWN_COLUMNS}:
            suggested = _url_suggestion(key, records_by_alias, current)
            hint = f" [{suggested}]" if suggested else ""
            try:
                typed = input(f"{label} sign-in URL for {current!r}{hint}: ").strip()
            except (EOFError, KeyboardInterrupt):
                return 1
            url = clients_mod.normalize_capture_url(key, typed or suggested)
            if not url:
                print("no URL given.", file=sys.stderr)
                return 1
        rc = add_client(current, key, url)
        if rc != 0:
            input("press enter to continue...")
        return rc

    rc = state.cooked_action(do)
    if rc == 0:
        return f"{label} added to {current!r}."
    return f"{label}: not added / not captured yet."


def _client_edit(
    state: PickerState, current: str, label: str, key: str | None, record: dict[str, Any] | None
) -> str:
    if key is None or record is None:
        return f"{label}: nothing to edit here."
    if key in clients_mod.DECLARED_SERVICES or key == clients_mod.PIM_CLIENT_ID:
        return f"{label} has no sign-in URL to edit."

    def do() -> str:
        old = str(record.get("capture_url", ""))
        try:
            typed = input(f"{label} sign-in URL [{old}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "edit cancelled."
        url = clients_mod.normalize_capture_url(key, typed) if typed else old
        if url == old:
            return "URL unchanged."
        _, err = clients_mod.declare_client(current, key, capture_url=url)
        if err:
            return err
        if _confirm("capture now?"):
            from . import capture

            ok, _ = capture.capture_bound_clients(current, only=[key])
            return f"{label} URL set and captured." if ok else f"{label} URL set; capture failed."
        return f"{label} URL set; r captures."

    return state.cooked_action(do)


def _client_remove(
    state: PickerState, current: str, label: str, key: str | None, record: dict[str, Any] | None
) -> str:
    if key is None:
        return "owa is the profile's own token; d on the first column deletes the profile."
    if record is None:
        return f"{label} is not on {current!r}."
    if not state.cooked_action(
        lambda: _confirm(f"remove {label} from {current!r} (forgets its token)?")
    ):
        return "remove cancelled."
    clients_mod.forget_client(current, key)
    return f"{label} removed from {current!r}."


def _client_capture(
    state: PickerState, current: str, label: str, key: str | None, record: dict[str, Any] | None
) -> str:
    if key is None:
        return _action_reseed(state, current)
    if record is None:
        return f"{label} is not on {current!r}; a adds it."
    if record.get("enabled", True) is False:
        return f"{label} is disabled; space enables it."
    if key in clients_mod.DECLARED_SERVICES:
        return f"{label} is declared only; nothing to capture."
    if key == clients_mod.PIM_CLIENT_ID:
        return "pim refreshes on demand; re-sign in with `owa-piggy clients add pim`."

    def do() -> int:
        from . import capture

        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        print(f"Capturing {label} for {current!r}...\n")
        ok, _ = capture.capture_bound_clients(current, only=[key])
        if not ok:
            input("press enter to continue...")
        return ok

    return f"{label} captured." if state.cooked_action(do) else f"{label}: capture failed."


def _action_reseed_all(state: PickerState) -> str:
    """Shift-r: reseed every configured profile sequentially.

    Surfaces the same capability as `owa-piggy reseed --all` from inside
    the picker so the routine "Monday morning, all RTs are stale" flow
    doesn't require dropping out to the shell.
    """

    def do() -> int:
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
        print("Reseeding all profiles...\n")
        rc: int = do_reseed_all()
        print()
        input("press enter to continue...")
        return rc

    rc = state.cooked_action(do)
    return "reseed --all succeeded." if rc == 0 else "reseed --all failed (see above)."


def print_plain_list() -> int:
    """Plain printed listing of profiles, marking default with '*' and
    enabled-but-not-default with 'x'.

    Used by cli.`_do_profiles_list` for the non-TTY / pipe / redirect
    case when called without a freshness probe. For the dashboard's own
    fallback (alias + token freshness) see `print_plain_status`.
    """
    profiles = list_profiles()
    reg = load_profiles_conf()
    default = reg["OWA_DEFAULT_PROFILE"]
    enabled = set(reg["OWA_PROFILES"])
    scheduled = set(reg.get("OWA_SCHEDULED", []))
    for alias in profiles:
        marker = "*" if alias == default else ("x" if alias in enabled else " ")
        sched = " (S)" if alias in scheduled else ""
        print(f" {marker} {alias}{sched}")
    return 0


# --- Dashboard (token health) ------------------------------------------


def _freshness_cell(report: dict[str, Any] | None) -> tuple[str, str]:
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
        return "probing...", DIM

    from .status import _humanize_minutes

    st = report.get("state", "fail")
    if st == "disabled":
        return "disabled", DIM

    at = report.get("access_token") or {}
    mins = at.get("minutes_remaining")

    if st == "ok":
        text = f"fresh {_humanize_minutes(mins)}" if mins is not None else "fresh"
        return text, GREEN
    if st == "warn":
        text = f"expiring {_humanize_minutes(mins)}" if mins is not None else "expiring"
        return text, YELLOW

    # fail: surface the first hint so the user knows the fix, truncated so
    # one broken profile can't blow out the row width.
    hints = report.get("hints") or []
    label = hints[0] if hints else "needs reseed (r)"
    if len(label) > 44:
        label = label[:43] + "..."
    return label, RED


# --- Client columns ----------------------------------------------------
# One column per service a profile can sign in to. `key` is how clients.py
# addresses the record (client id, else service name); None is the
# profile's own FOCI token, which isn't a clients[] record.

KNOWN_COLUMNS: tuple[tuple[str, str | None], ...] = (
    ("owa", None),
    ("teams", clients_mod.TEAMS_WEB_CLIENT_ID),
    ("ado", DEVOPS_CLIENT_ID),
    ("halo", clients_mod.HALO_KEY),
    ("swodp", "swodp"),
    ("kova", clients_mod.KOVA_KEY),
    ("pim", clients_mod.PIM_CLIENT_ID),
)

# SPA-bound AAD refresh tokens die 24h after issue; reseed renews them
# hourly, so an older stamp means reseed has been failing for that client.
_SPA_RT_MAX_AGE_S = 24 * 3600

# Clients whose sign-in URL is org-specific: adding one asks for it.
_URL_CLIENTS = frozenset({DEVOPS_CLIENT_ID, clients_mod.HALO_KEY})


def _client_columns(
    records_by_alias: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, str | None]]:
    """Every known column, then any unknown client some profile has (in
    first-seen order), labelled by its name's first 6 characters."""
    columns = list(KNOWN_COLUMNS)
    known = {key for _, key in columns}
    for records in records_by_alias.values():
        for record in records:
            key = clients_mod.record_key(record)
            if key not in known:
                known.add(key)
                columns.append((str(record["name"])[:6], key))
    return columns


def _client_cell(
    key: str | None,
    record: dict[str, Any] | None,
    config: dict[str, str],
    now: float,
) -> tuple[str, str, str]:
    """(glyph, color, detail) for one profile x client cell. Pure.

    ● green  in use and healthy        ● yellow  added, needs attention
    ○ dim    disabled (token kept)     · dim     not added
    """
    if classify_profile_type(config) != "m365":
        return " ", DIM, "not an AAD profile"
    if key is None:
        if config.get("OWA_REFRESH_TOKEN"):
            return "●", GREEN, "the profile's own token"
        return "·", DIM, "no profile token (setup)"
    if record is None:
        return "·", DIM, "not added (a adds it)"
    if record.get("enabled", True) is False:
        return "○", DIM, "disabled; token kept (space enables)"
    if key in clients_mod.DECLARED_SERVICES:
        return "●", GREEN, "declared (nothing to capture)"
    issued = record.get("rt_issued_at", "")
    if key in clients_mod.SESSION_SERVICES:
        if issued:
            return "●", GREEN, f"signed in {issued}"
        return "●", YELLOW, "not signed in yet (r captures)"
    if not record.get("refresh_token"):
        return "●", YELLOW, "added, not captured yet (r captures)"
    dt = parse_iso_utc(issued)
    spa = bool(record.get("client_id")) and key != clients_mod.PIM_CLIENT_ID
    if spa and dt is not None and now - dt.timestamp() > _SPA_RT_MAX_AGE_S:
        return "●", YELLOW, f"token from {issued}: older than 24h, reseed failing?"
    return "●", GREEN, f"token from {issued or 'unknown'}"


def _find_record(records: list[dict[str, Any]], key: str | None) -> dict[str, Any] | None:
    if key is None:
        return None
    return next((r for r in records if clients_mod.record_key(r) == key), None)


def _url_suggestion(
    key: str, records_by_alias: dict[str, list[dict[str, Any]]], current: str
) -> str:
    """The URL this client uses on another profile (same org, most likely)."""
    for alias, records in records_by_alias.items():
        record = _find_record(records, key)
        if alias != current and record and record.get("capture_url"):
            return str(record["capture_url"])
    return ""


def _cells_row(
    columns: list[tuple[str, str | None]],
    records: list[dict[str, Any]],
    config: dict[str, str],
    now: float,
    selected: int | None = None,
) -> str:
    """The client cells of one dashboard row; `selected` is the 0-based
    column under the cell cursor (reverse video), or None."""
    out = []
    for i, (label, key) in enumerate(columns):
        glyph, color, _ = _client_cell(key, _find_record(records, key), config, now)
        pad = " " * max(0, len(label) - 1)
        mark = REVERSE if i == selected else ""
        out.append(f"{mark}{color}{glyph}{RESET}{pad}")
    return " ".join(out)


def _cells_header(columns: list[tuple[str, str | None]]) -> str:
    return " ".join(label for label, _ in columns)


def print_plain_status(
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
) -> int:
    """Non-TTY fallback for `owa-piggy tui`: one line per profile with its
    token freshness, no escapes.

    Used when termios is unavailable or stdin/stdout isn't a TTY (pipes,
    CI, redirects). Shares `_freshness_cell` with the interactive dashboard
    so the two output paths cannot drift.
    """
    profiles = list_profiles()
    if not profiles:
        print("no profiles configured. Run: owa-piggy setup --profile <alias>")
        return 0
    from . import status as status_mod

    data = status_mod.status_all_report(
        audience=audience, scope=scope, sharepoint_tenant=sharepoint_tenant
    )
    reg = load_profiles_conf()
    default = reg["OWA_DEFAULT_PROFILE"]
    reports = {r["profile"]: r for r in data["profiles"]}
    width = max((len(a) for a in profiles), default=0)
    for alias in profiles:
        marker = "*" if alias == default else " "
        text, _color = _freshness_cell(reports.get(alias))
        mode, _mode_color = _headless_cell(_profile_config(alias))
        print(f" {marker} {alias.ljust(width)}  {mode.ljust(9)}  {text}")
    return 0


def run_dashboard(
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
) -> int:
    """Interactive token-health dashboard for `owa-piggy tui`.

    Also the screen bare `owa-piggy profiles` opens on a TTY. Combines the
    profile list, markers, and single-key registry actions with a
    per-profile token-freshness column driven by a live
    `status.status_all_report` probe. Keys:

      up/down or j/k   navigate
      left/right       move the cell cursor across the service columns
      space            toggle enabled (registered in OWA_PROFILES)
      enter            set highlighted profile default
      a                add a new profile
      d                delete profile
      l / u            add / remove from launchd schedule
      r                reseed highlighted profile        (re-probes)
      R                reseed every profile               (re-probes)
      e                open Edge for highlighted profile's sidecar session
      h                pin capture mode: headless <-> visible (offscreen)
      c                edit the highlighted profile's config in $EDITOR
      g                refresh token health (re-probe)
      q / esc          quit

    With the cell cursor on a service column, `space` enables/disables that
    service, `a` adds it, `e` edits its URL, `d` removes it and `r` captures
    it (the `_client_*` actions); every other key keeps its profile meaning.

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
        return print_plain_status(
            audience=audience, scope=scope, sharepoint_tenant=sharepoint_tenant
        )
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return print_plain_status(
            audience=audience, scope=scope, sharepoint_tenant=sharepoint_tenant
        )

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    state = PickerState(fd, old)
    # alias -> status report, populated by reprobe(). Stored on state so the
    # cooked-mode action helpers can't see a stale closure.
    state.reports = {}

    def load_state() -> tuple[list[str], str, set[str]]:
        profiles = list_profiles()
        reg = load_profiles_conf()
        return profiles, reg["OWA_DEFAULT_PROFILE"], set(reg["OWA_PROFILES"])

    def clamp_cursor(profiles: list[str]) -> None:
        if not profiles:
            state.idx = 0
        elif state.idx >= len(profiles):
            state.idx = len(profiles) - 1
        elif state.idx < 0:
            state.idx = 0

    def refresh() -> None:
        from . import status as status_mod

        data = status_mod.status_all_report(
            audience=audience, scope=scope, sharepoint_tenant=sharepoint_tenant
        )
        state.reports = {r["profile"]: r for r in data["profiles"]}

    def load_clients_view(
        profiles: list[str],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[tuple[str, str | None]]]:
        records = {alias: clients_mod.load_records(alias) for alias in profiles}
        columns = _client_columns(records)
        state.col = max(0, min(state.col, len(columns)))
        return records, columns

    def draw() -> None:
        profiles, default, enabled = load_state()
        clamp_cursor(profiles)
        records_by_alias, columns = load_clients_view(profiles)
        now = time.time()
        scheduled = set(load_profiles_conf().get("OWA_SCHEDULED", []))
        launchd_state = {alias: alias in scheduled for alias in profiles}
        width = max((len(a) for a in profiles), default=0)
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.write("owa-piggy dashboard\r\n")
        if state.col == 0:
            sys.stdout.write(
                f"  {DIM}"
                "up/down  navigate  ·  left/right  services  ·  space toggle  ·  "
                "enter set default\r\n"
                "  a add  ·  d delete  ·  l schedule  ·  u unschedule  ·  r reseed  ·  "
                "R reseed all  ·  g refresh\r\n"
                "  e edge  ·  h headless/visible  ·  c edit config  ·  q quit"
                f"{RESET}\r\n\r\n"
            )
        else:
            label = columns[state.col - 1][0]
            sys.stdout.write(
                f"  {DIM}"
                f"service {label}:  space enable/disable  ·  a add  ·  e edit URL  ·  "
                "d remove  ·  r capture\r\n"
                "  up/down  navigate  ·  left/right  services  ·  enter set default  ·  "
                "g refresh\r\n"
                "  l/u schedule  ·  h headless/visible  ·  c edit config  ·  q quit"
                f"{RESET}\r\n\r\n"
            )
        if not profiles:
            sys.stdout.write('  (no profiles - press "a" to add one, q to quit)\r\n')
        else:
            # Row prefix: " > [x] <alias><(S) or 4 spaces>  <mode 9>  "
            sys.stdout.write(f"{DIM}{' ' * (width + 24)}{_cells_header(columns)}{RESET}\r\n")
            for i, alias in enumerate(profiles):
                cursor = ">" if i == state.idx else " "
                if alias == default:
                    state_marker = f"{GREEN}*{RESET}"
                elif alias in enabled:
                    state_marker = f"{GREEN}x{RESET}"
                else:
                    state_marker = f"{DIM} {RESET}"
                launchd_marker = f" {CYAN}(S){RESET}" if launchd_state[alias] else "    "
                text, color = _freshness_cell(state.reports.get(alias))
                cell = f"{color}{text}{RESET}"
                config = _profile_config(alias)
                mode_text, mode_color = _headless_cell(config)
                mode_cell = f"{mode_color}{mode_text.ljust(9)}{RESET}"
                selected = state.col - 1 if i == state.idx and state.col else None
                cells = _cells_row(columns, records_by_alias[alias], config, now, selected)
                row_mark = REVERSE if i == state.idx and state.col == 0 else ""
                sys.stdout.write(
                    f" {cursor} [{state_marker}] {row_mark}{alias.ljust(width)}{RESET}"
                    f"{launchd_marker}  {mode_cell}  {cells}  {cell}{CLEAR_EOL}\r\n"
                )
        sys.stdout.write("\r\n")
        if state.message:
            sys.stdout.write(f"  {state.message}{CLEAR_EOL}\r\n")
        elif profiles and state.col:
            alias = profiles[state.idx]
            label, key = columns[state.col - 1]
            record = _find_record(records_by_alias[alias], key)
            _, _, detail = _client_cell(key, record, _profile_config(alias), now)
            url = (record or {}).get("capture_url", "")
            extra = f" · {url}" if url else ""
            sys.stdout.write(f"  {DIM}{alias} · {label}: {detail}{extra}{RESET}{CLEAR_EOL}\r\n")
        else:
            sys.stdout.write(f"{CLEAR_EOL}\r\n")
        sys.stdout.flush()

    def reprobe() -> None:
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

            if ch in ("q", "Q", "\x03"):
                break
            if ch == "\x1b":
                seq = sys.stdin.read(1)
                if seq == "[":
                    arrow = sys.stdin.read(1)
                    if arrow == "A":
                        state.idx = max(0, state.idx - 1)
                    elif arrow == "B":
                        state.idx = min(max(0, len(profiles) - 1), state.idx + 1)
                    elif arrow == "C":
                        state.col += 1  # draw() clamps to the column count
                    elif arrow == "D":
                        state.col = max(0, state.col - 1)
                    state.message = ""
                    draw()
                    continue
                # Bare ESC = quit.
                break
            if ch == "k":
                state.idx = max(0, state.idx - 1)
                state.message = ""
                draw()
                continue
            if ch == "j":
                state.idx = min(max(0, len(profiles) - 1), state.idx + 1)
                state.message = ""
                draw()
                continue

            if ch in ("g", "G"):
                state.message = ""
                reprobe()
                continue

            if state.col and current and ch in (" ", "a", "e", "d", "r"):
                # Cell cursor on a client column: these keys act on that
                # service for the highlighted profile, not on the profile.
                records_by_alias, columns = load_clients_view(profiles)
                if state.col:
                    label, key = columns[state.col - 1]
                    record = _find_record(records_by_alias[current], key)
                    if ch == " ":
                        state.message = _client_toggle(current, label, key, record)
                    elif ch == "a":
                        state.message = _client_add(
                            state, current, label, key, record, records_by_alias
                        )
                    elif ch == "e":
                        state.message = _client_edit(state, current, label, key, record)
                    elif ch == "d":
                        state.message = _client_remove(state, current, label, key, record)
                    else:
                        state.message = _client_capture(state, current, label, key, record)
                    draw()
                    continue

            if ch == "a":
                state.message = _action_add(state) or ""
                reprobe()
                continue

            if ch == "R":
                state.message = _action_reseed_all(state) or ""
                reprobe()
                continue

            if not current:
                # All remaining keys need a selected profile.
                state.message = "no profile selected."
                draw()
                continue

            if ch == " ":
                state.message = _action_toggle(current, enabled) or ""
                reprobe()
                continue

            if ch in ("\r", "\n"):
                state.message = _action_set_default(current, default) or ""
                draw()
                continue

            if ch == "d":
                state.message = _action_delete(state, current) or ""
                draw()
                continue

            if ch == "l":
                state.message = _action_install(state, current) or ""
                draw()
                continue

            if ch == "u":
                state.message = _action_uninstall(state, current) or ""
                draw()
                continue

            if ch == "r":
                state.message = _action_reseed(state, current) or ""
                reprobe()
                continue

            if ch == "e":
                state.message = _action_open_edge(current) or ""
                draw()
                continue

            if ch == "h":
                state.message = _action_toggle_headless(current) or ""
                draw()
                continue

            if ch == "c":
                state.message = _action_edit_config(state, current) or ""
                reprobe()
                continue

            # Unknown key - just clear any stale message.
            state.message = ""
            draw()
    except KeyboardInterrupt:
        # SIGINT mid-probe or mid-action (cooked mode) quits like q: the
        # finally below restores the terminal, no traceback.
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        state.restore()
    # Move cursor to the bottom on exit so the next shell prompt does not
    # overwrite the last frame.
    print()
    return 0
