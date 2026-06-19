# owa-piggy

[![PyPI](https://img.shields.io/pypi/v/owa-piggy.svg)](https://pypi.org/project/owa-piggy/)
[![GitHub release](https://img.shields.io/github/v/release/damsleth/owa-piggy.svg)](https://github.com/damsleth/owa-piggy/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![ci](https://github.com/damsleth/owa-piggy/actions/workflows/ci.yml/badge.svg)

Turn your existing Outlook Web session into a reusable API token from the
terminal. No app registration, no tenant admin ask, no client secrets.

`owa-piggy` is the auth broker. The companion
[`owa-tools`](https://github.com/damsleth/owa-tools) ships the `owa-*` CLIs
(`owa`, `owa-cal`, `owa-mail`, `owa-graph`, `owa-doctor`, `owa-people`,
`owa-sched`, `owa-drive`, and more) that borrow tokens from `owa-piggy` -
separate package, separate token store, never imported. `owa-piggy` stays the
only thing that ever touches your refresh tokens, and follows a consistent CLI
contract (output classes, exit codes - documented in `owa_piggy/conventions.py`).

## Install

Homebrew (recommended):

```bash
brew install damsleth/tap/owa-piggy
```

PyPI:

```bash
pipx install owa-piggy
```

Bleeding edge (main): `brew install --HEAD damsleth/tap/owa-piggy`

Then pull in the rest of the suite with one shortcut:

```bash
owa-piggy install-owa-tools           # brew install damsleth/tap/owa-tools
```

## Quickstart

```bash
# 1. One-time auth setup (opens Edge, signs you in, captures a refresh token)
owa-piggy setup --profile work --email you@yourcompany.com

# 2. Verify it works
owa-piggy status

# 3. Use the token
curl -H "Authorization: Bearer $(owa-piggy)" https://graph.microsoft.com/v1.0/me
```

For the Outlook REST audience:

```sh
curl -s -H "Authorization: Bearer $(owa-piggy --audience outlook)" \
  "https://outlook.office.com/api/v2.0/me/messages?\$top=1" | jq -r '.value[0].Subject'
```

For SharePoint tenant admin (CSOM / REST against `<tenant>-admin.sharepoint.com`):

```sh
curl -s -H "Authorization: Bearer $(owa-piggy --audience sharepoint-admin)" \
  -H "Accept: application/json;odata=nometadata" \
  "https://contoso365-admin.sharepoint.com/_api/web/title" | jq -r .value
```

The tenant host is auto-resolved on first use (see [SharePoint](#sharepoint-tenant-admin) below) - no flag needed.

Raw token on stdout, logs on stderr - pipe-friendly by design.

---

## CLI surface

```
owa-piggy <command> [options]
```

Bare `owa-piggy` is shorthand for `owa-piggy token` - the access token goes to stdout, nothing else.

| command                  | what it does                                                          |
| ------------------------ | --------------------------------------------------------------------- |
| `token` (default)        | print access token to stdout (default audience: Microsoft Graph)      |
| `status`                 | compact ISO8601 health summary; all profiles if `--profile` omitted; `--json` emits token health without token values |
| `debug`                  | full setup diagnostics for one profile                                 |
| `setup`                  | interactive first-time setup; creates the profile if new              |
| `reseed`                 | fetch a fresh refresh token headlessly from the Edge sidecar           |
| `decode`                 | print JWT header and payload of the current access token              |
| `remaining`              | print minutes remaining on the current access token                   |
| `audiences`              | list all known FOCI-accessible audiences                              |
| `profiles`               | on a TTY, opens the interactive dashboard (same screen as `tui`); `--json` emits aliases and config presence |
| `profiles list`          | non-interactive list (bare `profiles` without the dashboard); `--json` emits the registry doc - use this in scripts |
| `profiles set-default A` | make `A` the default profile; `--json` emits an action envelope     |
| `profiles delete A`      | remove profile `A`'s config + Edge sidecar dir; `--force` to override default-pointer guard, `--yes` to bypass TTY confirmation (required in non-TTY), `--json` for action envelope |
| `tui`                    | interactive dashboard: profile list + live per-profile token freshness in one screen, with reseed / toggle / set-default / edge actions; falls back to a plain status table when not a TTY. Bare `profiles` on a TTY opens the same screen - `tui` is the explicit name for it |
| `install-owa-tools`      | shorthand for `brew install damsleth/tap/owa-tools` (the companion suite) |
| `version`                | print version information; `--json` emits `{"tool": ..., "version": ...}` |

Top-level flags: `--version`, `--help`. Per-command options (`--profile <alias>`, `--audience <name>`, `--scope <explicit>`) are accepted on the bare invocation too, because it's rewritten to `owa-piggy token <opts>`. `--audience` is validated against the known list at parse time, so typos error out with the full audience set instead of silently using the default. Per-command help: `owa-piggy <command> --help`.

### Machine surface

owa-piggy exposes the same introspection/agent surface as the owa-tools
consumer CLIs, so one agent can drive the whole suite uniformly:

| invocation                 | what it does                                                        |
| -------------------------- | ------------------------------------------------------------------- |
| `owa-piggy schema`         | JSON command schema (`tool`, `suite`, `schema_version`, `commands`) |
| `owa-piggy schema <cmd>`   | schema for one command                                              |
| `owa-piggy --help --json`  | the same schema via the help flag                                   |
| `owa-piggy --agent <cmd>`  | wrap JSON stdout in a stable `{"_owa": …, "data": …}` envelope (or `OWA_AGENT=1`); non-interactive commands only |
| `owa-piggy --err-json <cmd>` | structured JSON error on stderr (or `OWA_ERR_JSON=1`)              |
| `owa-piggy --doctor [--json] [--fix]` | health doctor payload; `--fix` repairs known config permissions |

## Examples

```sh
owa-piggy                              # Graph token (default audience)
owa-piggy --audience outlook           # Outlook REST audience
owa-piggy --audience teams             # Teams audience
owa-piggy --audience sharepoint        # SharePoint site collections / content (host auto-resolved)
owa-piggy --audience sharepoint-admin  # SharePoint tenant admin
owa-piggy remaining                    # minutes left on current token
owa-piggy token --json | jq .scope     # inspect granted scopes
eval $(owa-piggy token --env)          # export ACCESS_TOKEN= / EXPIRES_IN=
owa-piggy status                       # compact ISO8601 health summary
owa-piggy status --json                # machine-readable health, no token values
owa-piggy profiles list                # non-interactive list (safe in scripts)
owa-piggy profiles list --json         # machine-readable profile registry
owa-piggy debug                        # full setup diagnostics
owa-piggy --version                    # print version
owa-piggy version --json               # machine-readable version
```

Token surfaces are intentionally secret-bearing. The raw token, `--json`,
`--env`, and `--agent` token paths return usable credentials because
`owa-piggy` is an auth broker and the goal is highest possible usability with
the least possible friction. Do not treat human or agent output as redacted.
Prefer command substitution such as `eval "$(owa-piggy token --env)"` when you
want to avoid putting token values in shell history, and avoid redirecting token
output to persistent logs unless that is the point of the command.

Pipe-friendly - raw token goes to stdout, everything else to stderr:

```sh
# Fetch calendar events via Graph
curl -s -H "Authorization: Bearer $(owa-piggy)" \
  "https://graph.microsoft.com/v1.0/me/events" | jq .

# Use in scripts
TOKEN=$(owa-piggy)
az rest --headers "Authorization=Bearer $TOKEN" --url "https://graph.microsoft.com/v1.0/me"
```

Default audience is **Microsoft Graph**, which covers everything Outlook REST exposes plus OneDrive, Teams, SharePoint, directory, and more. Override persistently with `OWA_DEFAULT_AUDIENCE=<short-name-or-https-url>`, or per-call with `--audience <name>` (see `owa-piggy audiences`) or `--scope <explicit>`.

### SharePoint tenant admin

SharePoint's resource URL is tenant-specific (`https://<tenant>-admin.sharepoint.com`), unlike the globally-fixed audiences. The same FOCI refresh token captured from the Outlook sign-in works for SharePoint unchanged - only the requested scope differs - so no separate sign-in is needed. Two templated audiences:

| audience | resource | use |
| --- | --- | --- |
| `sharepoint` | `https://<tenant>.sharepoint.com` | site collections / content (the token is valid for every `/sites/...` and `/teams/...` under the host) |
| `sharepoint-admin` | `https://<tenant>-admin.sharepoint.com` | tenant admin CSOM / REST (site-collection admins, tenant settings) |

A token's audience is the **host**, not a specific site - so one `sharepoint` token works across all site collections, while tenant-admin cmdlets need the separate `sharepoint-admin` host.

The `<tenant>` host prefix (the tenant's initial `.onmicrosoft.com` name, e.g. `contoso365`) isn't derivable from your email domain or tenant GUID. owa-piggy resolves it for you: on first use it mints a Graph token, reads the hostname from `GET /sites/root`, and persists it as `OWA_SHAREPOINT_TENANT` on the profile - every later call skips the round-trip. You can also set it explicitly:

```sh
owa-piggy --audience sharepoint                          # content host, tenant auto-resolved + persisted on first use
owa-piggy --audience sharepoint-admin                    # tenant admin host
owa-piggy --audience sharepoint --sharepoint-tenant contoso365   # set explicitly (also persists via setup/profiles new)
owa-piggy profiles new admin --email me@contoso.com --sharepoint-tenant contoso365

# Inspect the token's audience and scopes (look for Sites.FullControl.All):
owa-piggy debug --audience sharepoint-admin | grep -i scp

# Tenant admin REST call - e.g. read a site collection's owner:
TOKEN=$(owa-piggy --audience sharepoint-admin)
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json;odata=nometadata" \
  "https://contoso365-admin.sharepoint.com/_api/web/title"
```

Whether you get tenant-admin capability (e.g. `Sites.FullControl.All` in the token's `scp` claim) depends on the FOCI client's pre-consented delegated permissions **and** your directory roles - check with `owa-piggy debug --audience sharepoint-admin`.

### PnP PowerShell

Pass the token straight into `Connect-PnPOnline -AccessToken` - no app registration or `-ClientId` needed. Match the audience to the URL host: `sharepoint-admin` for tenant cmdlets, `sharepoint` for site work.

```powershell
# Tenant admin
Connect-PnPOnline -Url "https://contoso365-admin.sharepoint.com" -AccessToken (owa-piggy --audience sharepoint-admin)
Get-PnPTenantSite | Select-Object Url, Owner

# One content-host token reused across many site collections (~60 min lifetime; re-mint when it expires)
$token = owa-piggy --audience sharepoint
foreach ($u in Get-Content ./legacy-sites.txt) {
    Connect-PnPOnline -Url $u -AccessToken $token
    Add-PnPSiteCollectionAdmin -Owners "svc-admin@contoso.com"
}
```

Pure CSOM/REST cmdlets work with the SharePoint token; the few Graph-backed PnP cmdlets need a separate `owa-piggy --audience graph` connection instead.

---

## How?

OWA (One Outlook Web) is registered in Azure AD as a public SPA client with ID `9199bf20-a13f-4107-85dc-02114787ef48`. Public clients require no client secret. SPA refresh tokens live in your browser's `localStorage` and can be exchanged at Microsoft's standard OAuth2 token endpoint - the only requirement is that the request includes the `Origin` header AAD expects for SPA clients.

The token comes back with a broad set of delegated scopes: `Calendars.ReadWrite`, `Mail.ReadWrite`, `Files.ReadWrite`, and more. OWA is also a FOCI (Family of Client IDs) member, so the same refresh token works against `outlook.office.com`, `graph.microsoft.com`, and other Microsoft first-party APIs.

| Token         | Lifetime                                                           |
| ------------- | ------------------------------------------------------------------ |
| Access token  | ~60-90 min from issue                                              |
| Refresh token | 24h sliding window (rotates on use) AND 24h absolute hard-cap from original sign-in |

The sliding window renews on every exchange. The hard-cap does not - after 24h AAD returns `AADSTS700084` and the token is unrecoverable via rotation. The launchd agent handles the sliding window; `owa-piggy reseed` handles the hard-cap.

The rotated refresh token is saved automatically to `~/.config/owa-piggy/profiles/<alias>/config` after every exchange (only when the token originally came from the config file - env-only callers keep env-only semantics and get a rotation notice on stderr). A single shared LaunchAgent keeps the sliding window fresh for whichever profiles you opt in:

```sh
owa-piggy profiles schedule default     # add 'default' to the hourly schedule
owa-piggy profiles unschedule default   # remove it again
```

There is **one** LaunchAgent for the whole tool (`com.damsleth.owa-piggy.scheduled`), so macOS's Login Items & Extensions shows a single row no matter how many profiles you run. Which profiles it actually reseeds is the `OWA_SCHEDULED` set in `profiles.conf`, read at run time - so scheduling/unscheduling a profile is a pure config edit that never re-pokes launchd (and never re-prompts for background-item approval). You can also toggle the schedule from the `owa-piggy profiles` TUI (`l` / `u`).

The agent runs hourly via `launchd`'s `StartCalendarInterval` and, unlike cron, fires on wake for any hour that was missed while the Mac was asleep - so an overnight-closed laptop still rotates each scheduled profile's token before the 24h sliding window closes.

---

## Automated reseed (24h hard-cap recovery)

Because hourly rotation only keeps the sliding window alive, you still hit `AADSTS700084` after 24h of continuous use. `owa-piggy reseed` is the automated recovery path - it drives a sidecar Edge profile via the Chrome DevTools Protocol, extracts a fresh FOCI refresh token from MSAL's localStorage, and pipes it into `owa-piggy setup`.

One-time setup of the sidecar profile (per alias):

```sh
alias=default   # or work, personal, client-x ...
dir="$HOME/.config/owa-piggy/profiles/$alias/edge-profile"
mkdir -p "$dir"
/Applications/Microsoft\ Edge.app/Contents/MacOS/Microsoft\ Edge \
  --user-data-dir="$dir" \
  https://outlook.cloud.microsoft
# sign in, then close Edge
```

Thereafter:

```sh
owa-piggy reseed --profile $alias
```

The scraper detects stale caches (ID token JWT `iat` > 23h old), forces a Page.reload if MSAL gets wedged, and if session cookies have also expired it reopens Edge visibly so you can sign in interactively and then scrapes again automatically. When things work the whole thing is silent and takes a second or two.

The `AADSTS700084` error message from the normal flow also prints `hint: run owa-piggy reseed` so you don't need to remember the recipe.

### Mechanism hierarchy

Five token-acquisition mechanisms, ordered from least to most intrusive. `owa-piggy` walks this ladder so the silent paths run first and you only see a browser window when nothing cheaper works.

| # | Mechanism | When it runs | Where |
|---|-----------|--------------|-------|
| 1 | Pure HTTP `POST /oauth2/v2.0/token` (curl-equivalent) | Every `owa-piggy token` call. Trades RT for AT; AAD also rotates the RT in the response. | `owa_piggy/oauth.py` |
| 2 | Headless Edge - legacy MSAL scrape | `reseed` for profiles **without** `OWA_AUTH_MODE=capture`. Defaults to `--headless=new`, reads MSAL `localStorage`. | `scripts/reseed-from-edge.sh` (`HEADLESS=1`) |
| 3 | Headless Edge - network capture via CDP | `reseed` for profiles with `OWA_AUTH_MODE=capture` (encrypted MSAL cache, e.g. Okta-federated). Intercepts the `/oauth2/v2.0/token` response off the wire. | `owa_piggy/capture.py` (default headless) |
| 4 | Offscreen non-headless Edge | Fallback when headless is blocked by Conditional Access / device-compliance. Window parked at `-32000,-32000`. | `OWA_RESEED_HEADLESS=0` or `OWA_CAPTURE_HEADLESS=0`; automatic on `headless_blocked` |
| 5 | Visible Edge | TTY only. Triggered when sidecar cookies expired or AAD rejected the scraped RT (AADSTS700084 after scrape). | `visible_signin()` / `capture.capture_signin()` |

Only step 1 runs continuously. Steps 2-5 fire on `owa-piggy reseed`, which is needed when the RT itself is dead (24h SPA hard-cap). Under `launchd` (no TTY) step 5 is never reached - the reseed bails with an error logged to the per-profile `refresh.log` and the user must re-run interactively. See [.docs/headless-blocked-by-ca.md](.docs/headless-blocked-by-ca.md) for tenant-specific causes that drop the flow to step 4 or 5.

---

## Diagnostics

```sh
owa-piggy status --profile work
```
```
profile:      work
authtoken:    expires 2026-04-20T11:46:51Z (1h24m)
refreshtoken: expires 2026-04-21T09:30:00Z (22h38m)
audience:     graph (https://graph.microsoft.com)
scopes:       default(26)
launchd:      true
```

Without `--profile`, `status` probes every configured profile concurrently and reports them in a stanza per alias (in configuration order). Prints `no valid token` (exit 1) and an `ERROR:` line on stderr if the live probe fails for a profile. The refresh-token expiry is the 24h hard-cap, computed from `OWA_RT_ISSUED_AT` which is stamped on `setup` and `reseed` (setups from before this field landed will show `unknown` until the next reseed). `scopes:` collapses to `default(N)` for the default scope set; an explicit `--scope` request prints the granted scope list verbatim. `launchd:` shows whether a per-profile LaunchAgent is bootstrapped.

`owa-piggy status --json` returns a machine-readable shape for scripts and companion tools: one object per profile with `state` (`ok|fail|disabled`), `access_token.expires_at`, `refresh_token.expires_at`, and `hints[]`.

```sh
owa-piggy debug
```

Full triage dump: config file state, RT shape, live exchange probe, access-token claims (aud/scp/exp/iat), launchd agent status (`gui/<uid>/<label>` bootstrap, runs, last exit code), PATH install, Edge sidecar profile presence, reseed script discoverability. Also warns about leftover legacy cron entries.

---

## Security model

This tool deliberately operates within the boundaries of what Microsoft allows for public SPA clients:

- **No credentials stored in Azure** - there is no app registration to compromise
- **Delegated permissions only** - the token acts as you, with your existing access, nothing more
- **Standard OAuth2 token exchange** - no browser automation in the hot path, no cookie theft, no undocumented APIs
- **Your session, your token** - the refresh token is the same one OWA already stores in your browser; this tool just makes it usable from the terminal

The token is scoped to your user identity. A password change or admin revocation invalidates it immediately - the same as it would in the browser.

See [SECURITY.md](SECURITY.md) for the full threat model and known failure modes.

Per-profile config lives at `~/.config/owa-piggy/profiles/<alias>/config`, mode 0600:

```
OWA_REFRESH_TOKEN="1.AQ..."
OWA_TENANT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
OWA_RT_ISSUED_AT="2026-04-19T10:15:00Z"
```

A small registry at `~/.config/owa-piggy/profiles.conf` tracks which profiles exist and which is the default.

Writes are atomic (temp file + fsync + rename) so a crash mid-rotation cannot corrupt the only live token. Environment variables take precedence over the config file:

- `OWA_REFRESH_TOKEN`, `OWA_TENANT_ID` - override the corresponding config values (when `OWA_REFRESH_TOKEN` is env-supplied, rotated tokens are kept env-only and not written back to disk)
- `OWA_CLIENT_ID` - override the default OWA client ID (set automatically when capturing a non-FOCI client's token, e.g. Azure DevOps)
- `OWA_CAPTURE_URL` - point the capture/reseed sidecar at a non-OWA SPA so its bound refresh token is grabbed off the wire instead of OWA's. Use for non-FOCI clients (e.g. the Azure DevOps app) whose RT the FOCI client cannot mint itself (AADSTS65002 preauth wall). Persisted on the profile so scheduled reseeds rotate the *same* client's token; env wins for ad-hoc runs.
- `OWA_ORIGIN` - override the `Origin` header used in the token exchange. Auto-derived per client; set automatically alongside `OWA_CLIENT_ID` / `OWA_CAPTURE_URL` when capturing a non-FOCI client so the exchange replays under the minting origin.
- `OWA_DEFAULT_AUDIENCE` - change the default audience (a short name from `owa-piggy audiences` like `outlook`, or a full https URL). Command-line `--audience` / `--scope` still wins.
- `OWA_SHAREPOINT_TENANT` - SharePoint tenant name (the `.onmicrosoft.com` prefix, e.g. `contoso365`) used to fill the `{tenant}` placeholder for the `sharepoint` / `sharepoint-admin` audiences. Auto-derived and persisted on first use; `--sharepoint-tenant` overrides it. See [SharePoint](#sharepoint-tenant-admin).
- `OWA_PROFILE` - select the active profile, overriding `OWA_DEFAULT_PROFILE`. Equivalent to `--profile <alias>`.
- `OWA_AUTH_MODE` - stamped on the profile config by `setup` (`scrape` for legacy MSAL paste flow, `capture` for the network-capture flow used by encrypted-MSAL / Okta-federated tenants). `reseed` branches on this to pick the right mechanism.
- `OWA_EMAIL` - account hint stamped on the profile when `setup --email` is used; reseed validates captured tokens against it.
- `OWA_RT_ISSUED_AT` - ISO-8601 timestamp written on every `setup` / `reseed`. Drives the refresh-token hard-cap calculation in `status`.
- `OWA_RESEED_HEADLESS=0`, `OWA_CAPTURE_HEADLESS=0` - escape hatches for tenants whose Conditional Access blocks headless Edge. Drop the reseed/capture flow to an offscreen non-headless window (mechanism step 4 in the hierarchy table above).

---

## Multiple profiles

owa-piggy supports multiple independent tenants / identities via named profiles. Each profile gets its own config, access-token cache, Edge sidecar userdata dir, and launchd job, so a broken reseed on one profile does not knock out the others.

```sh
owa-piggy setup --profile work                # create a new profile
owa-piggy setup --profile personal            # ...and another
owa-piggy --profile work                      # raw token for 'work'
OWA_PROFILE=work owa-piggy                    # same, via env
owa-piggy tui                                 # dashboard: profiles + live token freshness
owa-piggy profiles                            # same dashboard on a TTY (explicit name: tui)
owa-piggy profiles list                       # non-interactive list (scripts, CI)
owa-piggy profiles list --json                # machine-readable registry
owa-piggy profiles set-default work           # change the default pointer
owa-piggy status --profile personal           # health check, per profile
owa-piggy reseed --profile work               # recover one profile after 24h
owa-piggy profiles delete personal            # remove a profile (config + Edge)
owa-piggy profiles schedule work              # add 'work' to the shared hourly agent
owa-piggy profiles unschedule work            # stop auto-reseeding 'work'
```

`owa-piggy tui` is the one-screen answer to *"are my tokens fresh, and if not, fix it"*: a profile list with single-key actions (toggle, set-default, schedule, add/delete, reseed, edge) plus a live freshness column from a `status` probe of every profile — green `fresh 58m`, yellow `expiring 4m`, red with the fix hint when a profile needs a reseed. Reseeding (`r`/`R`), toggling, and adding re-probe automatically; `g` forces a refresh. Probing is network-bound, so the screen paints a `probing...` skeleton first, then fills in results. Bare `owa-piggy profiles` on a TTY opens this same dashboard — `tui` is just the explicit, discoverable name for it. For scripts and CI, use `profiles list` (offline, no probe) or `status --json`.

Selection precedence when `--profile` is omitted: `OWA_PROFILE` env var > `OWA_DEFAULT_PROFILE` in `profiles.conf` > lone profile on disk > `default` on fresh installs. If multiple profiles exist but none is marked default, the command errors out rather than guessing.

Legacy single-config installs auto-migrate on first run: `~/.config/owa-piggy/{config,cache.json,edge-profile}` move into `profiles/default/` atomically and a `profiles.conf` is written that marks `default` as the active profile.

There is a single shared launchd agent (`com.damsleth.owa-piggy.scheduled`) that reseeds the `OWA_SCHEDULED` set, so macOS's Login Items & Extensions shows one row regardless of profile count. Add profiles to the schedule with `owa-piggy profiles schedule <alias>` (or the `profiles` TUI). Installing the agent boots out any older per-profile plists (`com.damsleth.owa-piggy.<alias>`) and the pre-profile single plist.

---

## Caveats

- **Seed from Microsoft Edge.** Edge integrates with Microsoft's native SSO broker and stores a real FOCI refresh token (`1.AQ...`) in MSAL's cache `.secret` field. Plain Chromium browsers (Vivaldi, Brave, Chrome) fall back to a lighter flow that stores a session-bound opaque token at `.data` which AAD rejects as malformed (`AADSTS9002313`). That's also why those browsers log you out of OWA more often - the session token has a shorter fuse.
- Requires an account with OWA access (Microsoft 365 / Exchange Online)
- Uses a Microsoft first-party client ID - fine for personal tooling, not for production services or anything you'd ship to other users
- Refresh tokens are bound to your session; admin revocation or a password change will invalidate them
- `owa-piggy reseed` is macOS + Edge specific (uses `--user-data-dir` profile isolation and Chrome DevTools Protocol). The manual `setup` flow works everywhere.

## Disclaimer

```
This is a personal CLI tool for people who understand OAuth tokens and their risks.
If you don't know why storing a refresh token on disk might be a bad idea you should not use this.
```
