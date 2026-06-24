"""Drive Edge via CDP and capture the FOCI refresh token off the wire.

This is the path for tenants whose SPA encrypts the MSAL token cache
(MSAL.js v3+ cacheEncryption=true). The encrypted-cache envelope
in localStorage is `{id, nonce, data}` with the plaintext RT inside
`data` wrapped by an AES-GCM key whose CryptoKey is non-extractable
in IndexedDB. We can't decrypt that from outside the browser context,
so instead we intercept the `/oauth2/v2.0/token` response body in
flight, before MSAL ever encrypts it.

Two flows:

* `capture_signin(alias, email)` - first-time onboarding. Launches
  Edge **visibly** so the user can complete sign-in (Okta password +
  Verify push, AAD MFA, whatever the tenant requires). Auto-closes
  Edge the moment a token response with `refresh_token` lands.

* `capture_silent(alias)` - scheduled reseed. Launches Edge **headless**.
  Forces MSAL to refresh by clearing access-token entries from
  localStorage and reloading the page; MSAL then POSTs to /token with
  its in-memory RT to get a new AT (and a rotated RT, which is what we
  want). No window flashes onscreen.

The localStorage wipe is selective: we keep RT and idToken entries so
MSAL still has something to refresh from. Without the wipe, MSAL serves
the cached AT in memory and never makes a /token call - the entire
point of this dance.
"""

import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import config as _config
from .cdp import CdpError, CdpSession, find_tab
from .jwt import decode_jwt_segment

# Match either OWA host the user might have in their bookmarks. The SPA
# at outlook.cloud.microsoft is the canonical post-2024 home; office.com
# is the legacy domain that still resolves and triggers the same auth.
OWA_URL = "https://outlook.cloud.microsoft"

# Substring match for the AAD token endpoint. We deliberately do NOT
# pin to login.microsoftonline.com - some sovereign clouds (USGov, China)
# use different login hosts but the same /oauth2/v2.0/token suffix.
TOKEN_PATH_SUFFIX = "/oauth2/v2.0/token"

# Edge binaries we know about. macOS first since this tool is macOS-first;
# the Linux paths exist for the rare dev who runs the test suite on Linux.
_EDGE_CANDIDATES = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
)


def find_edge():
    """Return the path to a Microsoft Edge binary, or None if absent.

    PATH lookup last so a brew-installed `microsoft-edge` shim doesn't
    win over the canonical .app bundle on macOS."""
    for path in _EDGE_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    on_path = shutil.which("microsoft-edge") or shutil.which("msedge")
    return on_path


