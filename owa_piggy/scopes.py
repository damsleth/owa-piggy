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
DEFAULT_AUDIENCE = "https://graph.microsoft.com"

# Well-known FOCI-accessible audiences (same refresh token works for all).
# Short names map to audience URLs; `{audience}/.default` is the scope we
# actually ask AAD for.
KNOWN_AUDIENCES = {
    "outlook": ("https://outlook.office.com", "Outlook REST"),
    "graph": ("https://graph.microsoft.com", "Microsoft Graph (default)"),
    "teams": (
        "https://api.spaces.skype.com",
        "Microsoft Teams middle-tier (mt/part, Skype audience)",
    ),
    "ic3": ("https://ic3.teams.office.com", "Microsoft Teams chatsvc / asyncgw (modern)"),
    "csa": (
        "https://chatsvcagg.teams.microsoft.com",
        "Microsoft Teams chat-service aggregator (csa: updates, chatsAndTeams)",
    ),
    "presence": ("https://presence.teams.microsoft.com", "Microsoft Teams presence / pubsub (ups)"),
    "uis": ("https://uis.teams.microsoft.com", "Microsoft Teams user/notification settings (nss)"),
    "azure": ("https://management.azure.com", "Azure Resource Manager"),
    "keyvault": ("https://vault.azure.net", "Azure Key Vault"),
    "storage": ("https://storage.azure.com", "Azure Blob/Table/Queue Storage"),
    "sql": ("https://database.windows.net", "Azure SQL"),
    "outlook365": ("https://outlook.office365.com", "Outlook REST (alternate)"),
    "substrate": ("https://substrate.office.com", "Office Substrate (Copilot, search)"),
    "manage": ("https://manage.office.com", "Office Management API"),
    "powerbi": ("https://analysis.windows.net/powerbi/api", "Power BI"),
    "flow": ("https://service.flow.microsoft.com", "Power Automate"),
    "devops": ("https://app.vssps.visualstudio.com", "Azure DevOps"),
}

# Tenant-templated audiences. Unlike KNOWN_AUDIENCES, SharePoint's resource
# URL embeds the tenant's SharePoint name (the `.onmicrosoft.com` prefix,
# e.g. `norconsult365`), which is NOT the AAD tenant GUID and NOT derivable
# from the user's email domain. The `{tenant}` placeholder is filled from
# (in precedence) an explicit --sharepoint-tenant flag, the
# OWA_SHAREPOINT_TENANT env var, or the per-profile OWA_SHAREPOINT_TENANT
# config field. The FOCI refresh token captured from the Outlook sign-in
# works for these resources unchanged - only the requested scope differs.
KNOWN_AUDIENCE_TEMPLATES = {
    "sharepoint": ("https://{tenant}.sharepoint.com", "SharePoint site collections / content"),
    "sharepoint-admin": (
        "https://{tenant}-admin.sharepoint.com",
        "SharePoint tenant admin (CSOM/REST)",
    ),
}


def templated_audience_name(audience=None, scope=None, profile_default=None):
    """Return the tenant-templated audience short name that resolve_audience
    WOULD select (ignoring whether a tenant is actually available), or None.

    Callers use this to decide whether to auto-derive the SharePoint tenant
    before resolving the scope. Mirrors resolve_audience's precedence:
    an explicit --scope short-circuits everything; --audience wins next;
    otherwise OWA_DEFAULT_AUDIENCE (env) then the profile default decide,
    and a non-templated value at either layer means "not templated"."""
    if scope:
        return None
    if audience:
        return audience if audience in KNOWN_AUDIENCE_TEMPLATES else None
    env = os.environ.get("OWA_DEFAULT_AUDIENCE", "").strip()
    if env:
        return env if env in KNOWN_AUDIENCE_TEMPLATES else None
    if profile_default:
        pd = profile_default.strip()
        return pd if pd in KNOWN_AUDIENCE_TEMPLATES else None
    return None


def _resolve_sharepoint_tenant(sharepoint_tenant, profile_sharepoint_tenant):
    """Resolve the SharePoint tenant name, honoring precedence:
      1. `sharepoint_tenant`         - explicit --sharepoint-tenant flag
      2. OWA_SHAREPOINT_TENANT       - env
      3. `profile_sharepoint_tenant` - per-profile config field

    Returns the tenant name string, or '' if none is set.
    """
    if sharepoint_tenant and sharepoint_tenant.strip():
        return sharepoint_tenant.strip()
    env = os.environ.get("OWA_SHAREPOINT_TENANT", "").strip()
    if env:
        return env
    if profile_sharepoint_tenant and profile_sharepoint_tenant.strip():
        return profile_sharepoint_tenant.strip()
    return ""


