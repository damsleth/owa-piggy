"""signin.py: TOTP, otpauth parsing, and the page-driving loop against a
scripted CDP session. No Edge, no network."""

import pytest

from owa_piggy import capture, signin
from owa_piggy.cdp import CdpError

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # ascii "12345678901234567890"


def test_totp_matches_rfc6238_vectors():
    assert signin.totp(RFC_SECRET, now=59) == "287082"
    assert signin.totp(RFC_SECRET, now=1111111109) == "081804"
    assert signin.totp(RFC_SECRET, now=59, digits=8) == "94287082"


def test_parse_otpauth_takes_secret_or_bare_value():
    uri = "otpauth://totp/Microsoft:a%40b.no?secret=" + RFC_SECRET + "&issuer=Microsoft"
    assert signin.parse_otpauth(uri) == RFC_SECRET
    assert signin.parse_otpauth("  " + RFC_SECRET + " ") == RFC_SECRET


def test_credentials_need_all_three():
    assert signin.credentials({"OWA_EMAIL": "a@b.no", "OWA_PASSWORD": "pw"}) is None
    full = {"OWA_EMAIL": "a@b.no", "OWA_PASSWORD": "pw", "OWA_TOTP_SECRET": RFC_SECRET}
    assert signin.credentials(full) == ("a@b.no", "pw", RFC_SECRET)


class _Session:
    """Scripted CdpSession: hostname samples and step answers pop in order;
    every Input.insertText is recorded."""

    def __init__(self, hosts, steps):
        self.hosts, self.steps, self.typed = list(hosts), list(steps), []
        self.refocus = None  # drive re-runs the step script before typing
        self.url = "https://login.microsoftonline.com/"

    def call(self, method, params=None, **_):
        if method == "Input.insertText":
            self.typed.append(params["text"])
            return {}
        if method == "Input.dispatchMouseEvent":
            self.typed.append(f"{params['type']}@{params['x']},{params['y']}")
            return {}
        if method != "Runtime.evaluate":
            return {}
        expr = params["expression"]
        if "/*owa-type*/" in expr:
            import json

            self.typed.append(json.loads(expr.split("const t = ", 1)[1].split(";", 1)[0]))
            return {"result": {"value": 6}}
        if expr == "location.href":
            value = self.url
        elif expr == "location.hostname":
            value = self.hosts.pop(0) if len(self.hosts) > 1 else self.hosts[0]
        elif expr.startswith("\n(() => {\n  const vis") and "activeElement" not in expr:
            if self.refocus:
                value, self.refocus = self.refocus, None
            else:
                value = self.steps.pop(0) if self.steps else "none"
                if value in ("email", "password", "otc"):
                    self.refocus = value
        else:
            value = "clicked"
        if value == "RACE":
            raise CdpError("Runtime.evaluate", {"message": "navigated"})
        return {"result": {"value": value}}


def _no_sleep(monkeypatch):
    monkeypatch.setattr(signin.time, "sleep", lambda *_: None)
    monkeypatch.setattr(signin, "_SETTLE_AFTER_SUBMIT", 0.0)
    monkeypatch.setattr(signin, "_SETTLE_BEFORE_TYPE", 0.0)


@pytest.mark.parametrize(
    "url",
    [
        "http://login.microsoftonline.com/",
        "https://login.evil.example/",
        "https://login.microsoftonline.com.evil.example/",
        "https://login.microsoftonline.com@evil.example/",
        "about:blank",
        "https://[invalid/",
    ],
)
@pytest.mark.parametrize("redirect", [False, True])
def test_drive_refuses_untrusted_url_before_typing(monkeypatch, url, redirect):
    _no_sleep(monkeypatch)
    sess = _Session(hosts=["login.microsoftonline.com"], steps=["password"])
    if redirect:
        monkeypatch.setattr(signin.time, "sleep", lambda *_: setattr(sess, "url", url))
    else:
        sess.url = url
    assert not signin.drive(
        sess,
        ("alice@example.com", "fake-password", RFC_SECRET),
        log=lambda *_: None,
        is_login=capture._is_login_host,
    )
    assert sess.typed == []


def test_drive_walks_the_pages_and_types_only_secrets(monkeypatch):
    _no_sleep(monkeypatch)
    login = "login.microsoftonline.com"
    sess = _Session(
        hosts=[login, "RACE"] + [login] * 6 + ["outlook.office.com"],
        steps=[
            'account|120,80|div#tile "a@b.no"',
            "none",
            "password",
            "method:other",
            "method:otc",
            "otc",
            "kmsi",
        ],
    )
    logged = []
    ok = signin.drive(
        sess,
        ("a@b.no", "hunter2", RFC_SECRET),
        log=logged.append,
        is_login=lambda h: h.startswith("login."),
        budget=30,
    )
    assert ok
    assert sess.typed[:3] == ["mouseMoved@120,80", "mousePressed@120,80", "mouseReleased@120,80"]
    assert sess.typed[3] == "hunter2"
    assert len(sess.typed) == 5 and sess.typed[4].isdigit() and len(sess.typed[4]) == 6
    assert not any("hunter2" in line or sess.typed[4] in line for line in logged)
    assert any("landed on outlook.office.com" in line for line in logged)


def test_drive_gives_up_when_a_step_repeats(monkeypatch):
    _no_sleep(monkeypatch)
    sess = _Session(hosts=["login.microsoftonline.com"], steps=["password"] * 5)
    ok = signin.drive(
        sess,
        ("a@b.no", "wrong", RFC_SECRET),
        log=lambda *_: None,
        is_login=lambda h: h.startswith("login."),
        budget=30,
    )
    assert not ok
    assert sess.typed == ["wrong"] * signin._MAX_REPEATS


def test_drive_times_out_on_a_page_that_never_moves(monkeypatch):
    _no_sleep(monkeypatch)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(signin.time, "monotonic", lambda: next(clock))
    sess = _Session(hosts=["login.microsoftonline.com"], steps=[])
    ok = signin.drive(
        sess,
        ("a@b.no", "pw", RFC_SECRET),
        log=lambda *_: None,
        is_login=lambda h: h.startswith("login."),
        budget=30,
    )
    assert not ok and sess.typed == []