def find_free_port():
    """Bind to port 0 to let the kernel pick an unused local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch_edge(edge_dir, port, *, headless, url, edge_path=None, offscreen=False, user_agent=None):
    """Launch Edge with a per-profile userdata dir + CDP listening on
    `port`. Returns the Popen handle.

    Headless uses --headless=new (the Chromium replacement for the old
    --headless=true) so the runtime is closer to a real browser - some
    SPAs detect the legacy headless and refuse to load.

    `offscreen=True` parks the (real) window at -32000,-32000 in non-
    headless mode so silent reseed fallbacks don't pop a visible window
    onscreen. Headless is implicitly offscreen. Visible (sign-in) mode
    is the only one that puts the window where the user can see and
    interact with it."""
    binary = edge_path or find_edge()
    if not binary:
        raise RuntimeError(
            "Microsoft Edge not found. Tried: "
            + ", ".join(_EDGE_CANDIDATES)
            + " and PATH lookup. Install Edge or set the path manually."
        )
    args = [
        binary,
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={edge_dir}",
    ]
    if headless:
        args += [
            "--headless=new",
            "--window-position=-32000,-32000",
            "--window-size=1,1",
        ]
    elif offscreen:
        args += ["--window-position=-32000,-32000", "--window-size=1,1"]
    else:
        args += ["--window-position=100,100", "--window-size=900,750"]
    if user_agent:
        # Spoof the UA before any navigation so AAD's first request hits
        # the override. Tenant CA policies that gate on platform (e.g.
        # "compliant device required except iOS Teams") can be satisfied
        # by claiming to be TeamsMobile-iOS from a desktop Edge.
        args.append(f"--user-agent={user_agent}")
    args.append(url)
    # Detach stdout/stderr so a slow CDP consumer can't backpressure
    # Edge into a hang; Edge's own crash logs land in the userdata dir.
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_edge(alias, *, url=None):
    """Launch a normal, interactive Edge window bound to <alias>'s sidecar
    userdata dir and return (Popen, edge_dir). Does NOT capture, reload, or
    auto-close anything - this is the "just open my profile's browser" path.

    Why a separate launcher from `launch_edge`: the capture flows attach a
    CDP debugger (--remote-debugging-port), park the window offscreen, and
    tear Edge down the moment a /token response lands. Here we want the
    opposite - a real window the user drives by hand, left running after we
    exit. So no debug port, no window-position games, and crucially
    start_new_session=True so the browser survives this process exiting and
    is detached from the terminal's process group (Ctrl-C in owa-piggy or
    closing the shell must not take Edge down with it).

    The point is persistence: signing in here writes session cookies into
    the per-profile edge-profile dir, which is the same dir the silent
    reseed (`capture_silent`) reads. A fresh browser sign-in generally
    yields a longer-lived session than a scraped refresh token, so a later
    `owa-piggy reseed` picks up from a healthier starting point.

    The userdata dir is created if absent (mode 0o700) so this also works
    as a first-time "open Edge for a brand-new profile" step.
    """
    edge_dir = _config.profile_edge_dir(alias)
    edge_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = find_edge()
    if not binary:
        raise RuntimeError(
            "Microsoft Edge not found. Tried: "
            + ", ".join(_EDGE_CANDIDATES)
            + " and PATH lookup. Install Edge or set the path manually."
        )
    args = [
        binary,
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={edge_dir}",
        url or OWA_URL,
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, edge_dir


def _terminate(proc):
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()


# --- Pure helpers (unit-tested) --------------------------------------------


def is_token_endpoint(url):
    """True if `url` looks like an AAD v2 token endpoint.

    Accepts any login host (login.microsoftonline.com, login.microsoftonline.us,
    login.partner.microsoftonline.cn, ...) since we only care about the path
    suffix. The path is `/{tenant}/oauth2/v2.0/token` so a substring check on
    the suffix is unambiguous - no other AAD endpoint shares that tail."""
    if not isinstance(url, str):
        return False
    return TOKEN_PATH_SUFFIX in url and "login." in url


def decode_id_token_payload(id_token):
    """Decode the JWT payload of an id_token. Returns the claims dict, or
    None on malformed input. Pure - no signature verification (we trust
    AAD over TLS as the source).

    Used to extract `tid` (tenant) and `preferred_username`/`upn` for
    sanity-checking that the captured token belongs to the expected user.
    """
    if not isinstance(id_token, str) or id_token.count(".") < 2:
        return None
    try:
        return decode_jwt_segment(id_token.split(".")[1])
    except (ValueError, json.JSONDecodeError):
        return None


def email_matches_claims(email, claims):
    """Case-insensitive compare against the claims most likely to hold
    the user's email-shaped identifier.

    AAD puts the email-shaped identifier in different claims depending
    on tenant config: `preferred_username` is the canonical one for v2,
    `upn` shows up for synced AD users, `email` is the OIDC standard
    claim. Federated (Okta) tokens generally surface `preferred_username`.
    Returns True iff any of those matches."""
    if not email or not isinstance(claims, dict):
        return False
    target = email.strip().lower()
    if not target:
        return False
    for key in ("preferred_username", "upn", "email"):
        v = claims.get(key)
        if isinstance(v, str) and v.strip().lower() == target:
            return True
    return False


# --- Capture flow ----------------------------------------------------------


def _capture_token_response(session, *, deadline, log=None, tick=None):
    """Block until a /token response with refresh_token lands, then return
    its parsed body dict.

    AAD's token endpoint can be hit several times during a session - the
    auth-code redemption is the one we care about; subsequent silent
    refreshes also work. We accept the first response that has a
    refresh_token in it. Errors-from-AAD responses (e.g. interaction
    required) are ignored; we keep listening until the deadline.

    `tick(elapsed_s)` is called every ~5s while waiting so callers can
    print a heartbeat to stderr; without this, capture_silent looks
    indistinguishable from a hang to anyone watching the terminal.
    """
    if log is None:
        log = lambda *_: None  # noqa: E731
    if tick is None:
        tick = lambda *_: None  # noqa: E731

    pending_request_ids = set()
    started = time.monotonic()
    last_tick = started

    def _on_response_received(params):
        resp = params.get("response", {})
        url = resp.get("url", "")
        if is_token_endpoint(url):
            pending_request_ids.add(params.get("requestId"))
            log(f"observed token response: {url}")
            return True
        return False

    def _on_loading_finished(params):
        return params.get("requestId") in pending_request_ids

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        now = time.monotonic()
        if now - last_tick >= 5.0:
            tick(int(now - started))
            last_tick = now
        # First, see a token-endpoint responseReceived. Use a short
        # window so we can loop and re-check the deadline cleanly.
        try:
            session.wait_event(
                "Network.responseReceived",
                _on_response_received,
                timeout=min(remaining, 5.0),
            )
        except TimeoutError:
            continue

        # Wait for the body to finish so getResponseBody returns the full
        # payload (not just headers). loadingFinished is the signal.
        try:
            finished = session.wait_event(
                "Network.loadingFinished",
                _on_loading_finished,
                timeout=min(deadline - time.monotonic(), 10.0),
            )
        except TimeoutError:
            log("responseReceived without loadingFinished; retrying")
            continue
        request_id = finished["requestId"]
        pending_request_ids.discard(request_id)

        try:
            body_msg = session.call(
                "Network.getResponseBody",
                {"requestId": request_id},
                timeout=10.0,
            )
        except CdpError as e:
            log(f"getResponseBody failed for {request_id}: {e}")
            continue

        body = body_msg.get("body", "")
        if body_msg.get("base64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8")
            except Exception:
                continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            log("token response not JSON; skipping")
            continue

        if isinstance(parsed, dict) and parsed.get("refresh_token"):
            return parsed
        # Probably an AAD error envelope (interaction_required, etc.).
        # Keep listening - the SPA will retry.
        log(f"token response had no refresh_token; keys={list(parsed)}")

    raise TimeoutError("no /oauth2/v2.0/token response with refresh_token observed before deadline")


def _open_session(port):
    """Wait for Edge to expose a page target, then open a CDP session."""
    tab = find_tab(port, timeout=20.0)
    return CdpSession(port, tab["webSocketDebuggerUrl"])


def _verbose():
    return bool(os.environ.get("OWA_CAPTURE_DEBUG"))


def _logger(prefix):
    if not _verbose():
        return lambda *_: None  # noqa: E731
    return lambda msg: print(f"[{prefix}] {msg}", file=sys.stderr)


def _ticker(alias):
    """Heartbeat printer for the wait-for-/token loop. Always on (not
    gated by OWA_CAPTURE_DEBUG) so a watching user sees the operation
    is alive, not hung."""
    return lambda elapsed: print(
        f"[{alias}] still waiting for /oauth2/v2.0/token response ({elapsed}s elapsed)...",
        file=sys.stderr,
    )


def _capture_url():
    """Where the capture sidecar navigates to trigger a /token round-trip.

    Defaults to OWA (which mints the FOCI family RT). Override via
    OWA_CAPTURE_URL to capture a different client's RT off the wire — e.g.
    https://dev.azure.com/<org> to grab the Azure DevOps app's bound RT,
    which the FOCI client cannot obtain (AADSTS65002 preauth wall). Pair
    with OWA_CLIENT_ID / OWA_ORIGIN so the exchange replays under the same
    client and origin that minted it."""
    return os.environ.get("OWA_CAPTURE_URL", "").strip() or OWA_URL


def _capture_headless_default():
    """Honor OWA_CAPTURE_HEADLESS=0 as the escape hatch for tenants whose
    Conditional Access / device-compliance check fails in headless mode
    (mirrors OWA_RESEED_HEADLESS for the legacy scrape path). Default is
    headless: True so launchd doesn't pop a window onscreen."""
    return os.environ.get("OWA_CAPTURE_HEADLESS", "1").strip() != "0"


