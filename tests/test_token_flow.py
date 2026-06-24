"""Tests for exchange_fresh's provider dispatch (msal vs google)."""

from owa_piggy import token_flow
from owa_piggy.config import load_config


def test_exchange_fresh_msal_path_unaffected(monkeypatch):
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda rt, tid, cid, scope, **kw: {"access_token": "AT"},
    )
    config = {"OWA_REFRESH_TOKEN": "1.AQ", "OWA_TENANT_ID": "tid"}

    result, info = token_flow.exchange_fresh(config, "scope", persist=False)

    assert result == {"access_token": "AT"}
    assert info["tid_present"] is True
    assert info["rt_shape_ok"] is True


def test_exchange_fresh_google_skips_tenant_and_foci_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        token_flow,
        "google_exchange_token",
        lambda cid, secret, rt: calls.append((cid, secret, rt)) or {"access_token": "GAT"},
    )
    config = {
        "OWA_PROVIDER": "google",
        "OWA_REFRESH_TOKEN": "opaque-google-rt",
        "OWA_CLIENT_ID": "gcid",
        "OWA_CLIENT_SECRET": "gsecret",
    }

    result, info = token_flow.exchange_fresh(config, "ignored-scope", persist=False)

    assert result == {"access_token": "GAT"}
    assert calls == [("gcid", "gsecret", "opaque-google-rt")]
    assert info["tid_present"] is True
    assert info["rt_shape_ok"] is True


def test_exchange_fresh_google_missing_rt_short_circuits(monkeypatch):
    called = []
    monkeypatch.setattr(
        token_flow,
        "google_exchange_token",
        lambda *a: called.append(a),
    )
    config = {"OWA_PROVIDER": "google", "OWA_CLIENT_ID": "gcid"}

    result, info = token_flow.exchange_fresh(config, "scope", persist=False)

    assert result is None
    assert called == []
    assert info["rt_present"] is False


def test_exchange_fresh_google_failure_has_no_aad_error(monkeypatch):
    monkeypatch.setattr(token_flow, "google_exchange_token", lambda *a: None)
    config = {
        "OWA_PROVIDER": "google",
        "OWA_REFRESH_TOKEN": "rt",
        "OWA_CLIENT_ID": "gcid",
    }

    result, info = token_flow.exchange_fresh(config, "scope", persist=False)

    assert result is None
    assert info["aad_error"] is None


def test_rotated_refresh_token_is_written_to_the_named_profile(monkeypatch, tmp_path, clean_env):
    """Every `token` call rotates the RT; if the new one never reaches disk
    the profile is dead within the day. Nothing covered that write."""
    from owa_piggy.config import load_config

    other = tmp_path / "other" / "config"
    other.parent.mkdir(parents=True)
    other.write_text('OWA_REFRESH_TOKEN="1.OLD"\nOWA_TENANT_ID="tid"\n')
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **kw: {"access_token": "AT", "refresh_token": "1.NEW"},
    )
    config = {"OWA_REFRESH_TOKEN": "1.OLD", "OWA_TENANT_ID": "tid"}

    _, info = token_flow.exchange_fresh(config, "scope", persist=True, config_path=other)

    assert info["rotated"] is True
    assert config["OWA_REFRESH_TOKEN"] == "1.NEW"
    assert load_config(other)[0]["OWA_REFRESH_TOKEN"] == "1.NEW"


def test_no_persist_leaves_the_config_file_untouched(monkeypatch, tmp_path, clean_env):
    """`status` probes with persist=False and must not write - a probe that
    rotates the on-disk RT would invalidate the token the caller still holds."""
    path = tmp_path / "config"
    path.write_text('OWA_REFRESH_TOKEN="1.OLD"\nOWA_TENANT_ID="tid"\n')
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **kw: {"access_token": "AT", "refresh_token": "1.NEW"},
    )
    config = {"OWA_REFRESH_TOKEN": "1.OLD", "OWA_TENANT_ID": "tid"}

    _, info = token_flow.exchange_fresh(config, "scope", persist=False, config_path=path)

    assert info["rotated"] is True
    assert "1.OLD" in path.read_text()


def test_connect_falls_through_to_the_next_address(monkeypatch):
    """A dead address must not end the attempt - that fallthrough is the whole
    point of interleaving the families."""
    import socket

    from owa_piggy import oauth

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    good_port = listener.getsockname()[1]

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()  # nothing listening -> connection refused

    monkeypatch.setattr(
        oauth.socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", dead_port)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", good_port)),
        ],
    )
    try:
        sock = oauth.happy_eyeballs_connect("example.invalid", 443, 5)
        assert sock.getpeername()[1] == good_port
        sock.close()
    finally:
        listener.close()


def test_connect_raises_the_last_error_when_every_address_fails(monkeypatch):
    import socket

    from owa_piggy import oauth

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()

    monkeypatch.setattr(
        oauth.socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port)),
        ],
    )
    try:
        oauth.happy_eyeballs_connect("example.invalid", 443, 5)
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError when no address connects")


# --- shape checks and AAD-error classification -------------------------
#
# From the quality branch's coverage pass. These never touch the network:
# `exchange_token` is replaced with a fake returning a canned dict (or None)
# and, on the error path, printing a synthetic AADSTS line to the captured
# stderr sink. Refresh tokens are obvious fakes so a grep never flags this
# file as shipping a real one.

RT = "1.fake-refresh-token"
TID = "00000000-0000-0000-0000-000000000000"
SCOPE = "https://graph.microsoft.com/.default"


