# AGENTS.md

Instructions for AI coding agents working in this repo.

## What this is

`owa-piggy` is a single-file Python CLI (`owa_piggy.py`, ~1000 lines,
stdlib-only) that exchanges a refresh token scraped from the user's
own browser for a fresh Microsoft Graph/Outlook/Teams/etc. access
token. It abuses OWA's first-party SPA client ID. No app
registration, no server, no multi-user deployment. One user, one
laptop, macOS-first.

Read `SECURITY.md` before suggesting changes that touch the OAuth
flow, the client ID, the `Origin` header, or refresh token storage.
The threat model is "just for me" - do not harden it into a service.

## Ground rules

- **Stdlib only** at runtime. No `requests`, no `msal`, no deps.
  `pytest` is dev-only under `[project.optional-dependencies] test`.
- **Do not touch `CLIENT_ID`, `ORIGIN`, or the `User-Agent` in
  `exchange_token`** without a clear reason. Those values make AAD
  accept the request; changing them breaks the tool silently.
- **Never commit real refresh tokens, access tokens, tenant IDs, or
  `~/.config/owa-piggy/config` contents**, even in tests or fixtures.
  Use obvious fakes (`"fake-rt-for-tests"`).
- Match existing style: docstrings are prose-heavy and explain *why*.
  Keep that tone when editing.

## Layout

```
owa_piggy.py          # the whole CLI
scripts/
  reseed-from-edge.sh # headless Edge sidecar to beat the 24h hard-cap
  scrape_edge.py
  setup-refresh.sh    # launchd agent installer
pyproject.toml
.doc/                 # design + implementation plans - read before big changes
README.md
SECURITY.md
```

Planning docs live in `.doc/`. If you are about to do something
non-trivial, check `.doc/plan-*.md` first - the plan may already
exist with acceptance criteria.

## Working on this repo

- **Read before editing.** Don't change code you haven't read.
  Navigate with `Grep` on function names.
- **Preserve behavior** unless a commit explicitly changes it.
  Recent commits encode subtle decisions: default audience = graph
  (dc7662e), env-token does not flip persist (c07a9ec), packaged
  installs ship reseed scripts (a4b8284). Do not regress those.
- **Don't add abstractions.** A `class TokenClient` wrapping one
  `urlopen` call is noise. Flat functions are the norm.
- **Test what matters.** Pure functions (`resolve_scope`,
  `parse_kv_stream`/`load_config`, `decode_jwt`,
  `token_minutes_remaining`) are the test targets. Interactive
  setup, network calls, and launchd plumbing are not.

## Verification before claiming done

- `python -m py_compile owa_piggy.py` passes.
- `python owa_piggy.py --help` and `--list-scopes` run without
  traceback on a machine with no config.
- If you touched token logic: `owa-piggy --decode` and `--status`
  still produce sane output against a real configured profile. If
  you cannot run against a real token, say so explicitly rather than
  claiming it works.
- If you touched `scripts/*.sh`: `shellcheck` clean.

## Commits and PRs

- Short imperative commit messages (see `git log`). One line is
  usually enough; expand in the body only when the *why* isn't
  obvious from the diff.
- One logical change per commit.
- Do not push or open PRs without the user asking. Do not force-push
  `main`. Do not rewrite published tags (see `.doc/plan-06`).

## What NOT to do

- Don't register an Azure AD app to "clean this up". That is the
  whole point of the tool not existing.
- Don't add telemetry, crash reporting, update checks, or any network
  call beyond the one to `login.microsoftonline.com`.
- Don't package this for other users. It is a personal hack. If the
  user asks about multi-user deployment, point them at `SECURITY.md`.
- Don't silently widen scopes or change the default audience.
  `OWA_DEFAULT_AUDIENCE` is the documented override.
- Don't add emoji, badges, or marketing copy to docs.