def capture_signin(alias, email, *, timeout=300, user_agent=None, capture_url=None):
    """Visible Edge for first-time setup. Returns a config dict on
    success, or raises RuntimeError with a user-facing message.

    The Edge profile dir is created if missing so subsequent
    `capture_silent` calls reuse the same session cookies. We do not
    pre-fill the email - AAD's home-realm discovery handles federation
    once the user types it in, and login_hint via URL is unreliable
    across MSAL versions.
    """
    log = _logger(f"capture/signin/{alias}")
    # Sign-in can legitimately take minutes (Okta Verify push approval,
    # MFA prompt, etc.). The visible Edge window is the primary signal,
    # but a stderr heartbeat reassures anyone watching the terminal.
    tick = lambda elapsed: print(  # noqa: E731
        f"[{alias}] still waiting for sign-in to complete ({elapsed}s elapsed)...", file=sys.stderr
    )
    edge_dir = _config.profile_edge_dir(alias)
    edge_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    port = find_free_port()
    log(
        f"launching Edge visibly on port {port}, userdata={edge_dir}"
        + (f", ua={user_agent!r}" if user_agent else "")
    )
    if capture_url is None:
        capture_url = _capture_url()
    if capture_url != OWA_URL:
        log(f"capture URL overridden to {capture_url}")
    proc = launch_edge(edge_dir, port, headless=False, url=capture_url, user_agent=user_agent)
    session = None
    try:
        session = _open_session(port)
        session.call("Network.enable", {})
        session.call("Runtime.enable", {})
        deadline = time.monotonic() + timeout
        log(f"awaiting /token response (timeout {timeout}s)...")
        token_response = _capture_token_response(session, deadline=deadline, log=log, tick=tick)

        # Let MSAL.js finish persisting its post-auth state to localStorage
        # before we kill Edge. The /token response arrives the moment AAD
        # returns the auth-code redemption; MSAL still needs to parse it,
        # encrypt under the msal.cache.encryption cookie, and write the
        # refreshtoken/idtoken/accesstoken entries. Without this wait the
        # sidecar profile ends up with valid session cookies but empty
        # MSAL localStorage, and the next silent reseed has nothing to
        # refresh - so it bounces to login.* and fast-fails to 'reauth'.
        log("waiting up to 8s for MSAL to persist localStorage...")
        persist_deadline = time.monotonic() + 8.0
        while time.monotonic() < persist_deadline:
            time.sleep(0.5)
            r = session.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "Object.keys(localStorage).filter("
                        'k => k.includes("|refreshtoken|") '
                        '|| k.includes("|idtoken|")).length'
                    ),
                    "returnByValue": True,
                },
            )
            n = (r.get("result", {}) or {}).get("value", 0) or 0
            if n > 0:
                log(f"MSAL persisted {n} idtoken/refreshtoken entries")
                break
        else:
            # Tenants with strict CA enforcement may refuse to let MSAL
            # complete its silent acquisition under any conditions - the
            # signin still works (we got the /token response) but MSAL
            # never writes a long-lived cache. Surface this so subsequent
            # silent reseeds don't look mysteriously broken.
            log("MSAL did not persist localStorage; silent reseed unlikely to work for this tenant")
    except (ConnectionError, CdpError, OSError) as e:
        # User force-closed Edge mid-auth, or the WS dropped for some
        # other reason. Surface a friendly RuntimeError instead of a raw
        # WebSocket traceback - the caller already prints a usable error.
        raise RuntimeError("Edge closed before sign-in completed (auth interrupted)") from e
    finally:
        if session is not None:
            session.close()
        _terminate(proc)

    return _build_config(token_response, email=email, mode="capture")