def resolve_audience(
    audience=None,
    scope=None,
    profile_default=None,
    sharepoint_tenant=None,
    profile_sharepoint_tenant=None,
):
    """Compute the scope string to request, honoring precedence:
      1. `scope`                 - explicit --scope value, returned as-is
      2. `audience`              - --audience short name (KNOWN_AUDIENCES or a
                                   tenant-templated name like `sharepoint`)
      3. OWA_DEFAULT_AUDIENCE    - short name or full https URL (env)
      4. `profile_default`       - per-profile config OWA_DEFAULT_AUDIENCE
      5. DEFAULT_AUDIENCE        - graph

    Tenant-templated audiences (see KNOWN_AUDIENCE_TEMPLATES) need a
    SharePoint tenant name, resolved from `sharepoint_tenant` (flag),
    OWA_SHAREPOINT_TENANT (env), or `profile_sharepoint_tenant` (config).

    Returns `(scope_string, err)`. err is '' on success. An unknown
    `audience` short name produces an error so typos fail loudly;
    a malformed OWA_DEFAULT_AUDIENCE (env) logs a warning to stderr and
    falls back to the next layer, because env misconfiguration should not
    silently break every invocation. A malformed `profile_default` is
    treated the same way - warn and fall through to graph.
    """
    if scope:
        return scope, ""

    sp_tenant = _resolve_sharepoint_tenant(sharepoint_tenant, profile_sharepoint_tenant)

    if audience:
        if audience in KNOWN_AUDIENCES:
            url = KNOWN_AUDIENCES[audience][0]
        elif audience in KNOWN_AUDIENCE_TEMPLATES:
            if not sp_tenant:
                return "", (
                    f"audience {audience!r} needs a SharePoint tenant name. "
                    f"Pass --sharepoint-tenant <name>, set OWA_SHAREPOINT_TENANT, "
                    f"or add OWA_SHAREPOINT_TENANT=<name> to the profile config."
                )
            url = KNOWN_AUDIENCE_TEMPLATES[audience][0].format(tenant=sp_tenant)
        else:
            return "", (
                f"unknown audience {audience!r}. "
                f"Run `owa-piggy audiences` for the list of known names."
            )
        return f"{url}/.default openid profile offline_access", ""

    env = os.environ.get("OWA_DEFAULT_AUDIENCE", "").strip()
    aud_url, env_err = _audience_url_from_default(env, sp_tenant, "OWA_DEFAULT_AUDIENCE")
    if env_err:
        return "", env_err
    if aud_url is None and profile_default:
        aud_url, pd_err = _audience_url_from_default(
            profile_default.strip(), sp_tenant, "profile OWA_DEFAULT_AUDIENCE"
        )
        if pd_err:
            return "", pd_err
    if aud_url is None:
        aud_url = DEFAULT_AUDIENCE
    return f"{aud_url}/.default openid profile offline_access", ""


def _audience_url_from_default(value, sp_tenant, label):
    """Resolve a default-audience value (env or profile config) to an
    audience URL. Accepts a KNOWN_AUDIENCES short name, a tenant-templated
    name, or a full https URL. Returns `(url_or_None, err)`. A malformed
    value warns to stderr and returns `(None, '')` so the caller falls
    through to the next layer; a templated name missing its tenant is a
    hard error (the user clearly meant SharePoint)."""
    if not value:
        return None, ""
    if value in KNOWN_AUDIENCES:
        return KNOWN_AUDIENCES[value][0], ""
    if value in KNOWN_AUDIENCE_TEMPLATES:
        if not sp_tenant:
            return None, (
                f"{label}={value!r} needs a SharePoint tenant name. "
                f"Pass --sharepoint-tenant <name>, set OWA_SHAREPOINT_TENANT, "
                f"or add OWA_SHAREPOINT_TENANT=<name> to the profile config."
            )
        return KNOWN_AUDIENCE_TEMPLATES[value][0].format(tenant=sp_tenant), ""
    if value.startswith("https://"):
        return value.rstrip("/"), ""
    print(
        f"WARNING: {label}={value!r} is not a known short name or an https URL; using default",
        file=sys.stderr,
    )
    return None, ""
