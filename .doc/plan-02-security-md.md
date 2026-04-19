# Plan 02 - Write a bluntly honest `SECURITY.md`

## Goal

Replace the empty `SECURITY.md` with a document that is honest about
what `owa-piggy` is, why it works, and why nobody should deploy it
for users who are not themselves.

## Tone

Not marketing. Not a disclaimer dump. A short technical note that an
engineer skimming the repo can read in under two minutes and walk
away with an accurate mental model of the risks.

Three things a reader must understand by the end:

1. This tool does not register an Azure AD app. It reuses a
   first-party SPA client ID (One Outlook Web) to call
   `/oauth2/v2.0/token` with a refresh token scraped from the user's
   own browser. The server cannot tell the difference between
   owa-piggy and a logged-in OWA tab.
2. Microsoft can break this on any Tuesday - and has every right to.
   There is no contract, no supported surface, no SLA. A client ID
   allowlist change, a header validation tweak, a FOCI policy update,
   a conditional access tightening, or an MSAL protocol bump can all
   kill it silently. Plan for that.
3. It is a "just for me" tool. Do not package it for other users,
   do not run it as a service account, do not hand it to a customer.
   The threat model assumes the refresh token lives on the same
   machine as the user who signed in.

## Document outline

```
# Security model for owa-piggy

## What this actually is
One paragraph. "Piggybacks on OWA's SPA client. Reads your own
refresh token out of your own browser's MSAL cache. Exchanges it at
login.microsoftonline.com. No app registration involved."

## Why this works today
- FOCI (Family of Client IDs) lets refresh tokens mint tokens for
  sibling first-party clients (Graph, Teams, SharePoint, OneDrive).
- AAD accepts the request because the `Origin` header matches what
  OWA itself sends.
- The refresh token is the same one OWA uses; rotation is handled by
  AAD on every call.

## Why this will break
- Microsoft can restrict `client_id=<OWA SPA>` to requests from
  Microsoft-owned origins at any time.
- Conditional Access / tenant policy can require device compliance
  signals that owa-piggy cannot produce.
- FOCI membership can change; sibling audiences can be removed.
- MSAL cache format or storage location can change.
- The 24h absolute hard-cap (AADSTS700084) is already a ceiling;
  anything that shortens it would make --reseed the steady-state
  path, not the exception.

When it breaks, expect AADSTS7000215, AADSTS700082, AADSTS700084,
AADSTS50126, or a blanket 400 with no detail. Do not assume the
tool is malfunctioning; assume Microsoft changed something.

## Threat model

In scope:
- Local disclosure of the refresh token (file at
  `~/.config/owa-piggy/config`, mode 0600, but still plaintext).
- Shoulder-surfing / screenshots of `--decode` / `--status`.
- A process running as the same user reading `~/.config/owa-piggy/`.

Out of scope:
- Multi-tenant deployment. There is none.
- Service accounts. Do not use this for one.
- Sharing tokens across hosts. The tool assumes one user, one laptop.
- Compromised browser profile. If someone owns your Edge profile,
  they already own your session - owa-piggy adds no new attack
  surface there.

## What owa-piggy will never do
- Register an application in anyone's tenant.
- Ask for admin consent.
- Issue tokens for anyone other than the user running it.
- Automate credential theft from other users on the same machine.
- Bypass Conditional Access. If your tenant blocks the sign-in,
  --reseed will fail the same way Edge would.

## Don't deploy this for other people
If you are thinking "I could wrap this in a service for my team" -
don't. The refresh token is a user credential. Sharing it across
users is credential sharing. Packaging it as a CLI for a teammate
to install is fine. Packaging it as a daemon that logs in on behalf
of N people is not.

## Reporting issues
This repo has one user. If you find a real security problem, open
a GitHub issue or email the address in the commit log. There is no
embargoed disclosure process because there is no deployed service.

## One-line summary
owa-piggy is a personal productivity hack that abuses a first-party
client. Treat it like your own shell alias, not like software.
```

## Acceptance

- `SECURITY.md` is populated with the above content (wording can be
  tightened; the three core messages - what it is, why it breaks,
  who should not use it - must all be present).
- README gets one new line under the top-of-file warning box:
  `See SECURITY.md for the full threat model and known failure modes.`
- No emoji, no badges, no marketing language.
- Fits on one screen of a default-width terminal when piped to `less`
  (soft target: under ~150 lines).

## Non-goals

- No CVE reporting policy. This is not a platform.
- No SLO for "we will keep this working". There is no we.
- No recommendation to use a "more secure alternative" - if there
  were one, this repo would not exist.
