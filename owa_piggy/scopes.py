"""Scope / audience resolution.

`resolve_scope` turns argv + env into the `scope` string we POST to AAD.
"""
import os
import sys

# Default audience for no-flag invocations. Graph is a strict superset of
# Outlook REST (mail/calendar/contacts/tasks) plus OneDrive, Teams,
# SharePoint, directory, and the rest of the Microsoft first-party surface,
# so it's the more useful default. Override with `--outlook` (or any other
# --<name> flag), with --scope, or persistently via OWA_DEFAULT_AUDIENCE
# which accepts either a KNOWN_AUDIENCES short name or a full https URL.
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


def resolve_scope(args):
    """Compute the scope string to request, honoring precedence:
      1. --scope <explicit>      (raw string, returned as-is)
      2. --<known-name>          (e.g. --outlook, --graph, --teams)
      3. OWA_DEFAULT_AUDIENCE    (short name or full https URL)
      4. DEFAULT_AUDIENCE        (graph)

    Returns (scope, error_message). error_message is non-empty only when
    --scope was given without a value; the caller should treat that as a
    fatal arg error and bail.

    A malformed OWA_DEFAULT_AUDIENCE logs a warning and falls back to the
    built-in default rather than erroring - a typo in an env var should
    not silently break every invocation."""
    # --scope wins over everything else.
    if '--scope' in args:
        idx = args.index('--scope')
        if idx + 1 < len(args):
            return args[idx + 1], None
        return None, '--scope requires a value'

    # Known-name flag.
    for name, entry in KNOWN_AUDIENCES.items():
        if f'--{name}' in args:
            return f'{entry[0]}/.default openid profile offline_access', None

    # Env var default.
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
    return f'{aud_url}/.default openid profile offline_access', None
