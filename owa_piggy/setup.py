"""Interactive first-time setup and the raw-tty input helper.

read_input() bypasses cooked-mode line-length limits so pasted refresh
tokens (which can exceed 4KB) don't get truncated. interactive_setup()
is the `setup` subcommand's flow; it also parses piped stdin so
`pbpaste | owa-piggy setup` works.

When called with `email=...` the setup flow shells Edge out and captures
the /token response off the wire (see capture.py). This is the path for
tenants whose SPA uses MSAL.js encrypted-cache (e.g. Okta-federated
accounts where the legacy localStorage scrape returns an opaque blob).
"""

from __future__ import annotations

import sys

from . import config as _config
from .config import iso_utc_now, parse_kv_stream, save_config

# Some tests monkeypatch setup.CONFIG_PATH directly (legacy fixture behavior).
# Expose it as a module attribute so those patches keep working, but read
# `_config.CONFIG_PATH` at call time everywhere it matters so the active
# profile's path is always current.
CONFIG_PATH = _config.CONFIG_PATH


def read_input(prompt: str, secret: bool = False) -> str:
    """Read input in raw tty mode to bypass the terminal line-length limit (~4096 bytes).

    Modern terminals wrap pasted text with bracketed-paste escape sequences
    (ESC [200~ ... ESC [201~). In cooked mode the terminal strips these; in
    raw mode they leak through as literal bytes and corrupt the payload, which
    for a refresh token means AAD rejects the exchange as malformed. We detect
    the BP start/end sequences and drop them, and strip any stray CSI escape.

    When secret=True, characters are not echoed and backspace does not emit
    visual feedback."""
    import re

    print(prompt)
    sys.stdout.flush()
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        chars: list[str] = []
        in_paste = False
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                # Handle CSI escape sequences (bracketed paste + anything else)
                if ch == "\x1b":
                    seq = ch + sys.stdin.read(1)  # expect '['
                    if seq == "\x1b[":
                        tail = ""
                        while True:
                            c = sys.stdin.read(1)
                            tail += c
                            if c.isalpha() or c == "~":
                                break
                        full = seq + tail
                        if full == "\x1b[200~":
                            in_paste = True
                        elif full == "\x1b[201~":
                            in_paste = False
                        # drop any other CSI sequence silently
                    continue
                if ch in ("\r", "\n"):
                    # Inside a pasted block, a newline is data, not submit.
                    if in_paste:
                        continue  # silently drop embedded newlines
                    if chars:
                        break
                    continue
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch in ("\x7f", "\x08"):  # backspace / ctrl-H
                    if chars and not in_paste:
                        chars.pop()
                        if not secret:
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                    continue
                if ord(ch) < 0x20:
                    continue  # drop other control chars (tabs, etc.)
                chars.append(ch)
                if not secret:
                    sys.stdout.write(ch)
                    sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()
        # Belt-and-suspenders: strip any residual CSI sequence that slipped
        # through a partial read, then trim whitespace.
        cleaned = re.sub(r"\x1b\[[\d;]*[ -/]*[@-~]", "", "".join(chars))
        return cleaned.strip()
    except ImportError:
        # No termios on this platform (Windows, etc.). Fall back to
        # cooked-mode input. Other errors (e.g. termios.error from a
        # non-tty stdin) propagate so a real failure is not silently
        # masked as "input got cooked-mode-truncated".
        if secret:
            import getpass

            return getpass.getpass("").strip()
        return input().strip()


