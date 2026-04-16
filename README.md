# owa-piggy

Get a MS JWT without an app registration, asking a tenant admin or managing client secrets.

```sh
brew install --HEAD damsleth/tap/owa-piggy
owa-piggy --setup
```

Then just:

```sh
curl -H "Authorization: Bearer $(owa-piggy --graph)" https://graph.microsoft.com/v1.0/me
```

---

## Examples

```sh
owa-piggy                         # access token to stdout
owa-piggy --graph                 # Graph API token
owa-piggy --remaining             # minutes left on current token
owa-piggy --json | jq .scope      # inspect granted scopes
```

Pipe-friendly - raw token goes to stdout, everything else to stderr:

```sh
# Fetch calendar events
curl -s -H "Authorization: Bearer $(owa-piggy)" \
  "https://outlook.office.com/api/v2.0/me/events" | jq .

# Use in scripts
TOKEN=$(owa-piggy --graph)
az rest --headers "Authorization=Bearer $TOKEN" --url "https://graph.microsoft.com/v1.0/me"
```

---

## How?

OWA (One Outlook Web) is registered in Azure AD as a public SPA client with ID `9199bf20-a13f-4107-85dc-02114787ef48`. Public clients require no client secret. SPA refresh tokens live in your browser's `localStorage` and can be exchanged at Microsoft's standard OAuth2 token endpoint - the only requirement is that the request includes the `Origin` header AAD expects for SPA clients.

The token comes back with a broad set of delegated scopes: `Calendars.ReadWrite`, `Mail.ReadWrite`, `Files.ReadWrite`, and more. OWA is also a FOCI (Family of Client IDs) member, so the same refresh token works against `outlook.office.com`, `graph.microsoft.com`, and other Microsoft first-party APIs.

| Token | Lifetime |
|---|---|
| Access token | ~90 minutes |
| Refresh token | 24h sliding window, rotates on each use |

The rotated refresh token is saved automatically to `~/.config/owa-piggy/config` after every exchange. Use `owa-piggy` at least once a day and the token never expires. Set up an hourly cron to keep it alive without thinking about it:

```sh
./setup-cron.sh
```

---

## Security model

This tool deliberately operates within the boundaries of what Microsoft allows for public SPA clients:

- **No credentials stored in Azure** - there is no app registration to compromise
- **Delegated permissions only** - the token acts as you, with your existing access, nothing more
- **Standard OAuth2 token exchange** - no browser automation, no cookie theft, no undocumented APIs
- **Your session, your token** - the refresh token is the same one OWA already stores in your browser; this tool just makes it usable from the terminal

The token is scoped to your user identity. A password change or admin revocation invalidates it immediately - the same as it would in the browser.

Config is stored at `~/.config/owa-piggy/config`:

```
OWA_REFRESH_TOKEN="1.AQ..."
OWA_TENANT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Environment variables (`OWA_REFRESH_TOKEN`, `OWA_TENANT_ID`) take precedence over the config file. `OWA_CLIENT_ID` can override the default client ID if needed.

---

## Caveats

- Requires an account with OWA access (Microsoft 365 / Exchange Online)
- Uses a Microsoft first-party client ID - fine for personal tooling, not for production services or anything you'd ship to other users
- Refresh tokens are bound to your session; admin revocation or a password change will invalidate them
- If the token lapses (no use for 24h), re-run `owa-piggy --setup` to seed a fresh one from the browser

## Disclaimer

```
This is a personal CLI tool for people who understand OAuth tokens and their risks.  
If you don't know why storing a refresh token on disk might be a bad idea you should not use this.
```
