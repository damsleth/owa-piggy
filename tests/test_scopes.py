"""Tests for audience resolution.

Exercises the precedence chain: scope override > named audience >
OWA_DEFAULT_AUDIENCE > DEFAULT_AUDIENCE (graph).
"""
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
