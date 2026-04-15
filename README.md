# owa-piggy

Get a Microsoft Outlook/Graph access token without registering an app in Azure AD.

Piggybacks on OWA's (One Outlook Web) existing first-party SPA client registration to exchange a refresh token for a fresh access token — no tenant admin, no app registration, no client secret.

```sh
token=$(owa-piggy)
```

## How it works

OWA is registered in Azure AD as a public SPA client (`9199bf20-a13f-4107-85dc-02114787ef48`). Public clients don't need a client secret. SPA refresh tokens are stored in your browser's `localStorage` and can be exchanged at Microsoft's standard OAuth2 token endpoint — as long as the request includes the `Origin` header that AAD requires for SPA clients.

The token comes back with a broad set of scopes including `Calendars.ReadWrite`, `Mail.ReadWrite`, `Files.ReadWrite`, and more. OWA is also a FOCI (Family of Client IDs) member, so the same token works against `outlook.office.com`, `graph.microsoft.com`, and other Microsoft first-party APIs.

## Setup

One-time. Takes about 2 minutes.

1. Open [outlook.cloud.microsoft](https://outlook.cloud.microsoft) in your browser
2. Open DevTools (F12) > Application > Local Storage > `https://outlook.cloud.microsoft`
3. Find the key ending in `-refreshtoken-9199bf20-a13f-4107-85dc-02114787ef48--`
4. Copy the `secret` field value (starts with `1.AQ...`)
5. Find the IdToken entry, copy the `realm` field (your tenant ID, a UUID)
6. Run:

```sh
owa-piggy --save-config
```

Or set environment variables directly:

```sh
export OWA_REFRESH_TOKEN="1.AQ..."
export OWA_TENANT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

## Install

```sh
./add-to-path.sh
```

Symlinks `owa-piggy` into `/usr/local/bin`.

## Usage

```sh
owa-piggy                    # access token to stdout
owa-piggy --remaining        # minutes left on current token
owa-piggy --json             # full token response as JSON
owa-piggy --json | jq .scope # inspect granted scopes
owa-piggy --scope 'https://graph.microsoft.com/.default'  # Graph token
owa-piggy --help             # full usage
```

Pipe-friendly — raw token goes to stdout, everything else to stderr:

```sh
curl -s -H "Authorization: Bearer $(owa-piggy)" \
  "https://outlook.office.com/api/v2.0/me/events" | jq .
```

## Token lifetime

| Token | Lifetime |
|---|---|
| Access token | ~90 minutes |
| Refresh token | 24h sliding window |

The refresh token rotates on every exchange and is saved automatically to `~/.config/owa-piggy/config`. Use `owa-piggy` at least once a day and the refresh token never expires. If it does lapse, re-run the setup steps above to seed a fresh one.

## Keeping the token alive automatically

Set up an hourly cron job that silently refreshes the token in the background:

```sh
./setup-cron.sh
```

This registers a cron entry that runs `owa-piggy` at the top of every hour, keeping the refresh token well within its 24h sliding window. Errors are logged to `~/.config/owa-piggy/cron.log`.

```sh
# verify
crontab -l | grep owa-piggy

# check logs
tail -f ~/.config/owa-piggy/cron.log

# remove
crontab -e   # delete the owa-piggy line
```

## Config file

Auto-created at `~/.config/owa-piggy/config` by `--save-config`:

```
OWA_REFRESH_TOKEN="1.AQ..."
OWA_TENANT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Environment variables take precedence over the file if both are set.
`OWA_CLIENT_ID` can be set to override the default client (rarely needed).

## Caveats

- Only works with accounts that have OWA access (Microsoft 365 / Exchange Online)
- Refresh tokens are tied to your user session — a password change or admin revocation invalidates them
- This uses a Microsoft first-party client ID, not your own registration. Use for personal tooling, not production services
