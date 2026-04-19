# Plan 05 - Expand `.gitignore` and keep the worktree clean

## Current state

```
.DS_Store
.env
.doc/            # ← wrong; we *do* want .doc/ tracked (contains these plans)
.pycache_local/
__pycache__/
build/
owa_piggy.egg-info/
```

Current worktree status from `git status`:

```
?? .pycache_local/
?? build/
?? owa_piggy.egg-info/
```

Those three are already ignored. But `.doc/` is also ignored, which
means none of the plans written in this round would be tracked. Fix
that first.

## Goal

1. Un-ignore `.doc/` so planning docs land in git.
2. Expand `.gitignore` to the standard Python + build + editor + OS
   noise list so nothing sneaks in later.
3. Verify the worktree is clean (no stray committed artifacts) and
   nothing we want tracked is currently ignored.

## New `.gitignore`

```gitignore
# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class

# C extensions
*.so

# Distribution / packaging
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
*.egg
MANIFEST

# PyInstaller
*.manifest
*.spec

# Installer logs
pip-log.txt
pip-delete-this-directory.txt

# Unit test / coverage reports
htmlcov/
.tox/
.nox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
*.py,cover
.hypothesis/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.pyre/
.pytype/

# Virtual environments
.env
.venv
env/
venv/
ENV/

# Editors / IDEs
.idea/
.vscode/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Project-local
.pycache_local/
```

Note: `.doc/` is NOT in this list. `.env` stays ignored.

## Worktree cleanup

After updating `.gitignore`:

1. `git rm -r --cached build/ owa_piggy.egg-info/ .pycache_local/ 2>/dev/null || true`
   - These are already untracked per current `git status`, but run
     it anyway in case a future push tries to add them.
2. `git status` - should show the `.gitignore` change, the new
   `.doc/` contents, and nothing else surprising.
3. `git check-ignore -v README.md LICENSE pyproject.toml owa_piggy.py`
   should return no matches (confirms nothing we care about is
   ignored).
4. `git check-ignore -v build/anything .pycache_local/foo __pycache__/bar .DS_Store`
   should match (confirms the noise is still ignored).

## What NOT to add

- No global `*.log` / `*.tmp` wildcards. If a log file appears in
  the repo, that is a bug to fix, not silence.
- No `secrets.*` / `credentials.*` catch-all. `.env` covers the
  realistic case; anything else should be named explicitly or kept
  outside the worktree.
- No IDE-specific files beyond the basics (`.idea/`, `.vscode/`).
  If a contributor uses something exotic, they can add it to their
  global `~/.config/git/ignore` instead of polluting the repo.

## `.gitattributes` (optional, skip if uncertain)

Not in scope for this plan. If line-ending issues appear on
Windows-side editing (unlikely - this is a macOS tool), revisit.

## Acceptance

- `.doc/` is tracked; `ls .doc/` shows the plan files and they appear
  in `git status`.
- `git status` on a fresh checkout shows no untracked build
  artifacts.
- `python -m build` then `git status` still shows a clean worktree
  (the build outputs are ignored).
- `pytest` then `git status` still clean (`.pytest_cache/` ignored).
- `pip install -e .` then `git status` still clean (`.egg-info/`
  ignored).

## Rollback

Revert the `.gitignore` edit. None of this changes runtime behavior.
