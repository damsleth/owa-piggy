"""Per-profile bound-client store and audience->client routing."""

import json

import pytest

from owa_piggy import clients
from owa_piggy.config import DEVOPS_CLIENT_ID
from owa_piggy.oauth import CLIENT_ID as FOCI_CLIENT_ID

TEAMS = clients.TEAMS_WEB_CLIENT_ID
SPACES_SCOPE = "https://api.spaces.skype.com/.default openid profile offline_access"
GRAPH_SCOPE = "https://graph.microsoft.com/.default openid profile offline_access"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """An empty profile tree with one alias, 'work'."""
    from owa_piggy import config as config_mod

    root = tmp_path / "owa-piggy"
    monkeypatch.setattr(config_mod, "ROOT_DIR", root)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", root / "profiles" / "work" / "config")
    (root / "profiles" / "work").mkdir(parents=True)
    return "work"


# --- store ------------------------------------------------------------


def test_missing_store_is_empty_not_an_error(profile):
    assert clients.load_clients(profile) == {}
    assert clients.capture_targets(profile) == []


def test_save_and_load_round_trip(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-1")
    entry = clients.load_clients(profile)[TEAMS]
    assert entry["refresh_token"] == "rt-1"
    # Origin and capture URL default from the known-client registry so the
    # caller never has to restate them.
    assert entry["origin"] == "https://teams.microsoft.com"
    assert entry["capture_url"] == "https://teams.microsoft.com/"
    assert entry["rt_issued_at"]


def test_store_is_owner_only(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-1")
    mode = clients.clients_path(profile).stat().st_mode & 0o777
    assert mode == 0o600


def test_saving_one_client_preserves_the_others(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-teams")
    clients.save_client(
        profile,
        DEVOPS_CLIENT_ID,
        refresh_token="rt-ado",
        capture_url="https://dev.azure.com/org/p/_workitems",
    )
    clients.save_client(profile, TEAMS, refresh_token="rt-teams-2")
    stored = clients.load_clients(profile)
    assert stored[TEAMS]["refresh_token"] == "rt-teams-2"
    assert stored[DEVOPS_CLIENT_ID]["refresh_token"] == "rt-ado"


def test_corrupt_store_degrades_to_empty(profile):
    clients.clients_path(profile).write_text("{not json")
    assert clients.load_clients(profile) == {}


def test_forget_client_removes_only_that_client(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-teams")
    clients.save_client(
        profile,
        DEVOPS_CLIENT_ID,
        refresh_token="rt-ado",
        capture_url="https://dev.azure.com/org/p/_workitems",
    )
    assert clients.forget_client(profile, TEAMS) is True
    assert list(clients.load_clients(profile)) == [DEVOPS_CLIENT_ID]
    assert clients.forget_client(profile, TEAMS) is False


# --- declaration (the profile's site list) ----------------------------


def test_declare_records_a_site_without_a_token(profile):
    entry, err = clients.declare_client(profile, TEAMS)
    assert err == ""
    assert entry["capture_url"] == "https://teams.microsoft.com/"
    assert entry["refresh_token"] == ""
    # Declared but not captured: reseed knows where to go, routing does not
    # hand callers an empty token.
    assert clients.capture_targets(profile)
    assert clients.select_for_scope(profile, SPACES_SCOPE) == (None, None)


def test_declare_requires_a_url_for_org_specific_clients(profile):
    entry, err = clients.declare_client(profile, DEVOPS_CLIENT_ID)
    assert entry is None
    assert "capture URL" in err
    entry, err = clients.declare_client(
        profile, DEVOPS_CLIENT_ID, capture_url="https://dev.azure.com/org/p/_workitems"
    )
    assert err == ""
    assert entry["capture_url"] == "https://dev.azure.com/org/p/_workitems"


def test_declare_keeps_an_existing_token(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-1")
    clients.declare_client(profile, TEAMS)
    assert clients.load_clients(profile)[TEAMS]["refresh_token"] == "rt-1"


# --- --with-client parsing --------------------------------------------


def test_parse_spec_forms():
    assert clients.parse_spec("teams") == (TEAMS, None, "")
    cid, url, err = clients.parse_spec("devops=https://dev.azure.com/o/p/_workitems")
    assert (cid, url, err) == (DEVOPS_CLIENT_ID, "https://dev.azure.com/o/p/_workitems", "")
    _, _, err = clients.parse_spec("nonesuch")
    assert "unknown client" in err
    _, _, err = clients.parse_spec("")
    assert err


def test_parse_spec_accepts_a_raw_client_id_with_a_url():
    raw = "00000000-1111-2222-3333-444444444444"
    cid, url, err = clients.parse_spec(f"{raw}=https://example.invalid/app")
    assert (cid, url, err) == (raw, "https://example.invalid/app", "")
    # ...but not without one: we have no default sign-in URL for it.
    _, _, err = clients.parse_spec(raw)
    assert err


# --- routing ----------------------------------------------------------


def test_teams_audiences_route_to_the_teams_client(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-teams")
    for scope in (
        SPACES_SCOPE,
        "https://ic3.teams.office.com/.default openid",
        "https://chatsvcagg.teams.microsoft.com/.default openid",
        # The authsvc audience itself - the 410 ApiRestricted one.
        "https://teams.microsoft.com/.default openid",
    ):
        cid, entry = clients.select_for_scope(profile, scope)
        assert cid == TEAMS, scope
        assert entry["refresh_token"] == "rt-teams"


def test_other_audiences_stay_on_the_foci_token(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-teams")
    assert clients.select_for_scope(profile, GRAPH_SCOPE) == (None, None)
    assert clients.select_for_scope(profile, "https://outlook.office.com/.default openid") == (
        None,
        None,
    )


def test_a_client_the_profile_lacks_does_not_route(profile):
    assert clients.select_for_scope(profile, SPACES_SCOPE) == (None, None)


def test_an_explicit_scope_has_no_audience_to_route_on(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-teams")
    assert clients.select_for_scope(profile, "some-custom-scope") == (None, None)
    assert clients.audience_from_scope("") == ""


def test_overlay_swaps_client_token_and_origin_without_mutating_config(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-teams")
    config = {"OWA_REFRESH_TOKEN": "rt-foci", "OWA_TENANT_ID": "tid", "OWA_EMAIL": "a@b.c"}
    _, entry = clients.select_for_scope(profile, SPACES_SCOPE)
    overlaid = clients.overlay_config(config, TEAMS, entry)
    assert overlaid["OWA_REFRESH_TOKEN"] == "rt-teams"
    assert overlaid["OWA_CLIENT_ID"] == TEAMS
    assert overlaid["OWA_ORIGIN"] == "https://teams.microsoft.com"
    assert overlaid["OWA_EMAIL"] == "a@b.c"
    # The caller's config still describes the profile's own FOCI token.
    assert config["OWA_REFRESH_TOKEN"] == "rt-foci"
    assert "OWA_CLIENT_ID" not in config


# --- folding legacy client-bound profiles ------------------------------


def _write_profile(root, alias, **kv):
    d = root / "profiles" / alias
    d.mkdir(parents=True, exist_ok=True)
    (d / "config").write_text("".join(f'{k}="{v}"\n' for k, v in kv.items()))


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """A FOCI profile and a client-bound profile for the same identity."""
    from owa_piggy import config as config_mod

    root = tmp_path / "owa-piggy"
    monkeypatch.setattr(config_mod, "ROOT_DIR", root)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", root / "profiles" / "me" / "config")
    _write_profile(
        root, "me", OWA_REFRESH_TOKEN="1.foci", OWA_TENANT_ID="tid-1", OWA_EMAIL="me@corp.example"
    )
    _write_profile(
        root,
        "me-teams",
        OWA_REFRESH_TOKEN="1.teams",
        OWA_CLIENT_ID=TEAMS,
        OWA_TENANT_ID="tid-1",
        OWA_EMAIL="ME@corp.example",
        OWA_ORIGIN="https://teams.microsoft.com",
        OWA_CAPTURE_URL="https://teams.microsoft.com/",
    )
    (root / "profiles.conf").write_text('OWA_PROFILES="me me-teams"\n')
    return root


def test_fold_candidates_pairs_bound_profile_with_its_identity(two_profiles):
    assert clients.fold_candidates() == [("me-teams", "me", TEAMS)]


def test_fold_moves_the_token_and_leaves_a_pointer(two_profiles):
    assert clients.fold_into_parent("me-teams", "me", TEAMS) is True
    assert clients.load_clients("me")[TEAMS]["refresh_token"] == "1.teams"
    folded = (two_profiles / "profiles" / "me-teams" / "config").read_text()
    assert 'OWA_FOLDED_INTO="me"' in folded
    # Moved, not copied: two profiles rotating one RT independently is how
    # one of them ends up superseded by AAD.
    assert 'OWA_REFRESH_TOKEN=""' in folded
    # Idempotent - a folded profile is no longer a candidate.
    assert clients.fold_candidates() == []


def test_fold_ignores_profiles_of_a_different_identity(tmp_path, monkeypatch):
    from owa_piggy import config as config_mod

    root = tmp_path / "owa-piggy"
    monkeypatch.setattr(config_mod, "ROOT_DIR", root)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", root / "profiles" / "a" / "config")
    _write_profile(
        root, "a", OWA_REFRESH_TOKEN="1.a", OWA_TENANT_ID="tid-a", OWA_EMAIL="a@corp.example"
    )
    _write_profile(
        root,
        "b-teams",
        OWA_REFRESH_TOKEN="1.b",
        OWA_CLIENT_ID=TEAMS,
        OWA_TENANT_ID="tid-b",
        OWA_EMAIL="b@corp.example",
    )
    (root / "profiles.conf").write_text('OWA_PROFILES="a b-teams"\n')
    assert clients.fold_candidates() == []


def test_a_plain_foci_profile_is_never_a_fold_candidate(tmp_path, monkeypatch):
    from owa_piggy import config as config_mod

    root = tmp_path / "owa-piggy"
    monkeypatch.setattr(config_mod, "ROOT_DIR", root)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", root / "profiles" / "a" / "config")
    _write_profile(
        root,
        "a",
        OWA_REFRESH_TOKEN="1.a",
        OWA_CLIENT_ID=FOCI_CLIENT_ID,
        OWA_TENANT_ID="tid",
        OWA_EMAIL="a@corp.example",
    )
    _write_profile(
        root, "b", OWA_REFRESH_TOKEN="1.b", OWA_TENANT_ID="tid", OWA_EMAIL="a@corp.example"
    )
    (root / "profiles.conf").write_text('OWA_PROFILES="a b"\n')
    assert clients.fold_candidates() == []


def test_fold_stamp_makes_the_scan_run_once(two_profiles):
    from owa_piggy.migration import fold_bound_clients_if_needed

    assert fold_bound_clients_if_needed() == [("me-teams", "me")]
    assert (two_profiles / "fold-bound-clients.done").exists()
    # Second run does not even scan, so a hand-made bound profile added
    # later is not silently swallowed.
    _write_profile(
        two_profiles,
        "me-ado",
        OWA_REFRESH_TOKEN="1.ado",
        OWA_CLIENT_ID=DEVOPS_CLIENT_ID,
        OWA_TENANT_ID="tid-1",
        OWA_EMAIL="me@corp.example",
    )
    assert fold_bound_clients_if_needed() == []


def test_store_is_valid_json_on_disk(profile):
    clients.save_client(profile, TEAMS, refresh_token="rt-1")
    data = json.loads(clients.clients_path(profile).read_text())
    assert list(data) == [TEAMS]