def test_missing_rt_returns_none(clean_env):
    config = {"OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert result is None
    assert info["rt_present"] is False
    assert info["tid_present"] is True
    assert info["rt_shape_ok"] is False


def test_missing_tid_returns_none(clean_env):
    config = {"OWA_REFRESH_TOKEN": RT}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert result is None
    assert info["rt_present"] is True
    assert info["tid_present"] is False
    assert info["rt_shape_ok"] is True


def test_foci_shape_rejected_for_default_client(clean_env):
    """An opaque (non-`1.`/`0.`) RT for the DEFAULT client is rejected."""
    config = {"OWA_REFRESH_TOKEN": "opaque-rt-no-prefix", "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert result is None
    assert info["rt_present"] is True
    assert info["tid_present"] is True
    assert info["rt_shape_ok"] is False


def test_opaque_rt_allowed_for_non_default_client(clean_env, monkeypatch):
    """An opaque RT IS allowed when OWA_CLIENT_ID overrides the default
    client - the FOCI shape check only applies to the default client."""
    other_cid = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
    config = {
        "OWA_REFRESH_TOKEN": "opaque-rt-no-prefix",
        "OWA_TENANT_ID": TID,
        "OWA_CLIENT_ID": other_cid,
    }
    monkeypatch.setattr(token_flow, "exchange_token", lambda *a, **k: {"access_token": "at"})
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False)
    assert info["rt_shape_ok"] is True
    assert info["cid"] == other_cid
    assert result == {"access_token": "at"}


def test_no_rotation_when_same_rt(tmp_config, clean_env, monkeypatch):
    """exchange_token returns the SAME refresh token (no rotation): the
    rotation branch is skipped (covers 147->149) and nothing is persisted."""
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **k: {"access_token": "at", "refresh_token": RT},
    )
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=True, config_path=tmp_config)
    assert result["access_token"] == "at"
    assert info["rotated"] is False
    assert not tmp_config.exists()


def test_no_rotation_when_rt_absent(tmp_config, clean_env, monkeypatch):
    """Response carries no refresh_token at all: rotation branch skipped."""
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    monkeypatch.setattr(token_flow, "exchange_token", lambda *a, **k: {"access_token": "at"})
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=True, config_path=tmp_config)
    assert info["rotated"] is False
    assert not tmp_config.exists()


def test_aad_error_70043_detected(clean_env, monkeypatch):
    """A failed exchange that prints AADSTS70043 to the captured stderr
    sink surfaces info['aad_error'] == 'AADSTS70043'."""

    def printing_exchange(*a, **k):
        from owa_piggy import oauth

        print("ERROR: invalid_grant: AADSTS70043 expired", file=oauth._err_stream())
        return None

    monkeypatch.setattr(token_flow, "exchange_token", printing_exchange)
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False, capture_stderr=True)
    assert result is None
    assert info["aad_error"] == "AADSTS70043"
    assert "AADSTS70043" in info["stderr_text"]


def test_aad_error_700084_detected(clean_env, monkeypatch):
    def printing_exchange(*a, **k):
        from owa_piggy import oauth

        print("ERROR: invalid_grant: AADSTS700084", file=oauth._err_stream())
        return None

    monkeypatch.setattr(token_flow, "exchange_token", printing_exchange)
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False, capture_stderr=True)
    assert result is None
    assert info["aad_error"] == "AADSTS700084"


def test_aad_error_none_when_unrecognized(clean_env, monkeypatch):
    """A failed exchange whose stderr has no recoverable AAD code leaves
    aad_error None (covers the detection loop completing without a match)."""

    def printing_exchange(*a, **k):
        from owa_piggy import oauth

        print("ERROR: something else entirely", file=oauth._err_stream())
        return None

    monkeypatch.setattr(token_flow, "exchange_token", printing_exchange)
    config = {"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID}
    result, info = token_flow.exchange_fresh(config, SCOPE, persist=False, capture_stderr=True)
    assert result is None
    assert info["aad_error"] is None


def test_token_sink_receives_the_rotated_token_instead_of_the_config(
    tmp_config, clean_env, monkeypatch
):
    """A bound client's rotated token must not reach OWA_REFRESH_TOKEN.

    That key holds the profile's FOCI token; overwriting it with a token
    AAD only accepts from another client would break every other audience
    on the profile.
    """
    from owa_piggy.config import save_config

    save_config({"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID})
    config = {"OWA_REFRESH_TOKEN": "1.bound-client-rt", "OWA_TENANT_ID": TID}
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **k: {"access_token": "AT", "refresh_token": "1.bound-client-rt-v2"},
    )
    sunk = []

    result, info = token_flow.exchange_fresh(config, SCOPE, persist=True, token_sink=sunk.append)

    assert result is not None
    assert info["rotated"] is True
    assert sunk == ["1.bound-client-rt-v2"]
    # The profile config on disk still holds the FOCI token.
    assert load_config()[0]["OWA_REFRESH_TOKEN"] == RT


def test_no_token_sink_still_persists_to_the_config(tmp_config, clean_env, monkeypatch):
    from owa_piggy.config import save_config

    save_config({"OWA_REFRESH_TOKEN": RT, "OWA_TENANT_ID": TID})
    config, persist = load_config()
    monkeypatch.setattr(
        token_flow,
        "exchange_token",
        lambda *a, **k: {"access_token": "AT", "refresh_token": "1.rotated"},
    )

    token_flow.exchange_fresh(config, SCOPE, persist=persist)

    assert load_config()[0]["OWA_REFRESH_TOKEN"] == "1.rotated"