def interactive_setup(
    config: dict[str, str],
    alias: str = "default",
    *,
    email: str | None = None,
    trough_url: str | None = None,
    trough_tenant: str | None = None,
    trough_sub: str | None = None,
    user_agent: str | None = None,
    google: bool = False,
    google_client_id: str | None = None,
    google_client_secret: str | None = None,
) -> bool:
    """Run the setup flow for profile <alias>. `CONFIG_PATH` must already
    be pointing at that profile's config file (caller's job, typically
    via `config.set_active_profile(alias)`).

    The alias is used only for user-facing labeling so whoever is
    setting up multiple tenants can tell which one they're typing into.

    When `email` is set, route to the network-capture path: launch Edge
    visibly, let the user complete sign-in (Okta / AAD / Verify push
    happens in that window), and intercept the /oauth2/v2.0/token
    response. This is the only way to onboard tenants whose MSAL.js
    cache is encrypted, since the localStorage paste path can no longer
    read the RT in plaintext there.

    When `trough_url` is set, route to the tailnet-capture path: pull
    the freshest FOCI refresh token for `trough_tenant` (or `trough_sub`,
    or the most recent overall) from a trough appliance's HTTP API and
    seed the profile non-interactively. This is the consumer half of the
    iPhone-routed-through-trough flow - no browser involvement on this
    machine.
    """
    # Persist the UA up front so all three branches (trough/email/paste)
    # write it alongside any tokens they capture; reseed.py later reads
    # OWA_USER_AGENT to keep silent refresh runs UA-consistent with the
    # original sign-in.
    if user_agent:
        config["OWA_USER_AGENT"] = user_agent
    if trough_url is not None:
        return _trough_setup(config, alias, trough_url, tenant=trough_tenant, sub=trough_sub)
    if email is not None:
        return _capture_setup(config, alias, email, user_agent=user_agent)
    if google:
        return _google_setup(config, alias, google_client_id, google_client_secret)

    # Non-interactive path: if stdin is piped, parse KEY=value lines from it.
    # This avoids the bracketed-paste corruption that raw-tty input is prone
    # to with very long secrets, and pairs directly with the JS snippet's
    # KEY=value output (e.g. `pbpaste | owa-piggy setup`).
    if not sys.stdin.isatty():
        parsed = parse_kv_stream(sys.stdin.read())
        if not parsed.get("OWA_REFRESH_TOKEN") or not parsed.get("OWA_TENANT_ID"):
            print(
                "ERROR: stdin missing OWA_REFRESH_TOKEN and/or OWA_TENANT_ID. "
                "Expected KEY=value lines as printed by the browser snippet.",
                file=sys.stderr,
            )
            return False
        config.update(parsed)
        # Stamp issuance time so `status` can show the 24h hard-cap. This is
        # set on setup/reseed paths only, never on ordinary rotation (which
        # does not reset the SPA hard-cap timer).
        config["OWA_RT_ISSUED_AT"] = iso_utc_now()
        save_config(config)
        _ensure_edge_profile_dir(alias)
        print(f"Config saved to {_config.CONFIG_PATH} [profile={alias}]", file=sys.stderr)
        return True

    print(f"owa-piggy setup [profile={alias}]\n")
    print("1. Open https://outlook.cloud.microsoft in Microsoft Edge")
    print("   (plain Chromium browsers store a session-bound token that")
    print("    AAD rejects as malformed - seed from Edge only.)")
    print("2. Open DevTools (F12) > Console")
    print("3. Paste this snippet to print both values:\n")
    print("   const find = s => Object.keys(localStorage).find(k => k.includes(s))")
    print("   const parse = s => JSON.parse(localStorage[find(s)])")
    print("   const rt = parse('|refreshtoken|'), it = parse('|idtoken|')")
    print("   if (!rt.secret) console.warn('WARN: non-MSAL shape.')")
    # Single console.log so the "VMXXX:X" line that the devtools console
    # appends after the first call does not end up copied into the paste.
    print(
        "   console.log(`OWA_REFRESH_TOKEN=${rt.secret || rt.data}\\n"
        "OWA_TENANT_ID=${(it.realm || find('|idtoken|').split('|')[5])}`)\n"
    )
    print("   Tip: to avoid terminal paste-corruption on very long tokens,")
    print("   copy the two output lines and pipe them in instead:")
    print(f"     pbpaste | owa-piggy setup --profile {alias}\n")
    rt = read_input(
        f'[{alias}] Refresh token (starts with "1.AQ..."), then Enter (input hidden):', secret=True
    )
    if not rt:
        print("ERROR: no refresh token provided", file=sys.stderr)
        return False

    tid = read_input(f"[{alias}] Tenant ID (a UUID), then Enter:")
    if not tid:
        print("ERROR: no tenant ID provided", file=sys.stderr)
        return False

    config["OWA_REFRESH_TOKEN"] = rt
    config["OWA_TENANT_ID"] = tid
    config["OWA_RT_ISSUED_AT"] = iso_utc_now()
    save_config(config)
    _ensure_edge_profile_dir(alias)
    print(f"\nConfig saved to {_config.CONFIG_PATH} [profile={alias}]")
    return True


