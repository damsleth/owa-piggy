"""Tests for status/debug behavior that should mirror token resolution."""

from types import SimpleNamespace

from owa_piggy import status as status_mod


def test_status_json_marks_disabled_without_probe(monkeypatch, tmp_config, clean_env):
    from owa_piggy.config import profile_dir, save_config, save_profiles_conf, set_active_profile

    profile_dir("work").mkdir(parents=True)
    set_active_profile("work")
    save_config(
        {
            "OWA_REFRESH_TOKEN": "1.AQ_fake",
            "OWA_TENANT_ID": "tid",
        }
    )
    save_profiles_conf({"OWA_DEFAULT_PROFILE": "", "OWA_PROFILES": []})
    calls = []
    monkeypatch.setattr(
        "owa_piggy.token_flow.exchange_token",
        lambda *_args: calls.append(True) or {"access_token": "unused"},
    )

    report = status_mod.status_report("work")

    assert report["state"] == "disabled"
    assert report["hints"] == ["profile is disabled"]
    assert calls == []


def test_status_json_keeps_legacy_fallback_when_registry_missing(
    monkeypatch, tmp_config, clean_env, make_jwt
):
    from owa_piggy.config import profile_dir, profiles_conf_path, save_config, set_active_profile

    profile_dir("work").mkdir(parents=True)
    set_active_profile("work")
    save_config(
        {
            "OWA_REFRESH_TOKEN": "1.AQ_fake",
            "OWA_TENANT_ID": "tid",
        }
    )
    assert not profiles_conf_path().exists()

    calls = []

    def _exchange(*_args):
        calls.append(True)
        return {
            "access_token": make_jwt(
                {
                    "exp": 9_999_999_999,
                    "aud": "https://graph.microsoft.com",
                    "scp": "User.Read",
                }
            ),
        }

    monkeypatch.setattr("owa_piggy.token_flow.exchange_token", _exchange)

    report = status_mod.status_report("work")

    assert report["state"] == "ok"
    assert calls == [True]


def test_status_honors_profile_default_audience(
    monkeypatch, tmp_config, clean_env, make_jwt, capsys
):
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile("work")
    save_config(
        {
            "OWA_REFRESH_TOKEN": "1.AQ_fake",
            "OWA_TENANT_ID": "tid",
            "OWA_DEFAULT_AUDIENCE": "teams",
        }
    )

    seen = {}

    def _exchange(_rt, _tid, _cid, scope):
        seen["scope"] = scope
        return {
            "access_token": make_jwt(
                {
                    "exp": 9_999_999_999,
                    "aud": "https://api.spaces.skype.com",
                    "scp": "User.Read",
                }
            ),
        }

    monkeypatch.setattr("owa_piggy.token_flow.exchange_token", _exchange)
    monkeypatch.setattr(status_mod, "launchd_is_scheduled", lambda _alias: False)

    rc = status_mod.do_status("work", verbose=True)

    assert rc == 0
    assert seen["scope"].startswith("https://api.spaces.skype.com/.default ")
    assert "audience:     teams" in capsys.readouterr().out


def test_debug_honors_profile_default_audience(
    monkeypatch, tmp_config, clean_env, make_jwt, capsys
):
    from owa_piggy.config import save_config, set_active_profile

    set_active_profile("work")
    save_config(
        {
            "OWA_REFRESH_TOKEN": "1.AQ_fake",
            "OWA_TENANT_ID": "tid",
            "OWA_DEFAULT_AUDIENCE": "outlook",
        }
    )

    seen = {}

    def _exchange(_rt, _tid, _cid, scope):
        seen["scope"] = scope
        return {
            "access_token": make_jwt(
                {
                    "exp": 9_999_999_999,
                    "iat": 9_999_990_000,
                    "aud": "https://outlook.office.com",
                    "scp": "Mail.Read",
                }
            ),
        }

    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not loaded")

    monkeypatch.setattr("owa_piggy.token_flow.exchange_token", _exchange)
    monkeypatch.setattr(status_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(status_mod, "find_reseed_script", lambda: None)

    rc = status_mod.do_debug("work")

    assert rc == 0
    assert seen["scope"].startswith("https://outlook.office.com/.default ")
    assert "access token aud: https://outlook.office.com" in capsys.readouterr().out
