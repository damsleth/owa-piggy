# Plan 04 - CI on push

## Goal

Every push and every PR to `main` runs syntax checks and the pytest
suite. Nothing fancy. Matrix the Python versions the project claims
to support (`>=3.8` per `pyproject.toml`).

## Platform

GitHub Actions. Single workflow file, single job with a matrix.

## File

`.github/workflows/ci.yml`

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python: ["3.8", "3.11", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
          cache: pip
      - name: Install
        run: pip install -e '.[test]'
      - name: Syntax check
        run: python -m compileall -q owa_piggy
      - name: Tests
        run: pytest -q
```

Notes:
- `fail-fast: false` so a breakage on 3.8 does not hide a 3.13-only
  regression or vice versa.
- Matrix endpoints only: lowest supported (3.8), a middle LTS-ish
  (3.11), latest stable (3.13). Three runs, not five. If a
  mid-version breaks while endpoints pass, add it then.
- `compileall` is the "syntax check" the task asks for. It is
  zero-config and catches `SyntaxError`/`IndentationError`
  before pytest imports anything.
- No linter in this plan. Adding `ruff` is a separate, reversible
  change; keep the first CI PR small.

## Pre-Plan-01 variant

If this lands before the module split:

```yaml
      - name: Syntax check
        run: python -m py_compile owa_piggy.py
```

After Plan 01, switch to `python -m compileall -q owa_piggy`.

## Shell script sanity (optional, small)

Three shell scripts ship in `scripts/`. Add a quick shellcheck step
so a typo there does not slip past:

```yaml
      - name: Shellcheck
        run: shellcheck scripts/*.sh
```

`shellcheck` is pre-installed on `ubuntu-latest`. If it complains
about existing scripts, fix or `# shellcheck disable=` as needed
before enabling the step (do not disable shellcheck globally).

## Badge

Add one line to `README.md` under the title:

```
![ci](https://github.com/<user>/owa-piggy/actions/workflows/ci.yml/badge.svg)
```

Replace `<user>` with the actual GitHub owner. Badge goes above the
top-level "what is this" paragraph.

## What CI does NOT do in this plan

- No publish to PyPI. There is no PyPI release. Homebrew tap release
  is Plan 06 and is manual.
- No coverage gate. Coverage is informational.
- No deploy / no release automation.
- No secrets. This repo has nothing that needs them, and - per
  `SECURITY.md` in Plan 02 - it should stay that way.
- No macOS runner. The tool is macOS-first but the tests are pure
  Python. If we ever add tests that exercise `scripts/reseed-from-edge.sh`
  or Edge profile handling, add `macos-latest` to the matrix then.

## Acceptance

- Push to a branch: workflow runs, all matrix cells green.
- Open a PR: the same workflow runs on the PR.
- Introduce a `SyntaxError` on a branch: workflow goes red on the
  `compileall` step before pytest even starts.
- Break a test: workflow goes red on `pytest` with the failure
  visible in the log.
- README badge renders green on `main`.

## Rollback

Delete `.github/workflows/ci.yml`. No other files depend on it.
