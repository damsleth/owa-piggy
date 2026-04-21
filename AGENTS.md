# AGENTS.md

Instructions for AI coding agents working in this repo.

## What this is

`owa-piggy` is a small stdlib-only Python CLI (package at `owa_piggy/`,
~1000 lines split across a handful of modules) that exchanges a refresh
token scraped from the user's own browser for a fresh Microsoft
Graph/Outlook/Teams/etc. access token. It abuses OWA's first-party
SPA client ID. No app registration, no server, no multi-user
deployment. One user, one laptop, macOS-first.

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
owa_piggy/
  __init__.py        # re-exports `main` so `owa-piggy = "owa_piggy:main"` resolves
  __main__.py        # `python -m owa_piggy`
  cli.py             # arg parsing + dispatch (--profile, --list-profiles, ...)
  scopes.py          # KNOWN_SCOPES, resolve_scope
  jwt.py             # decode_jwt_segment, decode_jwt, token_minutes_remaining
  config.py          # ROOT_DIR, CONFIG_PATH, profile path helpers,
                     # resolve_profile, profiles.conf I/O, load/save_config
  migration.py       # one-shot legacy single-config -> profiles/default/ rescue
  cache.py           # access-token cache keyed by (tenant, client, scope),
                     # scoped per-profile via CONFIG_PATH.parent
  oauth.py           # CLIENT_ID, ORIGIN, exchange_token (the one HTTP call)
  setup.py           # interactive_setup(alias), read_input (raw-tty paste safety)
  reseed.py          # find_reseed_script, do_reseed(alias) (shells out)
  status.py          # do_status(alias), do_debug(alias)
scripts/
  reseed-from-edge.sh  # headless Edge sidecar; reads OWA_PIGGY_EDGE_PROFILE_DIR
  scrape_edge.py
  setup-refresh.sh     # launchd agent installer, one plist per profile
  add-to-path.sh       # pipx-based installer shim
tests/                # pytest suite: pure functions + CLI smoke + profile suite
pyproject.toml
.doc/                 # design + implementation plans - read before big changes
README.md
SECURITY.md
```

On-disk layout at `~/.config/owa-piggy/`:

```
profiles.conf                     OWA_DEFAULT_PROFILE + OWA_PROFILES
profiles/
  <alias>/
    config                        per-profile KV (OWA_REFRESH_TOKEN, ...)
    cache.json                    access-token cache for this profile
    edge-profile/                 Edge sidecar userdata dir for this profile
    refresh.log                   per-profile launchd stderr
```

The single-file layout (`~/.config/owa-piggy/config`) is auto-migrated into
`profiles/default/` the first time any profile-aware code path runs; see
`owa_piggy/migration.py`.

Planning docs live in `.doc/`. If you are about to do something
non-trivial, check `.doc/plan-*.md` first - the plan may already
exist with acceptance criteria.

## Working on this repo

- **Read before editing.** Don't change code you haven't read.
  Navigate with `Grep` on function names.
- **Preserve behavior** unless a commit explicitly changes it.
  Recent commits encode subtle decisions: default audience = graph
  (dc7662e), env-token does not flip persist (c07a9ec), packaged
  installs ship reseed scripts (a4b8284), cache keyed by
  (tenant, client, scope) (QA fix #1). Do not regress those.
- **Don't add abstractions.** A `class TokenClient` wrapping one
  `urlopen` call is noise. Flat functions are the norm.
- **Test what matters.** Pure functions (`resolve_scope`,
  `parse_kv_stream`/`load_config`, `decode_jwt`,
  `token_minutes_remaining`, `cache.*`) plus CLI dispatch are the
  test targets. Interactive setup, network calls, and launchd
  plumbing are not.

## Verification before claiming done

- `python -m compileall -q owa_piggy` passes.
- `python -m owa_piggy --help` and `--list-scopes` run without
  traceback on a machine with no config.
- `pytest -q` is green.
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
