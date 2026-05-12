# owa-piggy

[![PyPI](https://img.shields.io/pypi/v/owa-piggy.svg)](https://pypi.org/project/owa-piggy/)
[![GitHub release](https://img.shields.io/github/v/release/damsleth/owa-piggy.svg)](https://github.com/damsleth/owa-piggy/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![ci](https://github.com/damsleth/owa-piggy/actions/workflows/ci.yml/badge.svg)

Turn your existing Outlook Web session into a reusable API token from the
terminal. No app registration, no tenant admin ask, no client secrets.

`owa-piggy` is the auth broker. The companion suite
[`owa-tools`](https://github.com/damsleth/owa-tools) ships eight binaries
(`owa`, `owa-cal`, `owa-mail`, `owa-graph`, `owa-doctor`, `owa-people`,
`owa-sched`, `owa-drive`) that borrow tokens from `owa-piggy` - separate
package, separate token store, never imported.

## Suite

`owa-piggy` is the M365 auth broker in the
**[mnem](https://github.com/damsleth/mnem)** memory suite, alongside
YAAMS (Tier 1 raw), cognitive-ledger (Tier 2 curated), and owa-tools
(M365 read/write). The suite gives you one install (`brew install
damsleth/tap/mnem`), one verb surface (`mnem auth ...`), and one
CLI contract (output classes, exit codes - see
[mnem/CONVENTIONS.md](https://github.com/damsleth/mnem/blob/main/CONVENTIONS.md)).
`owa-piggy` continues to work standalone and remains the only thing
that touches your refresh tokens.

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
| `profiles`               | list profiles (TTY: interactive picker); `--json` emits aliases and config presence |
| `profiles set-default A` | make `A` the default profile                                          |
| `profiles delete A`      | remove profile `A`'s config + Edge sidecar dir (`--force` to override) |
| `install-owa-tools`      | shorthand for `brew install damsleth/tap/owa-tools` (the companion suite) |
| `version`                | print version information; `--json` emits `{"tool": ..., "version": ...}` |

Global options: `--profile <alias>`, `--audience <name>`, `--scope <explicit>`, `--version`, `--help`. Per-command help: `owa-piggy <command> --help`.

## Examples

```sh
owa-piggy                              # Graph token (default audience)
owa-piggy --audience outlook           # Outlook REST audience
owa-piggy --audience teams             # Teams audience
owa-piggy remaining                    # minutes left on current token
owa-piggy token --json | jq .scope     # inspect granted scopes
owa-piggy status                       # compact ISO8601 health summary
owa-piggy status --json                # machine-readable health, no token values
owa-piggy profiles --json              # machine-readable profile registry
owa-piggy debug                        # full setup diagnostics
owa-piggy --version                    # print version
owa-piggy version --json               # machine-readable version
```

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

---

## How?

OWA (One Outlook Web) is registered in Azure AD as a public SPA client with ID `9199bf20-a13f-4107-85dc-02114787ef48`. Public clients require no client secret. SPA refresh tokens live in your browser's `localStorage` and can be exchanged at Microsoft's standard OAuth2 token endpoint - the only requirement is that the request includes the `Origin` header AAD expects for SPA clients.

The token comes back with a broad set of delegated scopes: `Calendars.ReadWrite`, `Mail.ReadWrite`, `Files.ReadWrite`, and more. OWA is also a FOCI (Family of Client IDs) member, so the same refresh token works against `outlook.office.com`, `graph.microsoft.com`, and other Microsoft first-party APIs.

| Token         | Lifetime                                                           |
| ------------- | ------------------------------------------------------------------ |
| Access token  | ~60-90 min from issue                                              |
| Refresh token | 24h sliding window (rotates on use) AND 24h absolute hard-cap from original sign-in |

The sliding window renews on every exchange. The hard-cap does not - after 24h AAD returns `AADSTS700084` and the token is unrecoverable via rotation. The launchd agent handles the sliding window; `owa-piggy reseed` handles the hard-cap.

The rotated refresh token is saved automatically to `~/.config/owa-piggy/profiles/<alias>/config` after every exchange (only when the token originally came from the config file - env-only callers keep env-only semantics and get a rotation notice on stderr). Install a LaunchAgent per profile to keep each sliding window fresh without thinking about it:

```sh
./scripts/setup-refresh.sh --profile default     # one profile
./scripts/setup-refresh.sh --all                 # every configured profile
```

The agents run hourly via `launchd`'s `StartCalendarInterval` and, unlike cron, fire on wake for any hour that was missed while the Mac was asleep - so an overnight-closed laptop still rotates each profile's token before the 24h sliding window closes.

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
owa-piggy status
```
```
authtoken:    expires 2026-04-20T11:46:51Z
audience:     outlook (https://outlook.office.com)
scope(s):     Calendars.ReadWrite, Mail.ReadWrite, Files.ReadWrite, ... (74 scopes)
refreshtoken: expires 2026-04-21T09:30:00Z
```

Prints `no valid token` (exit 1) if setup is missing or the live probe fails. The refresh-token expiry is the 24h hard-cap, computed from `OWA_RT_ISSUED_AT` which is stamped on `setup` and `reseed` (setups from before this field landed will show `unknown` until the next reseed).

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
- `OWA_CLIENT_ID` - override the default OWA client ID
- `OWA_DEFAULT_AUDIENCE` - change the default audience (a short name from `owa-piggy audiences` like `outlook`, or a full https URL). Command-line `--audience` / `--scope` still wins.

---

## Multiple profiles

owa-piggy supports multiple independent tenants / identities via named profiles. Each profile gets its own config, access-token cache, Edge sidecar userdata dir, and launchd job, so a broken reseed on one profile does not knock out the others.

```sh
owa-piggy setup --profile work                # create a new profile
owa-piggy setup --profile personal            # ...and another
owa-piggy --profile work                      # raw token for 'work'
OWA_PROFILE=work owa-piggy                    # same, via env
owa-piggy profiles                            # list (TTY: interactive picker)
owa-piggy profiles set-default work           # change the default pointer
owa-piggy status --profile personal           # health check, per profile
owa-piggy reseed --profile work               # recover one profile after 24h
owa-piggy profiles delete personal            # remove a profile (config + Edge)
./scripts/setup-refresh.sh --all              # install a plist for each profile
```

Selection precedence when `--profile` is omitted: `OWA_PROFILE` env var > `OWA_DEFAULT_PROFILE` in `profiles.conf` > lone profile on disk > `default` on fresh installs. If multiple profiles exist but none is marked default, the command errors out rather than guessing.

Legacy single-config installs auto-migrate on first run: `~/.config/owa-piggy/{config,cache.json,edge-profile}` move into `profiles/default/` atomically and a `profiles.conf` is written that marks `default` as the active profile. The legacy launchd plist (`com.damsleth.owa-piggy`) keeps running until you re-install via `./scripts/setup-refresh.sh --all`, which replaces it with per-profile plists labelled `com.damsleth.owa-piggy.<alias>`.

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
