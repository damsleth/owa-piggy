"""Refresh-token redaction guard (v1-08 Phase 5).

The access token is emitted to stdout on purpose - it is the product, and the
secret-surface policy (.plans/v1-01) is explicit that AT surfaces carry usable
tokens. The *refresh* token is different: it is the long-lived credential that
mints access tokens, and it must never reach stdout, stderr, or the launchd
``refresh.log``. ``token --json`` / ``--agent`` deliberately return the rotated
refresh_token (that is the documented machine surface), so those modes are out
of scope here; this guards the human/diagnostic paths.

The exchange is monkeypatched - no network, no real tokens.
"""

import sys

import pytest

import owa_piggy.cli as cli_mod

# Distinctive sentinels so a substring match can't false-positive on, say, a
# scope URL or a UUID. Neither must ever appear in human-facing output.
PLANTED_RT = "1.PLANTED-REFRESH-TOKEN-must-never-leak-AQAAA"
ROTATED_RT = "1.ROTATED-REFRESH-TOKEN-must-never-leak-BQBBB"


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["owa-piggy"] + list(argv))
    return cli_mod.main()


@pytest.fixture
def planted_profile(tmp_config, clean_env, make_jwt, monkeypatch):
    """Plant a profile holding PLANTED_RT and stub the AAD exchange to return a
    synthetic access token plus a rotated refresh token (ROTATED_RT)."""
    from owa_piggy.config import save_config

    save_config(
        {
            "OWA_REFRESH_TOKEN": PLANTED_RT,
            "OWA_TENANT_ID": "00000000-0000-0000-0000-000000000000",
        }
    )
    access_token = make_jwt(
        {
            "aud": "https://graph.microsoft.com",
            "scp": "Mail.Read Mail.Send",
            "exp": 9_999_999_999,
            "iat": 1_700_000_000,
        }
    )
    monkeypatch.setattr(
        "owa_piggy.token_flow.exchange_token",
        lambda *a, **k: {
            "access_token": access_token,
            "refresh_token": ROTATED_RT,
            "expires_in": 3600,
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    return access_token


@pytest.mark.parametrize("argv", [["token"], ["status"], ["debug"], ["--doctor"]])
def test_refresh_token_never_reaches_stdout_or_stderr(planted_profile, monkeypatch, capsys, argv):
    """token / status / debug / doctor must never echo either the on-disk
    refresh token or the freshly rotated one to stdout or stderr."""
    _run(monkeypatch, argv)
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert PLANTED_RT not in blob, f"planted RT leaked to output of {argv}"
    assert ROTATED_RT not in blob, f"rotated RT leaked to output of {argv}"


def test_debug_emits_access_token_claims_but_not_the_refresh_token(
    planted_profile, monkeypatch, capsys
):
    """`debug` is the most verbose surface: it prints access-token claims
    (aud/scp/exp/iat) yet still must not print the refresh token."""
    _run(monkeypatch, ["debug"])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    # At least one AT claim is surfaced (proves debug really renders the token)...
    assert any(claim in blob for claim in ("aud", "scp", "exp", "iat"))
    # ...but never the refresh token.
    assert PLANTED_RT not in blob
    assert ROTATED_RT not in blob


def test_refresh_log_never_receives_the_refresh_token(planted_profile, monkeypatch, capsys):
    """The launchd stderr log (refresh.log) must never contain the RT, even
    when the exchange errors - the AAD error path prints codes/hints, not the
    token. We force an error and check both the captured streams and the log
    file on disk."""
    from owa_piggy import config as config_mod
    from owa_piggy.oauth import _err_stream

    # Stub the exchange to fail the way AAD does: print an error to the
    # thread-local sink (what would land in refresh.log under launchd) and
    # return None. It must reference the error code, never the token.
    def _failing_exchange(rt, tid, cid, scope, **kwargs):
        print("ERROR: invalid_grant: AADSTS700084", file=_err_stream())
        return None

    monkeypatch.setattr("owa_piggy.token_flow.exchange_token", _failing_exchange)

    _run(monkeypatch, ["status"])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert PLANTED_RT not in blob
    assert ROTATED_RT not in blob

    # And nothing wrote the RT to any profile's refresh.log on disk.
    for alias in config_mod.list_profiles():
        log_path = config_mod.profile_log_path(alias)
        if log_path.exists():
            assert PLANTED_RT not in log_path.read_text()
            assert ROTATED_RT not in log_path.read_text()
