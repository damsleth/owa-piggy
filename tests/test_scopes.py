"""Tests for audience resolution.

Exercises the precedence chain: scope override > named audience >
OWA_DEFAULT_AUDIENCE > DEFAULT_AUDIENCE (graph).
"""
from owa_piggy.oauth import ORIGIN, origin_for_client
from owa_piggy.scopes import DEFAULT_AUDIENCE, KNOWN_AUDIENCES, resolve_audience


def test_default_audience_is_graph(clean_env):
    """Regression anchor for commit dc7662e: no flags, no env = graph."""
    scope, err = resolve_audience()
    assert err == ''
    assert DEFAULT_AUDIENCE == 'https://graph.microsoft.com'
    assert scope.startswith('https://graph.microsoft.com/.default ')
    assert 'offline_access' in scope


def test_named_audience_graph(clean_env):
    scope, err = resolve_audience(audience='graph')
    assert err == ''
    assert scope.startswith('https://graph.microsoft.com/.default ')


def test_named_audience_outlook(clean_env):
    scope, err = resolve_audience(audience='outlook')
    assert err == ''
    assert scope.startswith('https://outlook.office.com/.default ')


def test_named_audience_teams(clean_env):
    scope, err = resolve_audience(audience='teams')
    assert err == ''
    assert scope.startswith('https://api.spaces.skype.com/.default ')


def test_named_audience_csa(clean_env):
    scope, err = resolve_audience(audience='csa')
    assert err == ''
    assert scope.startswith('https://chatsvcagg.teams.microsoft.com/.default ')


def test_named_audience_presence(clean_env):
    scope, err = resolve_audience(audience='presence')
    assert err == ''
    assert scope.startswith('https://presence.teams.microsoft.com/.default ')


def test_named_audience_uis(clean_env):
    scope, err = resolve_audience(audience='uis')
    assert err == ''
    assert scope.startswith('https://uis.teams.microsoft.com/.default ')


def test_origin_defaults_to_outlook_for_unknown_client():
    assert origin_for_client('00000000-0000-0000-0000-000000000000') == ORIGIN


def test_origin_for_teams_web_client():
    # Teams web app (5e3ce6c0) is registered against a Teams origin; AAD's
    # cross-origin check rejects the Outlook origin for it.
    assert (
        origin_for_client('5e3ce6c0-2b1f-4285-8d4b-75ee78787346')
        == 'https://teams.microsoft.com'
    )


def test_origin_explicit_override_wins():
    assert (
        origin_for_client('5e3ce6c0-2b1f-4285-8d4b-75ee78787346', 'https://custom.example')
        == 'https://custom.example'
    )


def test_unknown_audience_errors(clean_env):
    scope, err = resolve_audience(audience='nonesuch')
    assert scope == ''
    assert 'unknown audience' in err
    assert 'nonesuch' in err


def test_scope_override_wins_over_audience(clean_env):
    scope, err = resolve_audience(audience='graph', scope='custom-scope-value')
    assert err == ''
    assert scope == 'custom-scope-value'


def test_env_default_audience_short_name(monkeypatch, clean_env):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'teams')
    scope, err = resolve_audience()
    assert err == ''
    assert scope.startswith('https://api.spaces.skype.com/.default ')


def test_env_default_audience_full_url(monkeypatch, clean_env):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'https://example.invalid/')
    scope, err = resolve_audience()
    assert err == ''
    assert scope.startswith('https://example.invalid/.default ')


def test_audience_arg_beats_env(monkeypatch, clean_env):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'teams')
    scope, err = resolve_audience(audience='graph')
    assert err == ''
    assert scope.startswith('https://graph.microsoft.com/.default ')


def test_malformed_env_falls_back_to_default(monkeypatch, clean_env, capsys):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'not-a-url-not-a-name')
    scope, err = resolve_audience()
    assert err == ''
    assert scope.startswith(f'{DEFAULT_AUDIENCE}/.default ')
    captured = capsys.readouterr()
    assert 'WARNING' in captured.err
    assert 'OWA_DEFAULT_AUDIENCE' in captured.err


def test_known_audiences_has_graph_default():
    assert 'graph' in KNOWN_AUDIENCES
    url, desc = KNOWN_AUDIENCES['graph']
    assert url == DEFAULT_AUDIENCE
    assert 'default' in desc.lower()


# --- profile_default tier ---------------------------------------------


def test_profile_default_short_name_used_when_no_flag_or_env(clean_env):
    """Per-profile OWA_DEFAULT_AUDIENCE picks the audience when the
    caller didn't pass --audience/--scope and the env override is unset."""
    scope, err = resolve_audience(profile_default='outlook')
    assert err == ''
    assert scope.startswith('https://outlook.office.com/.default ')


def test_profile_default_full_url_accepted(clean_env):
    scope, err = resolve_audience(profile_default='https://example.invalid/')
    assert err == ''
    assert scope.startswith('https://example.invalid/.default ')


def test_env_beats_profile_default(monkeypatch, clean_env):
    """Env OWA_DEFAULT_AUDIENCE wins over the per-profile setting so a
    one-shot override stays predictable across all profiles."""
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'teams')
    scope, err = resolve_audience(profile_default='outlook')
    assert err == ''
    assert scope.startswith('https://api.spaces.skype.com/.default ')


def test_audience_flag_beats_profile_default(clean_env):
    """An explicit --audience always wins over both env and per-profile."""
    scope, err = resolve_audience(audience='graph', profile_default='outlook')
    assert err == ''
    assert scope.startswith('https://graph.microsoft.com/.default ')


def test_malformed_profile_default_falls_back(clean_env, capsys):
    scope, err = resolve_audience(profile_default='not-a-name-not-a-url')
    assert err == ''
    assert scope.startswith(f'{DEFAULT_AUDIENCE}/.default ')
    captured = capsys.readouterr()
    assert 'WARNING' in captured.err
    assert 'profile' in captured.err