def capture_silent(alias, *, timeout=None, headless=None, user_agent=None, capture_url=None):
    """Headless Edge for scheduled reseed. Returns (status, config_dict):

      ('ok', dict)              success - dict has OWA_REFRESH_TOKEN/OWA_TENANT_ID
      ('reauth', None)          sidecar session cookies expired; user must
                                re-run `setup --email` interactively
      ('headless_blocked', None) headless Edge never reached OWA - tenant's
                                CA/device-compliance probably rejects truly
                                headless. Caller should retry with
                                headless=False before giving up.
      ('error', None)           other failure (no token observed after page
                                loaded, Edge launch failed, CDP error) - log
                                line written to stderr

    `headless=None` reads OWA_CAPTURE_HEADLESS (default headless). Pass
    headless=False to force the offscreen-but-not-headless mode. Even in
    the offscreen mode no onscreen window appears - the window is parked
    at -32000,-32000 - so launchd users still don't get a flashing browser.

    `timeout=None` reads OWA_CAPTURE_TIMEOUT (default 60s). The default was
    20s historically but Conditional-Access-heavy tenants routinely take
    25-40s on the post-reload /token round-trip; the 20s budget tripped
    spurious 'error' returns on otherwise healthy sessions.

    `capture_url=None` falls back to `_capture_url()` (OWA_CAPTURE_URL env
    or OWA). A profile that captured a non-FOCI client's RT persists its
    OWA_CAPTURE_URL to the config; the reseed path reads it back and passes
    it here so the headless reseed navigates to the *same* SPA that minted
    the token. Without this the silent reseed loads OWA, MSAL never touches
    the non-FOCI client's cache, and the round-trip yields the wrong RT (or
    none) - so launchd reseed silently rots and the user reseeds by hand.
    """
    if timeout is None:
        try:
            timeout = int(os.environ.get("OWA_CAPTURE_TIMEOUT", "60"))
        except ValueError:
            timeout = 60
    if capture_url is None:
        capture_url = _capture_url()
    log = _logger(f"capture/silent/{alias}")
    tick = _ticker(alias)
    edge_dir = _config.profile_edge_dir(alias)
    if not edge_dir.is_dir():
        log(f"no Edge profile dir at {edge_dir}; cannot reseed silently")
        return "reauth", None
    if headless is None:
        headless = _capture_headless_default()
    port = find_free_port()
    log(
        f"launching Edge {'headless' if headless else 'offscreen'} on port {port}"
        + (f" at {capture_url}" if capture_url != OWA_URL else "")
    )
    try:
        # offscreen=True keeps the non-headless fallback parked at
        # -32000,-32000 so the user doesn't see a window pop onscreen
        # and assume it's interactive - capture_silent never is.
        proc = launch_edge(
            edge_dir,
            port,
            headless=headless,
            url=capture_url,
            offscreen=True,
            user_agent=user_agent,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return "error", None

    session = None
    try:
        session = _open_session(port)
        session.call("Page.enable", {})
        session.call("Network.enable", {})

        # Poll location.hostname while Edge navigates. The page often goes
        # about:blank -> outlook.cloud.microsoft -> (maybe) login.* over a
        # few hundred ms; sampling once is racy and misses login redirects
        # that resolve a hair after the sample, leaving us to time out on
        # the /token wait instead of returning a clean 'reauth'.
        host = ""
        host_deadline = time.monotonic() + 7.0
        while time.monotonic() < host_deadline:
            loc = session.call(
                "Runtime.evaluate",
                {
                    "expression": "location.hostname",
                    "returnByValue": True,
                },
            )
            # session.call returns the `result` field of the JSON-RPC envelope
            # already, so `loc.result` IS the RemoteObject - no double-deref.
            host = (loc.get("result", {}) or {}).get("value", "") or ""
            if (
                host.startswith("login.")
                or host.endswith(".b2clogin.com")
                or host == "account.microsoft.com"
            ):
                log(f"post-load host: {host} (reauth required)")
                return "reauth", None
            if host and host != "about:blank":
                log(f"post-load host: {host}")
                break
            time.sleep(0.3)
        else:
            # Hostname never populated - Edge is stuck on a blank document
            # for 7+ seconds. Causes we have actually seen:
            #   - Edge launch was abnormally slow (cold cache, GPU init hang)
            #   - The user-data-dir was locked by another Edge process
            #   - First-run profile state mid-bootstrap
            # We've not yet observed a real tenant-policy "block headless"
            # in the wild; the legacy comment claiming Conditional Access
            # was the cause was wrong (the dereference one line up was
            # silently returning '' for every poll, so this branch fired
            # on every run). Keep the status name for backwards-compat
            # with reseed.py's fallback ladder.
            log("post-load host stayed empty after 7s")
            if headless:
                return "headless_blocked", None
            # Non-headless and still no navigation: cookies are almost
            # certainly expired (AAD redirected to login but the page
            # hadn't resolved yet, or session was rejected outright).
            # Silent capture can't do MFA - kick the user to setup.
            return "reauth", None

        # Wipe cached access tokens so MSAL has to round-trip /token.
        # The RT and idToken entries are kept so MSAL knows who the
        # session belongs to. In encrypted-cache mode the values are
        # opaque AES-GCM blobs but the keys are still readable, so the
        # substring filter on key names works regardless.
        wipe = session.call(
            "Runtime.evaluate",
            {
                "expression": """(() => {
                const ks = Object.keys(localStorage);
                let n = 0;
                for (const k of ks) {
                    if (k.includes('|accesstoken|')) {
                        localStorage.removeItem(k);
                        n++;
                    }
                }
                return n;
            })()""",
                "returnByValue": True,
            },
        )
        wiped = (wipe.get("result", {}) or {}).get("value", 0)
        log(f"wiped {wiped} accesstoken cache entries")

        session.call("Page.reload", {"ignoreCache": True})

        # Fast-fail reauth check. If MSAL's silent refresh attempt fails
        # (no usable RT in localStorage, sidecar cookies past their
        # lifetime, etc.) the SPA redirects to login.microsoftonline.com
        # within a couple of seconds of the reload. Without this poll the
        # caller waits the full /token timeout (20s) for a token request
        # that will never come, even though we have a definitive signal
        # that the session is dead within ~3s.
        post_reload_deadline = time.monotonic() + 4.0
        while time.monotonic() < post_reload_deadline:
            time.sleep(0.5)
            loc = session.call(
                "Runtime.evaluate",
                {
                    "expression": "location.hostname",
                    "returnByValue": True,
                },
            )
            h = (loc.get("result", {}) or {}).get("value", "") or ""
            if (
                h.startswith("login.")
                or h.endswith(".b2clogin.com")
                or h == "account.microsoft.com"
            ):
                log(f"post-reload host: {h} (reauth required, fast-fail)")
                return "reauth", None

        deadline = time.monotonic() + timeout
        log(f"awaiting /token response (timeout {timeout}s)...")
        token_response = _capture_token_response(session, deadline=deadline, log=log, tick=tick)
    except TimeoutError as e:
        print(
            f"[{alias}] timed out after {timeout}s waiting for "
            f"/oauth2/v2.0/token. Tenant may require non-headless "
            f"Edge - try OWA_CAPTURE_HEADLESS=0.",
            file=sys.stderr,
        )
        log(f"timeout: {e}")
        return "error", None
    except (ConnectionError, CdpError, OSError) as e:
        log(f"CDP failure: {e}")
        return "error", None
    finally:
        if session is not None:
            session.close()
        _terminate(proc)

    return "ok", _build_config(token_response, email=None, mode="capture")


def _build_config(token_response, *, email, mode):
    """Translate an AAD /token JSON response into the KV dict the
    profile config expects. Validates email-vs-claims if `email` is set.

    `mode` is stamped into OWA_AUTH_MODE so reseed knows which path to
    take next time around. We do not stamp the email itself on silent
    refresh - the user already proved identity at setup time."""
    rt = token_response.get("refresh_token")
    id_token = token_response.get("id_token")
    if not rt or not id_token:
        raise RuntimeError(
            f"token response missing required fields (have {sorted(token_response.keys())})"
        )
    claims = decode_id_token_payload(id_token)
    if not claims:
        raise RuntimeError("captured id_token failed to decode")
    tid = claims.get("tid")
    if not tid:
        raise RuntimeError("captured id_token has no tid claim; cannot determine tenant")
    if email and not email_matches_claims(email, claims):
        observed = (
            claims.get("preferred_username") or claims.get("upn") or claims.get("email") or "?"
        )
        raise RuntimeError(
            f"captured token belongs to {observed!r}, not {email!r}. "
            f"Did the wrong account sign in? Wipe the profile and retry."
        )
    out = {
        "OWA_REFRESH_TOKEN": rt,
        "OWA_TENANT_ID": tid,
        "OWA_AUTH_MODE": mode,
    }
    if email:
        out["OWA_EMAIL"] = email
    # When capturing a non-FOCI client's RT (OWA_CAPTURE_URL pointed at a
    # different SPA), persist the client/origin/capture-url so the exchange
    # replays under the same identity and a later silent reseed navigates
    # back to the same SPA. FOCI captures leave these unset and fall back
    # to the built-in defaults.
    for env_key, cfg_key in (
        ("OWA_CLIENT_ID", "OWA_CLIENT_ID"),
        ("OWA_ORIGIN", "OWA_ORIGIN"),
        ("OWA_CAPTURE_URL", "OWA_CAPTURE_URL"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            out[cfg_key] = val
    return out
