# Security model for owa-piggy

## TL;DR

`owa-piggy` is a personal productivity hack that abuses a first-party
client. Microsoft can kill it any Tuesday.  
Don't deploy it for
users who aren't you.

## What this actually is

`owa-piggy` piggybacks on Outlook on the Web's first-party SPA client
(client ID `9199bf20-a13f-4107-85dc-02114787ef48`) to exchange a
refresh token - the same one the user's own Edge profile already
holds for OWA - at `login.microsoftonline.com`. It reads that
refresh token out of the user's MSAL cache, calls AAD with the
`Origin: https://outlook.cloud.microsoft` header that an OWA tab
would send, and prints back the access token. No app registration,
no client secret, no server component.

## Why this works today

- **FOCI.** The OWA client ID is part of Microsoft's Family of
  Client IDs. A refresh token minted for any FOCI member can mint
  access tokens for siblings: Graph, Outlook, Teams, SharePoint,
  OneDrive, Azure Management, Key Vault, etc.
- **Origin check.** AAD's SPA flow validates the `Origin` header
  against the client ID's allowed origins. OWA allows
  `https://outlook.cloud.microsoft`, so the request is accepted.
- **Rotation is normal.** The refresh token rotates on every call.
  That is OAuth2 behavior, not a vulnerability the tool exploits.

## Why this will break

All of these are normal, expected, and unannounced:

- Microsoft restricts the OWA SPA client to Microsoft-owned origins.
  The `Origin` check fails and the tool returns `AADSTS9002327` or
  similar.
- Conditional Access / tenant policy starts requiring device
  compliance, Intune, or MFA signals that `owa-piggy` cannot
  produce. Expect `AADSTS53000`, `AADSTS50005`, or a blanket 400.
- FOCI membership changes; a sibling audience is removed or fenced.
- The 24h absolute hard-cap (`AADSTS700084`) already bounds usable
  token lifetime to one day without an Edge session. If that cap
  shortens, `owa-piggy reseed` becomes the steady state instead of the
  exception.
- MSAL's cache format or storage location changes. The browser
  snippet in the README stops finding the token.

When it breaks, do not assume `owa-piggy` is buggy. Assume Microsoft
changed something. Read the AADSTS code and plan accordingly.

## Threat model

**In scope:**  The tool assumes a single user, logged in to OWA in
Edge on their own machine, running the CLI under their own account.

- The refresh token is stored at `~/.config/owa-piggy/config`,
  mode `0600`. Any process running as that user can
  read it.
- Access tokens are cached at `~/.config/owa-piggy/cache.json`,
  same mode, same rules - keyed by the scope string, valid until
  each token's `exp`. Delete the file to force a fresh mint.
- The `decode`, `status`, and `debug` subcommands print claims from the
  access token to stdout/stderr. Treat that output like credentials.
- Atomic writes (temp file + fsync + rename) protect the on-disk
  token from partial writes that would require a browser reseed.

**Out of scope:**

- Multi-tenant deployment. There is none.
- Service accounts, daemons, or CI secret stores. Do not use this
  tool to mint tokens for non-human principals.
- Sharing tokens across hosts or users. The token is a user
  credential; sharing it is credential sharing.
- A compromised Edge profile. If someone else can read your Edge
  storage, they already have your session - `owa-piggy` adds no
  new attack surface on top of that.

## What `owa-piggy` does _not_ do

- Register an application in anyone's tenant
- Ask for admin consent
- Issue tokens for anyone other than the user running it
- Automate credential theft from other users on the same machine
- Bypass Conditional Access. If your tenant blocks the sign-in,
  `owa-piggy reseed` fails the same way Edge would
- Send telemetry, crash reports, or update checks. The only network
  call is `POST login.microsoftonline.com`

## Don't deploy this for other people

If you are thinking _"I could wrap this in a service for my team"_, 
don't. The refresh token is a user credential. Sharing it across
users is credential sharing. Packaging the CLI so a teammate can
install it on their own laptop, using their own OWA session is
fine. Packaging it as a daemon that logs in on behalf of N people
is not.

There is no support, no SLA, and no promise this keeps working past
today. Treat the tool like your own shell alias, not like software.

## Reporting issues

This repo has one user. If you find a real security problem that
affects that user (local privilege escalation via the config file,
token exfiltration through an error path, etc.), open a GitHub
issue or email the address in the commit log. There is no embargoed
disclosure process because there is no deployed service to embargo.