def _ensure_edge_profile_dir(alias: str) -> None:
    """Create the per-profile Edge sidecar userdata dir if missing.

    Each profile needs its own dir so `reseed --profile <alias>` can
    drive Edge headlessly without clobbering another profile's cookies.
    First-ever reseed will still trigger an interactive sign-in because
    the dir starts empty; thereafter the existing session is reused."""
    d = _config.profile_edge_dir(alias)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)


def _capture_setup(
    config: dict[str, str], alias: str, email: str, *, user_agent: str | None = None
) -> bool:
    """Drive Edge visibly, capture the FOCI refresh token off the wire,
    and persist it to the profile config.

    This path is required for tenants whose MSAL.js cache is encrypted
    (e.g. Okta-federated accounts on newer MSAL releases). The plain
    localStorage paste path returns an AES-GCM envelope, not an RT, so
    the network-capture detour is the only way in.

    Imported lazily so a `setup` invocation that uses the legacy paste
    flow does not pay the cost of importing the CDP machinery.
    """
    # Local import: keeps cdp/capture out of the import graph for the
    # paste-only path used by every existing profile.
    from . import capture

    print(f"owa-piggy setup [profile={alias}, email={email}]\n", file=sys.stderr)
    print("Opening Microsoft Edge so you can sign in.", file=sys.stderr)
    print("owa-piggy will capture the refresh token off the wire and close", file=sys.stderr)
    print("the window automatically once authentication completes.\n", file=sys.stderr)

    try:
        captured = capture.capture_signin(alias, email, user_agent=user_agent)
    except TimeoutError:
        print(
            "ERROR: timed out waiting for sign-in to complete. Re-run setup --email to try again.",
            file=sys.stderr,
        )
        return False
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False
    except KeyboardInterrupt:
        print("\nABORTED.", file=sys.stderr)
        return False

    config.update(captured)
    config["OWA_RT_ISSUED_AT"] = iso_utc_now()
    save_config(config)
    print(f"Config saved to {_config.CONFIG_PATH} [profile={alias}]", file=sys.stderr)
    return True


def _google_setup(
    config: dict[str, str], alias: str, client_id: str | None, client_secret: str | None
) -> bool:
    """Run Google's installed-app consent flow and persist the resulting
    refresh token to the profile config.

    Unlike the MSAL paths above, owa-piggy owns a real OAuth client here,
    so this is a normal browser consent screen - no Edge, no CDP.

    Imported lazily so a `setup` invocation that never touches Google does
    not pay the import cost.
    """
    from . import oauth_google

    if not client_id or not client_secret:
        print(
            "ERROR: --google requires --google-client-id and "
            "--google-client-secret (from a Google Cloud OAuth client).",
            file=sys.stderr,
        )
        return False

    print(f"owa-piggy setup [profile={alias}, provider=google]\n", file=sys.stderr)
    print("Opening your browser to sign in to Google.", file=sys.stderr)

    try:
        tokens = oauth_google.run_local_consent_flow(client_id, client_secret)
    except oauth_google.ConsentError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False
    except KeyboardInterrupt:
        print("\nABORTED.", file=sys.stderr)
        return False

    if not tokens.get("refresh_token"):
        print(
            "ERROR: Google returned no refresh_token. Revoke prior access "
            "at https://myaccount.google.com/permissions and re-run setup.",
            file=sys.stderr,
        )
        return False

    config["OWA_PROVIDER"] = "google"
    config["OWA_CLIENT_ID"] = client_id
    config["OWA_CLIENT_SECRET"] = client_secret
    config["OWA_REFRESH_TOKEN"] = tokens["refresh_token"]
    # No OWA_RT_ISSUED_AT: that field only means something as the start of
    # AAD's 24h SPA hard-cap window (see status._rt_expires_at). Google
    # refresh tokens don't expire on a schedule, so leave it unset rather
    # than have status.py render a bogus countdown.
    save_config(config)
    print(f"Config saved to {_config.CONFIG_PATH} [profile={alias}]", file=sys.stderr)
    return True


