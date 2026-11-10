"""Unattended Entra sign-in with a stored password + TOTP secret.

`capture_silent` normally gives up the moment the sidecar session comes to
rest on login.* - a dead session needs MFA, and nobody is at the keyboard
at 03:00. A profile that has opted in (`owa-piggy creds --op-item ...`)
carries OWA_PASSWORD and OWA_TOTP_SECRET in its config.json instead, and
`drive` walks the sign-in pages the way a user would: account tile,
password, "use a verification code", the TOTP, "stay signed in". The
secret is the same base32 seed 1Password holds for the one-time password;
owa-piggy computes the code itself (RFC 6238, stdlib only), so nothing
asks 1Password anything at reseed time.

Deliberately dumb page driving: every tick asks the page what is on
screen (`_STEP_JS` focuses the one input it finds, or clicks the one
link it recognises) and the Python side types the matching secret over
CDP `Input.insertText` - real keystrokes, so React/Knockout bindings see
them. Nothing here is a selector contract with Microsoft; when the login
UI changes, `OWA_CAPTURE_DEBUG=1 owa-piggy reseed` shows which step
stalls, and `_STEP_JS` gets one more regex.

Stored credentials are a deliberate weakening of the account's MFA: a
process that can read config.json can complete a sign-in. See
SECURITY.md before enabling this on a profile.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import struct
import time
import urllib.parse
from collections.abc import Callable

from .cdp import CdpError, CdpSession

PASSWORD_KEY = "OWA_PASSWORD"
TOTP_KEY = "OWA_TOTP_SECRET"

# Credentials bundle: (email, password, totp_secret).
Credentials = tuple[str, str, str]


def totp(secret: str, now: float | None = None, *, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 TOTP (SHA-1, 30s, 6 digits - the Entra default)."""
    raw = secret.strip().replace(" ", "").upper()
    key = base64.b32decode(raw + "=" * (-len(raw) % 8))
    counter = int((time.time() if now is None else now) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return f"{code:0{digits}d}"


def parse_otpauth(value: str) -> str:
    """The base32 secret out of an `otpauth://totp/...?secret=...` URI, or
    the value itself when 1Password handed us the bare secret."""
    value = value.strip()
    if value.startswith("otpauth://"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
        return (query.get("secret") or [""])[0]
    return value


def credentials(config: dict[str, str]) -> Credentials | None:
    """The profile's stored sign-in bundle, or None if it has not opted in."""
    email = (config.get("OWA_EMAIL") or "").strip()
    password = config.get(PASSWORD_KEY) or ""
    secret = (config.get(TOTP_KEY) or "").strip()
    if email and password and secret:
        return email, password, secret
    return None


# One tick of the sign-in walk. Focuses an empty input and names it, or
# clicks the one navigational element that moves the flow toward a TOTP
# prompt, or says 'none' while the page is still loading. %s is the
# JSON-encoded lowercase email, for the "pick an account" tile.
_STEP_JS = r"""
(() => {
  const vis = e => !!e && e.getClientRects().length > 0 && !e.disabled;
  const q = s => Array.from(document.querySelectorAll(s)).find(vis);
  // name=otc only: the method chooser's own controls carry ids like
  // idBtn_SAOTCS_Cancel, which an id*="OTC" match mistook for the code box.
  const otc = q('input[name="otc"]');
  if (otc) { otc.focus(); otc.value = ''; return 'otc'; }
  const pw = q('input[type="password"]');
  if (pw) { pw.focus(); pw.value = ''; return 'password'; }
  const em = q('input[name="loginfmt"], input[type="email"]');
  if (em) { em.focus(); em.value = ''; return 'email'; }
  const texts = Array.from(document.querySelectorAll(
    'a, button, div[role="button"], div[role="listitem"], [data-value], '
    + 'input[type="submit"], input[type="button"]'
  )).filter(vis);
  const label = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim();
  // Innermost match: a listitem wrapping the real tile also carries its
  // text, and clicking the wrapper reaches no handler.
  const inner = ms => ms.filter(e => !ms.some(o => o !== e && e.contains(o)))[0];
  const byText = re => inner(texts.filter(e => re.test(label(e))));
  const click = (e, tag) => {
    e.scrollIntoView({block: 'center'});
    const r = e.getBoundingClientRect();
    const desc = e.tagName.toLowerCase() + (e.id ? '#' + e.id : '')
      + ' "' + label(e).slice(0, 40) + '"';
    const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
    return tag + '|' + x + ',' + y + '|' + desc;
  };
  const body = document.body ? document.body.innerText : '';
  if (/stay signed in|forbli p.logget/i.test(body)) {
    const cb = document.querySelector('#KmsiCheckboxField');
    if (cb && !cb.checked) cb.click();
    const yes = byText(/^(yes|ja)$/i);
    if (yes) return click(yes, 'kmsi');
  }
  // Entra's passkey page (login.microsoft.com/.../bridge/fido) renders
  // nothing until the browser's WebAuthn prompt resolves, which it never
  // does headless. The page carries its own way out in the URL: cancelUrl
  // is where Escape on the prompt sends the flow (back to the method
  // chooser), so take it directly.
  if (/\/bridge\/fido$/.test(location.pathname)) {
    const cancel = new URLSearchParams(location.search).get('cancelUrl');
    if (cancel) { location.href = cancel; return 'fido:cancel'; }
  }
  const email = %s;
  const tile = inner(texts.filter(e => label(e).toLowerCase().includes(email)));
  if (tile) return click(tile, 'account');
  const pwd = byText(/use my password|bruk passordet/i);
  if (pwd) return click(pwd, 'method:pwd');
  const code = byText(/verification code|verifiseringskode/i)
    || document.querySelector('[data-value="PhoneAppOTP"]');
  if (code) return click(code, 'method:otc');
  const other = byText(
    new RegExp(['other ways to sign in', 'sign in another way', 'sign in with (a )?different',
      'use a different', 'try another', 'can.?t use my', 'andre m.ter', 'p. en annen m.te'
    ].join('|'), 'i')
  ) || document.querySelector('#signInAnotherWay');
  if (other) return click(other, 'method:other');
  return 'none';
})()
"""

# Put text into the input the step focused. Setting .value through the
# prototype setter plus an input event is what Knockout (Entra) and React
# both listen to; CDP Input.insertText depended on keyboard focus, which
# the sidecar's 1Password extension steals the moment its inline menu
# opens on a code box. %s is the JSON-encoded text. Returns the length
# the field holds afterwards, so the log can say the text landed.
_TYPE_JS = r"""
(() => { /*owa-type*/
  const t = %s;
  const el = document.activeElement;
  if (!el || el.tagName !== 'INPUT') return -1;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, t);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return el.value.length;
})()
"""

# Submit whatever form owns the focused input. Clicking the submit button
# is what the page expects; Enter via Input.dispatchKeyEvent needs a
# `text: "\r"` dance that headless=new honors inconsistently.
_SUBMIT_JS = r"""
(() => {
  const vis = e => !!e && e.getClientRects().length > 0 && !e.disabled;
  const b = Array.from(
    document.querySelectorAll('input[type="submit"], button[type="submit"]')
  ).find(vis);
  if (b) { b.click(); return 'clicked'; }
  const f = document.activeElement && document.activeElement.form;
  if (f) { f.requestSubmit ? f.requestSubmit() : f.submit(); return 'submitted'; }
  return 'nothing';
})()
"""

# Entra remembers "passkey" as the account's last method and sends the
# flow to /bridge/fido, whose navigator.credentials.get() waits forever in
# a headless browser for a prompt nobody can answer. Make that call fail
# at once like a cancelled prompt (and report no platform authenticator)
# so the page falls through to its "sign in another way" link - the same
# path a user takes by pressing Escape.
_NO_WEBAUTHN_JS = r"""
(() => {
  try {
    const reject = () => Promise.reject(
      new DOMException('owa-piggy: no authenticator here', 'NotAllowedError'));
    if (navigator.credentials) {
      navigator.credentials.get = reject;
      navigator.credentials.create = reject;
    }
    if (window.PublicKeyCredential) {
      const no = () => Promise.resolve(false);
      PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable = no;
      PublicKeyCredential.isConditionalMediationAvailable = no;
    }
  } catch (e) {}
})()
"""

# Title, path and the visible text of a page the walk does not recognise.
_SNAPSHOT_JS = r"""
(document.title + ' ' + location.pathname + ' :: '
 + (document.body ? document.body.innerText : '')).replace(/\s+/g, ' ').trim()
"""

# Pause between an input appearing and typing into it (see drive()).
_SETTLE_BEFORE_TYPE = 3.0

# How long a submitted form gets to turn into the next page before the
# same input counts as "presented again".
_SETTLE_AFTER_SUBMIT = 12.0

# A step the page keeps presenting is a rejected credential, not a slow
# page: 'password' three times means the password is wrong.
_MAX_REPEATS = 3


def _eval(session: CdpSession, expression: str) -> str:
    result = session.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    return (result.get("result", {}) or {}).get("value", "") or ""


def drive(
    session: CdpSession,
    creds: Credentials,
    *,
    log: Callable[[str], None],
    is_login: Callable[[str], bool],
    budget: float = 120.0,
) -> bool:
    """Walk the sign-in pages until the document leaves login.*.

    Returns True once the page has landed somewhere that is not a sign-in
    host (the SPA then boots as if the session had never died), False
    when a step repeats past `_MAX_REPEATS` (credential rejected) or the
    budget runs out. Logs step names only, never what was typed.
    """
    email, password, secret = creds
    step_js = _STEP_JS % json.dumps(email.lower())

    def trusted_page() -> bool:
        # Inspect the URL, not just the hostname: credentials must never be
        # entered over HTTP, even when the host itself is allowlisted.
        try:
            url = urllib.parse.urlsplit(_eval(session, "location.href"))
            return url.scheme == "https" and is_login(url.hostname or "")
        except ValueError:
            return False

    # Headless Edge is launched with --window-size=1,1: every element centre
    # is outside that viewport and a mouse click there lands on nothing.
    # Give the page a real one for the duration of the walk.
    with contextlib.suppress(CdpError):
        session.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
    with contextlib.suppress(CdpError):
        session.call("Page.addScriptToEvaluateOnNewDocument", {"source": _NO_WEBAUTHN_JS})
        _eval(session, _NO_WEBAUTHN_JS)
    start = time.monotonic()
    last_snapshot = start
    seen: dict[str, int] = {}
    # (step, when) of the last submit: the same input showing again within
    # _SETTLE_AFTER_SUBMIT seconds is the server still working, not a
    # rejection - re-typing then would cancel the in-flight request.
    submitted: tuple[str, float] = ("", 0.0)
    while time.monotonic() - start < budget:
        try:
            host = _eval(session, "location.hostname")
            if host and host != "about:blank" and not is_login(host):
                log(f"signed in; landed on {host}")
                return True
            if not trusted_page():
                return False
            raw = _eval(session, step_js)
        except CdpError as e:
            # Mid-navigation; the next sample lands on the new document.
            log(f"sign-in sample raced a navigation ({e}); still polling")
            time.sleep(0.5)
            continue
        if raw == "none":
            if time.monotonic() - last_snapshot >= 10:
                # Nothing recognised for a while: say what the page shows, so
                # the next regex can be written from the log alone.
                last_snapshot = time.monotonic()
                log("sign-in page: " + _eval(session, _SNAPSHOT_JS)[:300])
            time.sleep(0.7)
            continue
        step, _, rest = raw.partition("|")
        if step == submitted[0] and time.monotonic() - submitted[1] < _SETTLE_AFTER_SUBMIT:
            time.sleep(0.5)
            continue
        seen[step] = seen.get(step, 0) + 1
        if seen[step] > _MAX_REPEATS:
            where = _eval(session, "document.title + ' ' + location.pathname")
            log(f"sign-in step {step!r} keeps coming back on {where!r}; giving up")
            return False
        log(f"sign-in step: {step}" + (f" -> {rest.partition('|')[2]}" if rest else ""))
        if seen[step] > 1:
            # Same page again: its text usually says why ("incorrect code").
            log("sign-in page: " + _eval(session, _SNAPSHOT_JS)[:300])
        text = {"email": email, "password": password, "otc": totp(secret)}.get(step)
        if text:
            # The input shows up before the view behind it is ready: the
            # code box appears while SAS/BeginAuth is still in flight, and a
            # Verify clicked during that request is dropped and the box
            # cleared. Let the view settle, then focus the box again.
            time.sleep(_SETTLE_BEFORE_TYPE)
            if not trusted_page():
                return False
            _eval(session, step_js)
            if step == "otc":
                text = totp(secret)
            got = _eval(session, _TYPE_JS % json.dumps(text))
            log(f"typed {got} chars into {step}; submit: {_eval(session, _SUBMIT_JS)}")
            submitted = (step, time.monotonic())
        elif rest:
            # A real mouse click at the element's centre: Knockout/React
            # handlers that ignore a synthetic element.click() still see it.
            x, y = (int(v) for v in rest.partition("|")[0].split(","))
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                session.call(
                    "Input.dispatchMouseEvent",
                    {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
                )
        time.sleep(1.5)
    log(f"sign-in did not finish within {budget:.0f}s")
    return False
