"""Unit tests for the trough-fetch adapter and its setup wiring."""

import json

import pytest

from owa_piggy import setup as setup_mod
from owa_piggy import trough


class _FakeResp:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mk_token(kind, *, foci, token, tid="", sub="", last_seen=1000, extra=None):
    """Match the shape trough's /tokens endpoint returns (subset)."""
    payload = {"tid": tid, "sub": sub}
    if extra:
        payload.update(extra)
    return {
        "kind": kind,
        "token": token,
        "foci": 1 if foci else 0,
        "last_seen": last_seen,
        "src_host": "login.microsoftonline.com",
        "sub": sub,
        "payload_json": json.dumps(payload),
    }


def test_fetch_foci_picks_freshest_refresh(monkeypatch):
    rows = {
        "tokens": [
            _mk_token("access", foci=False, token="at-1", tid="t1", last_seen=500),
            _mk_token("refresh", foci=True, token="rt-old", tid="t1", sub="u1", last_seen=600),
            _mk_token("refresh", foci=True, token="rt-new", tid="t1", sub="u1", last_seen=800),
        ]
    }
    monkeypatch.setattr(trough, "_http_get_json", lambda url, *, timeout: rows)
    rt, tid, info = trough.fetch_foci("http://x:8765")
    assert rt == "rt-new"
    assert tid == "t1"
    assert info["matched"] == 2
    assert info["total_candidates"] == 3
    assert info["token_len"] == len("rt-new")


def test_fetch_foci_filters_by_tenant(monkeypatch):
    rows = {
        "tokens": [
            _mk_token("refresh", foci=True, token="rt-other", tid="t-other", last_seen=900),
            _mk_token("refresh", foci=True, token="rt-want", tid="t-want", last_seen=800),
        ]
    }
    monkeypatch.setattr(trough, "_http_get_json", lambda url, *, timeout: rows)
    rt, tid, _ = trough.fetch_foci("http://x:8765", tenant="t-want")
    assert rt == "rt-want"
    assert tid == "t-want"


def test_fetch_foci_filters_by_sub(monkeypatch):
    rows = {
        "tokens": [
            _mk_token("refresh", foci=True, token="rt-x", tid="t1", sub="alice"),
            _mk_token("refresh", foci=True, token="rt-y", tid="t1", sub="bob"),
        ]
    }
    monkeypatch.setattr(trough, "_http_get_json", lambda url, *, timeout: rows)
    rt, _, info = trough.fetch_foci("http://x:8765", sub="bob")
    assert rt == "rt-y"
    assert info["sub"] == "bob"


def test_fetch_foci_skips_access_and_empty_tokens(monkeypatch):
    rows = {
        "tokens": [
            _mk_token("access", foci=False, token="at-1", tid="t1"),
            _mk_token("refresh", foci=True, token="", tid="t1"),  # no token body
        ]
    }
    monkeypatch.setattr(trough, "_http_get_json", lambda url, *, timeout: rows)
    with pytest.raises(RuntimeError, match="no FOCI refresh token"):
        trough.fetch_foci("http://x:8765")


def test_fetch_foci_no_match_for_filter(monkeypatch):
    rows = {
        "tokens": [
            _mk_token("refresh", foci=True, token="rt-x", tid="t-other"),
        ]
    }
    monkeypatch.setattr(trough, "_http_get_json", lambda url, *, timeout: rows)
    with pytest.raises(RuntimeError, match=r"tenant=t-want"):
        trough.fetch_foci("http://x:8765", tenant="t-want")


def test_fetch_foci_empty_store(monkeypatch):
    monkeypatch.setattr(trough, "_http_get_json", lambda url, *, timeout: {"tokens": []})
    with pytest.raises(RuntimeError, match="no FOCI refresh tokens"):
        trough.fetch_foci("http://x:8765")


def test_trough_setup_persists_token_and_ua(tmp_config, monkeypatch):
    """`interactive_setup(trough_url=...)` writes RT+TID+UA atomically."""
    monkeypatch.setattr(
        trough,
        "fetch_foci",
        lambda url, *, tenant=None, sub=None, timeout=10, limit=50: (
            "1.AQ_fake-rt",
            "tid-abc",
            {
                "tid": "tid-abc",
                "sub": "oid-1",
                "src_host": "login.x",
                "last_seen": 123,
                "expires_in_at_capture": 4200,
                "total_candidates": 1,
                "matched": 1,
                "token_len": 13,
            },
        ),
    )
    cfg = {}
    ok = setup_mod.interactive_setup(
        cfg,
        alias="trough-test",
        trough_url="http://1.2.3.4:8765",
        trough_tenant="tid-abc",
        user_agent="Mozilla/5.0 (iPad) TeamsMobile-iOS",
    )
    assert ok is True
    assert cfg["OWA_REFRESH_TOKEN"] == "1.AQ_fake-rt"
    assert cfg["OWA_TENANT_ID"] == "tid-abc"
    assert cfg["OWA_USER_AGENT"] == "Mozilla/5.0 (iPad) TeamsMobile-iOS"
    assert "OWA_RT_ISSUED_AT" in cfg


def test_trough_setup_surfaces_fetch_failure(tmp_config, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(trough, "fetch_foci", _boom)
    cfg = {}
    ok = setup_mod.interactive_setup(cfg, alias="trough-fail", trough_url="http://bad:8765")
    assert ok is False
    assert "OWA_REFRESH_TOKEN" not in cfg