def _trough_setup(
    config: dict[str, str],
    alias: str,
    trough_url: str,
    *,
    tenant: str | None = None,
    sub: str | None = None,
) -> bool:
    """Seed the profile from a tailnet-side trough appliance.

    Imported lazily so a `setup` invocation that does not touch the
    network adapter does not pay the import cost.
    """
    from . import trough

    print(f"owa-piggy setup [profile={alias}, trough={trough_url}]\n", file=sys.stderr)
    filt = []
    if tenant:
        filt.append(f"tenant={tenant}")
    if sub:
        filt.append(f"sub={sub}")
    if filt:
        print(f"Filtering by {' '.join(filt)}.", file=sys.stderr)
    else:
        print("No tenant/sub filter - taking the freshest FOCI RT in the trough.", file=sys.stderr)

    try:
        rt, tid, info = trough.fetch_foci(trough_url, tenant=tenant, sub=sub)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False

    print(
        f"Captured FOCI RT: tid={tid} sub={info['sub']} "
        f"src_host={info['src_host']} bytes={info['token_len']} "
        f"last_seen={info['last_seen']}",
        file=sys.stderr,
    )

    config["OWA_REFRESH_TOKEN"] = rt
    config["OWA_TENANT_ID"] = tid
    config["OWA_RT_ISSUED_AT"] = iso_utc_now()
    save_config(config)
    _ensure_edge_profile_dir(alias)
    print(f"Config saved to {_config.CONFIG_PATH} [profile={alias}]", file=sys.stderr)
    return True


# --- interactive service selection -------------------------------------
#
# A profile is one identity that may sign in to several SPAs (see
# clients.py). Asking about them here - before any browser opens - keeps
# the whole flow to "answer three questions, then sign in N times", rather
# than making the user remember `--with-client devops=<long url>`.


def _ask(question: str, *, default: bool) -> bool:
    """Yes/no prompt. Enter takes the default; EOF/Ctrl-C takes it too, so a
    closed stdin mid-setup cannot hang or crash the flow."""
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def prompt_for_clients(alias: str) -> list[str]:
    """Ask which services this identity signs in to.

    Returns `--with-client`-style specs, so the prompt and the flag feed the
    same code path and cannot drift. Teams defaults to yes: it is the client
    that makes chatsvc, presence and the middle tier reachable at all, and
    its sign-in URL is the same for every tenant. Azure DevOps defaults to
    no and asks for an org, since its URL is org-specific.
    """
    from . import clients as clients_mod

    print(
        f"\nServices for profile {alias!r} - one browser sign-in each, "
        f"same account:\n  Outlook / Microsoft 365 is always included.",
        file=sys.stderr,
    )
    specs: list[str] = []
    if _ask("Teams as well? (chats, presence, channels)", default=True):
        specs.append("teams")
    if _ask("Azure DevOps as well?", default=False):
        while True:
            try:
                org = input("    org name or full URL: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not org:
                print("    skipped Azure DevOps (no org given).", file=sys.stderr)
                break
            url = clients_mod.normalize_capture_url(clients_mod.DEVOPS_CLIENT_ID, org)
            print(f"    -> {url}", file=sys.stderr)
            specs.append(f"devops={org}")
            break
    return specs


def prompt_for_email(alias: str) -> str | None:
    """Ask for the sign-in address, mirroring the TUI's add-profile prompt.

    Blank means the legacy paste flow, which is still the fast path on plain
    MSAL tenants - so the question has to allow an empty answer rather than
    insisting.
    """
    while True:
        try:
            email = input(
                f"[{alias}] email address for Edge sign-in capture (blank = paste flow): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not email or "@" in email:
            return email or None
        print("  enter an email address, or leave blank for the paste flow.", file=sys.stderr)
