"""Tests for scope resolution.

Exercises the precedence chain: --scope > --<known> > OWA_DEFAULT_AUDIENCE
> DEFAULT_AUDIENCE (graph).
"""
import pytest

from owa_piggy.scopes import DEFAULT_AUDIENCE, KNOWN_SCOPES, resolve_scope


def test_default_audience_is_graph(clean_env):
    """Regression anchor for commit dc7662e: no flags, no env = graph."""
    scope, err = resolve_scope([])
    assert err is None
    assert DEFAULT_AUDIENCE == 'https://graph.microsoft.com'
    assert scope.startswith('https://graph.microsoft.com/.default ')
    assert 'offline_access' in scope


@pytest.mark.parametrize('flag,expected_prefix', [
    ('--graph', 'https://graph.microsoft.com'),
    ('--outlook', 'https://outlook.office.com'),
    ('--teams', 'https://api.spaces.skype.com'),
    ('--azure', 'https://management.azure.com'),
    ('--keyvault', 'https://vault.azure.net'),
    ('--devops', 'https://app.vssps.visualstudio.com'),
])
def test_known_flag(clean_env, flag, expected_prefix):
    scope, err = resolve_scope([flag])
    assert err is None
    assert scope.startswith(f'{expected_prefix}/.default ')


def test_scope_override_wins_over_flags(clean_env):
    scope, err = resolve_scope(['--graph', '--scope', 'custom-scope-value'])
    assert err is None
    assert scope == 'custom-scope-value'


def test_scope_without_value_errors(clean_env):
    scope, err = resolve_scope(['--scope'])
    assert err == '--scope requires a value'
    assert scope is None


def test_env_default_audience_short_name(monkeypatch, clean_env):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'teams')
    scope, err = resolve_scope([])
    assert err is None
    assert scope.startswith('https://api.spaces.skype.com/.default ')


def test_env_default_audience_full_url(monkeypatch, clean_env):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'https://example.invalid/')
    scope, err = resolve_scope([])
    assert err is None
    assert scope.startswith('https://example.invalid/.default ')


def test_cli_flag_beats_env(monkeypatch, clean_env):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'teams')
    scope, err = resolve_scope(['--graph'])
    assert err is None
    assert scope.startswith('https://graph.microsoft.com/.default ')


def test_malformed_env_falls_back_to_default(monkeypatch, clean_env, capsys):
    monkeypatch.setenv('OWA_DEFAULT_AUDIENCE', 'not-a-url-not-a-name')
    scope, err = resolve_scope([])
    assert err is None
    assert scope.startswith(f'{DEFAULT_AUDIENCE}/.default ')
    captured = capsys.readouterr()
    assert 'WARNING' in captured.err
    assert 'OWA_DEFAULT_AUDIENCE' in captured.err


def test_known_scopes_has_graph_default():
    assert 'graph' in KNOWN_SCOPES
    url, desc = KNOWN_SCOPES['graph']
    assert url == DEFAULT_AUDIENCE
    assert 'default' in desc.lower()
