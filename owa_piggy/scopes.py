"""Audience / scope resolution.

`resolve_audience(audience, scope)` turns a parsed (audience-name,
scope-override) pair plus the env default into the `scope` string we
POST to AAD. The CLI layer does the argv parsing; this module is pure.
"""
import os
import sys

# Default audience for no-flag invocations. Graph is a strict superset of
# Outlook REST (mail/calendar/contacts/tasks) plus OneDrive, Teams,
# SharePoint, directory, and the rest of the Microsoft first-party surface,
# so it's the more useful default. Override per-call with `--audience`
# or `--scope`, or persistently via OWA_DEFAULT_AUDIENCE which accepts
# either a KNOWN_AUDIENCES short name or a full https URL.
DEFAULT_AUDIENCE = 'https://graph.microsoft.com'

# Well-known FOCI-accessible audiences (same refresh token works for all).
# Short names map to audience URLs; `{audience}/.default` is the scope we
# actually ask AAD for.
KNOWN_AUDIENCES = {
    'outlook':    ('https://outlook.office.com',                   'Outlook REST'),
    'graph':      ('https://graph.microsoft.com',                  'Microsoft Graph (default)'),
    'teams':      ('https://api.spaces.skype.com',                 'Microsoft Teams'),
    'azure':      ('https://management.azure.com',                 'Azure Resource Manager'),
    'keyvault':   ('https://vault.azure.net',                      'Azure Key Vault'),
    'storage':    ('https://storage.azure.com',                    'Azure Blob/Table/Queue Storage'),
    'sql':        ('https://database.windows.net',                 'Azure SQL'),
    'outlook365': ('https://outlook.office365.com',                'Outlook REST (alternate)'),
    'substrate':  ('https://substrate.office.com',                 'Office Substrate (Copilot, search)'),
    'manage':     ('https://manage.office.com',                    'Office Management API'),
    'powerbi':    ('https://analysis.windows.net/powerbi/api',     'Power BI'),
    'flow':       ('https://service.flow.microsoft.com',           'Power Automate'),
    'devops':     ('https://app.vssps.visualstudio.com',           'Azure DevOps'),
}


def resolve_audience(audience=None, scope=None):
    """Compute the scope string to request, honoring precedence:
      1. `scope`                 - explicit --scope value, returned as-is
      2. `audience`              - --audience short name (must be in KNOWN_AUDIENCES)
      3. OWA_DEFAULT_AUDIENCE    - short name or full https URL
      4. DEFAULT_AUDIENCE        - graph

    Returns `(scope_string, err)`. err is '' on success. An unknown
    `audience` short name produces an error so typos fail loudly;
    a malformed OWA_DEFAULT_AUDIENCE logs a warning to stderr and falls
    back to the built-in default, because env misconfiguration should not
    silently break every invocation.
    """
    if scope:
        return scope, ''

    if audience:
        if audience not in KNOWN_AUDIENCES:
            return '', (
                f'unknown audience {audience!r}. '
                f'Run `owa-piggy audiences` for the list of known names.'
            )
        url = KNOWN_AUDIENCES[audience][0]
        return f'{url}/.default openid profile offline_access', ''

    env = os.environ.get('OWA_DEFAULT_AUDIENCE', '').strip()
    aud_url = None
    if env:
        if env in KNOWN_AUDIENCES:
            aud_url = KNOWN_AUDIENCES[env][0]
        elif env.startswith('https://'):
            aud_url = env.rstrip('/')
        else:
            print(f'WARNING: OWA_DEFAULT_AUDIENCE={env!r} is not a known '
                  f'short name or an https URL; using default',
                  file=sys.stderr)
    if aud_url is None:
        aud_url = DEFAULT_AUDIENCE
    return f'{aud_url}/.default openid profile offline_access', ''
