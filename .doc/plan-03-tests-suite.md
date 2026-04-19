# Plan 03 - `tests/` suite: scope resolution, config parsing, token-shape validation

## Relationship to Plan 01

Plan 01 establishes the module split and seeds the three
pure-function test files. This plan is the *full* test suite around
those same concerns plus token-shape validation. If Plan 01 lands
first, this plan extends those files; if this plan lands first,
it targets the flat `owa_piggy.py` via `importlib` and gets
refactored into the package imports once Plan 01 lands. Both orderings
work; the test cases are the same.

## What "token-shape validation" means

Not cryptographic validation - `owa-piggy` never verifies signatures.
"Shape" means: three dot-separated base64url segments, the middle
segment decodes to JSON, that JSON has an `exp` claim and a couple of
the expected MSAL fields (`aud`, `iss`, `tid`). The current
`decode_jwt` / `token_minutes_remaining` code assumes all of this.
The tests lock that assumption in.

## Layout

```
tests/
  __init__.py
  conftest.py                # shared fixtures: tmp config dir, synthetic JWT builder
  fixtures/
    config_valid.ini         # realistic config file
    config_malformed.ini     # trailing whitespace, CRLF, comments, values with =
  test_scopes.py
  test_config.py
  test_jwt.py
  test_cli_smoke.py          # optional; argparse wiring only, no network
```

## Fixtures (`conftest.py`)

- `tmp_config_home(monkeypatch, tmp_path)` - redirects
  `~/.config/owa-piggy/` to `tmp_path` by patching `pathlib.Path.home`
  or the resolved `CONFIG_PATH` constant.
- `make_jwt(payload: dict, *, header: dict | None = None) -> str` -
  builds `base64url(header) + "." + base64url(payload) + ".sig"`.
  Pads correctly; the code under test must accept unpadded input.
- `frozen_time(monkeypatch)` - freezes `time.time()` to a known value
  so `token_minutes_remaining` is deterministic.

## `test_scopes.py`

Cases:
- Default scope (no CLI flags, no `OWA_DEFAULT_AUDIENCE`) resolves to
  Graph, matching commit dc7662e.
- `OWA_DEFAULT_AUDIENCE=teams` flips the default; explicit `--graph`
  still overrides env.
- Each preset flag (`--graph`, `--teams`, `--sharepoint`,
  `--onedrive`) returns the scope string the FOCI client ID expects.
- `--scope "X Y Z"` explicit override beats preset flags.
- Unknown `OWA_DEFAULT_AUDIENCE` value exits with a clear message
  (not a `KeyError` traceback).
- `--list-scopes` output contains every entry in `SCOPE_MAP` exactly
  once.

## `test_config.py`

Cases for `parse_kv_stream`:
- Strips leading/trailing whitespace on both key and value.
- Ignores blank lines and `#` comments (including indented comments).
- Accepts values containing `=` (split on first `=` only).
- Handles CRLF input.
- Quoted values: if quotes are currently stripped, test that; if
  not, test that they are preserved verbatim. Pick one and lock it.

Cases for `load_config` / `save_config`:
- Save then load round-trips a dict without loss.
- Saved file has mode `0600`.
- `load_config` with no file returns `{}` (not an exception).
- Env vars override file values:
  - `OWA_REFRESH_TOKEN`
  - `OWA_TENANT_ID`
  - `OWA_DEFAULT_AUDIENCE`
- **Regression for commit c07a9ec:** when refresh token comes from
  env, the returned config object must NOT be flagged as persistable
  (`persist=False` / whatever the internal flag is). Trying to
  `save_config` on an env-derived config either no-ops or refuses.
- Config path expansion handles `~` and respects `XDG_CONFIG_HOME`
  if the current code uses it (check first).

## `test_jwt.py`

Cases:
- Round-trip: `make_jwt({"exp": now+3600, "aud": "..."})` decoded by
  `decode_jwt` returns the original payload.
- Unpadded base64url input decodes correctly.
- `token_minutes_remaining`:
  - `exp = now + 3600` returns `60` (with frozen time).
  - `exp = now - 60` returns a non-positive integer.
  - Missing `exp` surfaces a clean error (not a KeyError traceback).
- Malformed inputs:
  - Not three segments -> clear error.
  - Middle segment is not valid base64 -> clear error.
  - Middle segment is base64 but not JSON -> clear error.
  - Empty string -> clear error.
- Header decode: `decode_jwt` returns both header and payload; header
  has the expected `alg`/`typ`/`kid` when present.

## `test_cli_smoke.py` (optional, high value)

Only touches argparse and dispatch; no HTTP.

- `owa-piggy --help` exits 0, output contains every documented flag
  from the docstring in `owa_piggy.py`.
- `owa-piggy --list-scopes` exits 0.
- `owa-piggy --decode` with a valid config (fixture) exits 0 and
  prints valid JSON header+payload. Monkeypatch `exchange_token` to
  return a synthetic JWT so no network call happens.
- `owa-piggy` with no config and no env vars exits non-zero with a
  message pointing at `--setup`.

## What the tests must NOT do

- No network. Ever. `exchange_token` is monkeypatched or not called.
- No real refresh tokens in fixtures. Use obvious fakes
  (`"fake-rt-for-tests"`), never anything that looks like a real JWT
  from the user's tenant.
- No writes outside `tmp_path`.
- No dependency on the user's actual `~/.config/owa-piggy/`.

## Running

- `pip install -e '.[test]'`
- `pytest` (plain). Add `pytest -q` to the default CI command in
  Plan 04.
- Target: sub-second local run. These are pure-function tests.

## Acceptance

- All tests pass on a fresh checkout with `pip install -e '.[test]'`.
- Coverage (informational, not gated): scopes.py and jwt.py near
  100%, config.py above 90% (the interactive paths are not covered
  here).
- The three regression anchors are explicit and named:
  - `test_env_refresh_token_does_not_persist` (commit c07a9ec)
  - `test_default_audience_is_graph` (commit dc7662e)
  - `test_token_minutes_remaining_handles_past_exp`
