# Plan 01 - Modularize `owa_piggy.py` and add pytest coverage

## Goal

Break the single 985-line `owa_piggy.py` into focused modules and add
`pytest` coverage for the pure functions (JWT parse, config read/write,
scope resolution). The CLI entry point stays a single command
(`owa-piggy`); only the internal layout changes.

## Why now

- The file is large enough that logical units (JWT decode, config I/O,
  scope map, token exchange, reseed, status/debug output) are hard to
  find and hard to test in isolation.
- Plans 03 and 04 (tests + CI) both need importable pure functions.
  Splitting is the prerequisite.

## Non-goals

- No behavior change. No CLI flag rename. No config format change.
- Do not refactor the HTTP call path beyond moving it into its own file.
- Do not introduce a dependency (stdlib only, matching the current design).

## Target layout

```
owa_piggy/
  __init__.py           # re-exports `main` so `owa-piggy = "owa_piggy:main"` still works
  __main__.py           # `python -m owa_piggy`
  cli.py                # arg parsing + dispatch (was main/print_help)
  config.py             # load_config, save_config, parse_kv_stream, iso_utc_now
  scopes.py             # FOCI_CLIENTS, SCOPE_MAP, resolve_scope, list_scopes
  jwt.py                # decode_jwt_segment, decode_jwt, token_minutes_remaining
  oauth.py              # exchange_token (the one HTTP call)
  reseed.py             # find_reseed_script, do_reseed
  status.py             # do_status, do_debug
  setup.py              # interactive_setup, read_input
```

`pyproject.toml` switches from `py-modules = ["owa_piggy"]` to a
package layout (`[tool.setuptools.packages.find]` or explicit
`packages = ["owa_piggy"]`). The `[project.scripts]` entry
`owa-piggy = "owa_piggy:main"` keeps working because
`owa_piggy/__init__.py` re-exports `main` from `cli`.

## Migration steps

1. Create `owa_piggy/` directory. Move `owa_piggy.py` to
   `owa_piggy/_monolith.py` as a safety net on disk (delete at end of
   the PR; it is useful during the split to diff against).
2. Extract `scopes.py` first - it is pure data plus `resolve_scope`.
   Has no imports from the rest of the module.
3. Extract `jwt.py` - stdlib only (`base64`, `json`, `time`).
4. Extract `config.py` - stdlib only (`os`, `pathlib`, `datetime`).
5. Extract `oauth.py` - the `exchange_token` function. Keep the
   `User-Agent`/`Origin` headers exactly as they are today.
6. Extract `setup.py`, `reseed.py`, `status.py` in any order; they
   import from the modules above.
7. Create `cli.py` with `main()` and `print_help()`. Import the
   handlers from the other modules.
8. `owa_piggy/__init__.py`: `from .cli import main`.
9. Update `pyproject.toml` to package layout. Rebuild, `pipx install -e .`,
   smoke test: `owa-piggy --help`, `owa-piggy --list-scopes`,
   `owa-piggy --decode` against a real token.
10. Delete `_monolith.py`.

## Tests (the pytest part)

New `tests/` directory at repo root. `pytest` is a dev-only
dependency; add `[project.optional-dependencies] test = ["pytest"]`
to `pyproject.toml` so `pip install -e '.[test]'` works. Do not make
`pytest` a runtime dependency.

Tests to add in this PR (the three "pure function" targets called out
in the task):

- `tests/test_scopes.py`
  - `resolve_scope` default (no flags) returns the Graph audience.
  - `--graph`, `--teams`, `--sharepoint`, `--onedrive` each return the
    expected scope string.
  - `--scope "foo bar"` explicit override wins over preset flags.
  - Unknown preset name raises / exits with the same error the CLI
    currently prints.
- `tests/test_config.py`
  - `parse_kv_stream` handles `KEY=value`, blank lines, `#` comments,
    values containing `=`, trailing whitespace, CRLF.
  - `load_config` reads a tmp_path file and returns the expected dict.
  - `save_config` round-trips through `load_config` without loss and
    sets mode `0600` on the file.
  - Env vars (`OWA_REFRESH_TOKEN`, `OWA_TENANT_ID`, `OWA_DEFAULT_AUDIENCE`)
    override file values.
  - Env-provided refresh token does NOT flip `persist=True`
    (regression test for commit c07a9ec).
- `tests/test_jwt.py`
  - `decode_jwt_segment` accepts unpadded base64url.
  - `decode_jwt` on a synthetic header.payload.signature returns the
    expected dict.
  - `token_minutes_remaining`: tokens with `exp` in the past return
    a non-positive number; future `exp` returns the right minute count.
  - Malformed JWT (not three segments, not base64) surfaces a clean
    error, not a traceback.

Use `pytest tmp_path` for config tests. No network. No real tokens in
the repo - build synthetic JWTs in the test file (`base64url(json)` +
`.sig`).

## Acceptance

- `pytest` passes locally with 100% of the three target modules'
  branches hit.
- `owa-piggy --help`, `--list-scopes`, `--decode`, `--status`,
  `--remaining`, `--graph`, `--teams` behave identically to `main`
  before the split (manual smoke test checklist in the PR).
- `pipx install -e .` installs and runs the same as before.
- Build artifacts (`build/`, `*.egg-info/`) are regenerated cleanly;
  no stale references to the flat `owa_piggy.py`.

## Risk / rollback

Low. The monolith stays on disk (under a different name) through the
PR, and the package layout change is the only thing that could surprise
a packaged install. Roll back by restoring `owa_piggy.py` at the root
and reverting `pyproject.toml`.
