# owa-piggy

Turn your existing Outlook Web session into a reusable API token from the terminal.  
No app registration, asking a tenant admin or managing client secrets.

```sh
brew install --HEAD damsleth/tap/owa-piggy
owa-piggy --setup
# or, to avoid terminal paste-corruption on long tokens:
# copy the two lines the browser snippet prints, then
pbpaste | owa-piggy --save-config
```

Then

```sh
curl -H "Authorization: Bearer $(owa-piggy)" https://graph.microsoft.com/v1.0/me
```

Or, for the Outlook REST audience:

```sh
curl -s -H "Authorization: Bearer $(owa-piggy --outlook)" \
  "https://outlook.office.com/api/v2.0/me/messages?\$top=1" | jq -r '.value[0].Subject'
```

---

## Examples

```sh
owa-piggy                         # Graph token (default audience)
owa-piggy --outlook               # Outlook REST audience
owa-piggy --teams                 # Teams audience
owa-piggy --remaining             # minutes left on current token
owa-piggy --json | jq .scope      # inspect granted scopes
owa-piggy --status                # compact ISO8601 health summary
owa-piggy --debug                 # full setup diagnostics
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

Default audience is **Microsoft Graph**, which covers everything Outlook REST exposes plus OneDrive, Teams, SharePoint, directory, and more. Override persistently with `OWA_DEFAULT_AUDIENCE=<short-name-or-https-url>`, or per-call with `--outlook`/`--teams`/`--azure`/... or `--scope <explicit>`.

---

## How?

OWA (One Outlook Web) is registered in Azure AD as a public SPA client with ID `9199bf20-a13f-4107-85dc-02114787ef48`. Public clients require no client secret. SPA refresh tokens live in your browser's `localStorage` and can be exchanged at Microsoft's standard OAuth2 token endpoint - the only requirement is that the request includes the `Origin` header AAD expects for SPA clients.

The token comes back with a broad set of delegated scopes: `Calendars.ReadWrite`, `Mail.ReadWrite`, `Files.ReadWrite`, and more. OWA is also a FOCI (Family of Client IDs) member, so the same refresh token works against `outlook.office.com`, `graph.microsoft.com`, and other Microsoft first-party APIs.

| Token         | Lifetime                                                           |
| ------------- | ------------------------------------------------------------------ |
| Access token  | ~60-90 min from issue                                              |
| Refresh token | 24h sliding window (rotates on use) AND 24h absolute hard-cap from original sign-in |

The sliding window renews on every exchange. The hard-cap does not - after 24h AAD returns `AADSTS700084` and the token is unrecoverable via rotation. The launchd agent handles the sliding window; `--reseed` handles the hard-cap.

The rotated refresh token is saved automatically to `~/.config/owa-piggy/config` after every exchange (only when the token originally came from the config file - env-only callers keep env-only semantics and get a rotation notice on stderr). Install a LaunchAgent to keep the sliding window fresh without thinking about it:

```sh
./scripts/setup-refresh.sh
```

The agent runs hourly via `launchd`'s `StartCalendarInterval` and, unlike cron, fires on wake for any hour that was missed while the Mac was asleep - so an overnight-closed laptop still rotates the token before the 24h sliding window closes.

---

## Automated reseed (24h hard-cap recovery)

Because hourly rotation only keeps the sliding window alive, you still hit `AADSTS700084` after 24h of continuous use. `--reseed` is the automated recovery path - it drives a sidecar Edge profile via the Chrome DevTools Protocol, extracts a fresh FOCI refresh token from MSAL's localStorage, and pipes it into `--save-config`.

One-time setup of the sidecar profile:

```sh
mkdir -p ~/.config/owa-piggy/edge-profile
/Applications/Microsoft\ Edge.app/Contents/MacOS/Microsoft\ Edge \
  --user-data-dir="$HOME/.config/owa-piggy/edge-profile" \
  https://outlook.cloud.microsoft
# sign in, then close Edge
```

Thereafter:

```sh
owa-piggy --reseed
```

The scraper detects stale caches (ID token JWT `iat` > 23h old), forces a Page.reload if MSAL gets wedged, and if session cookies have also expired it reopens Edge visibly so you can sign in interactively and then scrapes again automatically. When things work the whole thing is silent and takes a second or two.

The `AADSTS700084` error message from the normal flow also prints `hint: run owa-piggy --reseed` so you don't need to remember the recipe.

---

## Diagnostics

```sh
owa-piggy --status
```
```
authtoken:    expires 2026-04-20T11:46:51Z
audience:     outlook (https://outlook.office.com)
scope(s):     Calendars.ReadWrite, Mail.ReadWrite, Files.ReadWrite, ... (74 scopes)
refreshtoken: expires 2026-04-21T09:30:00Z
```

Prints `no valid token` (exit 1) if setup is missing or the live probe fails. The refresh-token expiry is the 24h hard-cap, computed from `OWA_RT_ISSUED_AT` which is stamped on `--setup` and `--reseed` (setups from before this field landed will show `unknown` until the next reseed).

```sh
owa-piggy --debug
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

Config is stored at `~/.config/owa-piggy/config`, mode 0600:

```
OWA_REFRESH_TOKEN="1.AQ..."
OWA_TENANT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
OWA_RT_ISSUED_AT="2026-04-19T10:15:00Z"
```

Writes are atomic (temp file + fsync + rename) so a crash mid-rotation cannot corrupt the only live token. Environment variables take precedence over the config file:

- `OWA_REFRESH_TOKEN`, `OWA_TENANT_ID` - override the corresponding config values (when `OWA_REFRESH_TOKEN` is env-supplied, rotated tokens are kept env-only and not written back to disk)
- `OWA_CLIENT_ID` - override the default OWA client ID
- `OWA_DEFAULT_AUDIENCE` - change the default audience (a short name from `--list-scopes` like `outlook`, or a full https URL). Command-line `--<name>` / `--scope` still wins.

---

## Caveats

- **Seed from Microsoft Edge.** Edge integrates with Microsoft's native SSO broker and stores a real FOCI refresh token (`1.AQ...`) in MSAL's cache `.secret` field. Plain Chromium browsers (Vivaldi, Brave, Chrome) fall back to a lighter flow that stores a session-bound opaque token at `.data` which AAD rejects as malformed (`AADSTS9002313`). That's also why those browsers log you out of OWA more often - the session token has a shorter fuse.
- Requires an account with OWA access (Microsoft 365 / Exchange Online)
- Uses a Microsoft first-party client ID - fine for personal tooling, not for production services or anything you'd ship to other users
- Refresh tokens are bound to your session; admin revocation or a password change will invalidate them
- `--reseed` is macOS + Edge specific (uses `--user-data-dir` profile isolation and Chrome DevTools Protocol). The manual `--setup` flow works everywhere.

## Disclaimer

```
This is a personal CLI tool for people who understand OAuth tokens and their risks.
If you don't know why storing a refresh token on disk might be a bad idea you should not use this.
```
