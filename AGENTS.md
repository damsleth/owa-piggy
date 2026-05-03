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
  cli.py             # arg parsing + dispatch (argparse subparsers: token, status, ...)
  scopes.py          # KNOWN_AUDIENCES, resolve_audience
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
.plans/               # design + implementation plans - read before big changes
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

Planning docs live in `.plans/`. If you are about to do something
non-trivial, check `.plans/plan-*.md` first - the plan may already
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
- **Test what matters.** Pure functions (`resolve_audience`,
  `parse_kv_stream`/`load_config`, `decode_jwt`,
  `token_minutes_remaining`, `cache.*`) plus CLI dispatch are the
  test targets. Interactive setup, network calls, and launchd
  plumbing are not.

## Verification before claiming done

- `python -m compileall -q owa_piggy` passes.
- `python -m owa_piggy --help` and `python -m owa_piggy audiences` run
  without traceback on a machine with no config.
- `pytest -q` is green.
- If you touched token logic: `owa-piggy decode` and `owa-piggy status`
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
  `main`. Do not rewrite published tags (see `.plans/plan-06`).

## Cutting a release (only when the user asks)

Releases are pushed out through a Homebrew tap at
`~/Code/homebrew-tap` (`damsleth/homebrew-tap` on GitHub). The
formula pins a specific tag tarball and sha256, so a version bump
here must be followed by a tap update or `brew upgrade` stays on
the old version.

When the user says "cut a release" / "new patch version" / "ship it":

1. Pick the bump. Patch (`0.3.0 -> 0.3.1`) for bug fixes, doc
   corrections, small UX polish. Minor (`0.3.1 -> 0.4.0`) for new
   flags, new behaviors, anything a user might notice. Never bump
   major without explicit instruction - this tool is 0.x by design.
2. Commit the feature work separately from the version bump. Recent
   history: one `Bump version to X.Y.Z` commit sitting on top of the
   feature commit. Keep that pattern so `git log` reads cleanly.
3. Update `pyproject.toml` `version = "X.Y.Z"`. No other file tracks
   the version today.
4. Push `main`, then create an **annotated** tag whose message is the
   release notes (a short prose summary + bullet list of changes
   since the previous tag - this is the canonical place for them,
   since there's no CHANGELOG.md):
   ```
   git tag -a vX.Y.Z -m "vX.Y.Z - <one-line headline>

   <optional prose paragraph>

   - bullet: user-visible change
   - bullet: breaking change (call out explicitly)
   - bullet: internal refactor worth noting
   "
   git push origin vX.Y.Z
   ```
   Render with `git show vX.Y.Z` or `git tag -n99 vX.Y.Z`. Lightweight
   tags (`git tag vX.Y.Z`) are what the old v0.2.2-v0.3.2 tags did
   and should not be used going forward. Never retag a version that's
   already public - Homebrew users cache the tarball by sha.
5. Fetch the GitHub-generated tarball and compute its sha256:
   `curl -sL https://github.com/damsleth/owa-piggy/archive/refs/tags/vX.Y.Z.tar.gz -o /tmp/owa-piggy-X.Y.Z.tar.gz && shasum -a 256 /tmp/owa-piggy-X.Y.Z.tar.gz`
6. Edit `~/Code/homebrew-tap/Formula/owa-piggy.rb` - bump the `url`
   tag and the `sha256`. Nothing else changes unless dependencies
   did.
7. Commit the tap with message `owa-piggy X.Y.Z` (matches the tap's
   existing convention) and push.
8. `brew upgrade owa-piggy owa-cal` on the dev machine to actually
   pull the new formula locally - the tap push only updates the
   metadata; nothing on disk changes until brew refetches. Skipping
   this leaves the dev machine on the previous version even though
   `git log` and PyPI both say the new one shipped.

   Note: the launchd reseed agent invokes `~/.local/bin/owa-piggy`,
   which on this machine is a *pipx editable* install pointing at
   the repo (see `pipx list`). Code changes here are live in
   launchd the moment they hit disk - you do not need to reinstall
   pipx after editing `scripts/scrape_edge.py` or any Python
   module. The brew copy is what end users get.
9. Publish the sdist + wheel to PyPI. Build with the venv's `build`
   module and upload with `uv publish`, which reads the API token
   from `UV_PUBLISH_TOKEN` (kept in `./.env` at the repo root - do
   NOT commit it; `.gitignore` already excludes it). Use `uv`
   specifically; the system `twine` is missing deps on this machine
   and falls over on `twine check`.
   ```
   rm -rf dist build
   /Users/damsleth/.local/pipx/venvs/owa-piggy/bin/python3 -m build
   /Users/damsleth/.local/pipx/venvs/owa-piggy/bin/python3 -m twine check dist/*
   set -a && . ./.env && set +a && uv publish dist/owa_piggy-X.Y.Z*
   ```
   PyPI's JSON index (`/pypi/owa-piggy/json`) lags by minutes after
   upload - if `uv publish` reports "File already exists" on a
   retry but `pypi.org/pypi/owa-piggy/X.Y.Z/json` returns 200, the
   upload actually succeeded and the index is just stale. Don't
   re-tag or re-build to "fix" it.

If any step fails midway (tag push rejected, sha mismatch, tap push
rejected, PyPI 4xx that isn't "File already exists"), stop and
surface the error - do not try to "fix" a published tag by
force-pushing, and never bump the patch version a second time to
work around an already-published file.

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
