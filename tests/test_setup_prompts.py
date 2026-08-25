"""Interactive service selection in `setup`.

The point of these prompts is that setting up an identity with Outlook +
Teams + an Azure DevOps org should be three questions, not three flags -
and that a scripted or piped setup must stay exactly as silent as before.
"""

import builtins

import pytest

from owa_piggy import clients, profiles, setup
from owa_piggy.config import DEVOPS_CLIENT_ID

TEAMS = clients.TEAMS_WEB_CLIENT_ID


@pytest.fixture
def answers(monkeypatch):
    """Queue up canned answers for input(); returns the prompts seen."""
    seen = []

    def _install(queue):
        pending = list(queue)

        def fake_input(prompt=""):
            seen.append(prompt)
            if not pending:
                raise EOFError
            return pending.pop(0)

        monkeypatch.setattr(builtins, "input", fake_input)
        return seen

    return _install


def test_teams_is_the_default_answer(answers):
    answers(["", ""])  # Enter, Enter
    assert setup.prompt_for_clients("work") == ["teams"]


def test_teams_can_be_declined(answers):
    answers(["n", "n"])
    assert setup.prompt_for_clients("work") == []


def test_devops_asks_for_an_org_and_normalizes_it(answers):
    answers(["n", "y", "MyOrg"])
    specs = setup.prompt_for_clients("work")
    assert specs == ["devops=MyOrg"]
    # The spec goes through the same parser the flag uses.
    client_id, url, err = clients.parse_spec(specs[0])
    assert (client_id, url, err) == (DEVOPS_CLIENT_ID, "https://dev.azure.com/MyOrg", "")


def test_devops_with_a_blank_org_is_skipped_not_broken(answers):
    answers(["y", "y", ""])
    assert setup.prompt_for_clients("work") == ["teams"]


def test_both_services(answers):
    answers(["y", "y", "https://dev.azure.com/Org/Proj/_workitems"])
    assert setup.prompt_for_clients("work") == [
        "teams",
        "devops=https://dev.azure.com/Org/Proj/_workitems",
    ]


def test_closed_stdin_takes_the_defaults_instead_of_crashing(answers):
    answers([])  # every input() raises EOFError
    assert setup.prompt_for_clients("work") == ["teams"]


def test_email_prompt_accepts_blank_for_the_paste_flow(answers):
    answers([""])
    assert setup.prompt_for_email("work") is None


def test_email_prompt_rejects_a_non_address_then_accepts(answers):
    seen = answers(["not-an-email", "me@corp.example"])
    assert setup.prompt_for_email("work") == "me@corp.example"
    assert len(seen) == 2


# --- the non-interactive contract --------------------------------------


def _stub_setup(monkeypatch, calls):
    """Replace the sign-in machinery so create_profile can run offline."""
    monkeypatch.setattr(profiles, "interactive_setup", lambda *a, **k: calls.append(k) or True)
    monkeypatch.setattr(profiles, "ensure_profile_registered", lambda *a, **k: None)
    monkeypatch.setattr(profiles, "_seed_bound_clients", lambda *a, **k: None)


def test_no_prompts_when_stdin_is_not_a_tty(monkeypatch, tmp_config, clean_env):
    """A piped or cron-driven setup must never block on a question."""
    calls = []
    _stub_setup(monkeypatch, calls)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def boom(*a, **k):
        raise AssertionError("prompted on a non-TTY")

    monkeypatch.setattr(profiles, "prompt_for_clients", boom)
    monkeypatch.setattr(profiles, "prompt_for_email", boom)
    assert profiles.create_profile("work") == 0


def test_no_prompts_when_the_caller_already_said_what_it_wants(monkeypatch, tmp_config, clean_env):
    """`--email x --with-client teams` is a complete answer; asking again
    would make the flags useless in scripts."""
    calls = []
    _stub_setup(monkeypatch, calls)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def boom(*a, **k):
        raise AssertionError("prompted despite explicit flags")

    monkeypatch.setattr(profiles, "prompt_for_clients", boom)
    monkeypatch.setattr(profiles, "prompt_for_email", boom)
    assert profiles.create_profile("work", email="me@corp.example", with_client=["teams"]) == 0


def test_prompts_feed_the_same_path_as_the_flags(monkeypatch, tmp_config, clean_env):
    """What the prompt collects is handed to create_profile's client seeding
    verbatim, so prompt and flag cannot drift apart."""
    calls = []
    _stub_setup(monkeypatch, calls)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(profiles, "prompt_for_email", lambda alias: "me@corp.example")
    monkeypatch.setattr(profiles, "prompt_for_clients", lambda alias: ["teams", "devops=MyOrg"])
    seeded = {}
    monkeypatch.setattr(
        profiles,
        "_seed_bound_clients",
        lambda alias, with_client, **k: seeded.update(alias=alias, with_client=with_client),
    )

    assert profiles.create_profile("work") == 0
    assert seeded == {"alias": "work", "with_client": ["teams", "devops=MyOrg"]}
    assert calls[0]["email"] == "me@corp.example"


def test_tui_add_flow_keeps_its_own_blank_email_answer(monkeypatch, tmp_config, clean_env):
    """The TUI asks for the email itself, where blank means "paste flow".
    create_profile must not treat that as "nobody asked" and ask again."""
    calls = []
    _stub_setup(monkeypatch, calls)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def boom(*a, **k):
        raise AssertionError("re-asked for an email the TUI already collected")

    monkeypatch.setattr(profiles, "prompt_for_email", boom)
    monkeypatch.setattr(profiles, "prompt_for_clients", lambda alias: [])

    assert profiles.create_profile("work", email=None, ask_email=False) == 0
    assert calls[0]["email"] is None
