# Plan 06 - Tag v0.1.0, publish to `damsleth/tap`, drop `--HEAD`

## Goal

Cut a real first release. Stop making tap users install from `HEAD`.

## Prerequisites (do not skip)

- Plans 02 (SECURITY.md), 04 (CI) landed and green on `main`.
- Plan 05 worktree is clean; `git status` on `main` is empty.
- `pyproject.toml` `version = "0.1.0"` (already set - verify before tagging).
- `README.md` install section points at the tap, not a direct
  `brew install --HEAD` curl.
- Manual smoke test on a fresh machine or a fresh pipx env:
  - `pipx install git+https://github.com/<user>/owa-piggy@v0.1.0`
    (after the tag exists)
  - `owa-piggy --help`, `--list-scopes`, `--status` on an already
    configured profile.
  - `owa-piggy --reseed` on a machine with the Edge sidecar profile
    set up.

## Step 1 - Tag the release

```
# On a clean main with CI green
git checkout main
git pull --ff-only
git tag -a v0.1.0 -m "v0.1.0 - first tagged release"
git push origin v0.1.0
```

Create a GitHub Release from the tag. Body should be terse:

```
## v0.1.0

First tagged release. Stable enough for personal use; see SECURITY.md
for what "stable" does and does not mean here.

### Highlights
- Default audience is Microsoft Graph (`OWA_DEFAULT_AUDIENCE` override).
- `--status` one-line ISO8601 health summary.
- `--debug` full setup diagnostics.
- `--reseed` handles the 24h AAD hard-cap via Edge sidecar profile.
- Config at `~/.config/owa-piggy/config`, 0600.

### Known limits
- Refresh token hard-caps at 24h; `--reseed` is required past that.
- Non-Edge Chromium browsers store a session-bound token; seed from
  Edge (README has the steps).
```

Attach no binaries. This is a pip/pipx/brew-source install.

## Step 2 - Build artifacts for the tap

Homebrew formula needs a source tarball URL and its sha256. Use the
GitHub-generated tag tarball:

```
URL=https://github.com/<user>/owa-piggy/archive/refs/tags/v0.1.0.tar.gz
curl -sL "$URL" | shasum -a 256
```

Record the sha256. Do not re-download later - GitHub-generated
tarballs are deterministic per tag, so the hash will not drift.

## Step 3 - Write / update the Homebrew formula in `damsleth/tap`

Repo: `github.com/damsleth/homebrew-tap`. Formula path:
`Formula/owa-piggy.rb`.

```ruby
class OwaPiggy < Formula
  include Language::Python::Virtualenv

  desc "Get an Outlook/Graph access token without registering an app in Azure AD"
  homepage "https://github.com/<user>/owa-piggy"
  url "https://github.com/<user>/owa-piggy/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "<sha from step 2>"
  license "MIT"
  head "https://github.com/<user>/owa-piggy.git", branch: "main"

  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "owa-piggy", shell_output("#{bin}/owa-piggy --help")
  end
end
```

Notes:
- `owa-piggy` has no runtime Python deps (stdlib-only). If that
  changes, add `resource` blocks. Regenerating them is a `poet`
  / `homebrew-pypi-poet` task; not needed today.
- `head "..."` stays so `brew install --HEAD damsleth/tap/owa-piggy`
  still works for contributors, but the default install is now the
  tagged release.
- `test do` keeps it minimal - `--help` exits 0 and contains the
  binary name. No network, no config writes.

## Step 4 - Publish to the tap

```
# In the tap repo
git checkout -b owa-piggy-0.1.0
# edit Formula/owa-piggy.rb
brew install --build-from-source ./Formula/owa-piggy.rb   # local sanity
brew test ./Formula/owa-piggy.rb
brew audit --strict --new ./Formula/owa-piggy.rb
git add Formula/owa-piggy.rb
git commit -m "owa-piggy 0.1.0 (new formula)"
git push origin owa-piggy-0.1.0
# Open PR in damsleth/homebrew-tap, merge after CI if the tap has any.
```

After merge, verify from a clean shell on a different machine or a
fresh `/opt/homebrew` prefix:

```
brew tap damsleth/tap
brew install owa-piggy          # no --HEAD
owa-piggy --help
```

## Step 5 - Drop `--HEAD` from install docs

In `README.md`, replace any `brew install --HEAD damsleth/tap/owa-piggy`
instructions with:

```
brew tap damsleth/tap
brew install owa-piggy
```

Keep a single line at the bottom of the install section for
contributors who want bleeding edge:

```
Bleeding edge (main): brew install --HEAD damsleth/tap/owa-piggy
```

## Step 6 - Announce / handoff (optional)

- Pin a GitHub issue or Discussion linking the release and SECURITY.md.
- If `brkh` / personal notes reference the install command, update
  those too.

## Rollback

- Formula breakage: `git revert` the tap commit. Users fall back to
  `--HEAD` until fixed.
- Tag mistake: do not force-push the tag; cut a `v0.1.1` with the
  fix. Tags are immutable in consumer minds even if git allows
  rewriting them.
- Bad release on pipx/pip side: `pipx install ... @v0.1.1` once the
  patch tag exists. Do not delete v0.1.0.

## Acceptance

- `git tag -l v0.1.0` exists on `main`, pushed to origin.
- GitHub Release page exists for v0.1.0.
- `damsleth/homebrew-tap` `Formula/owa-piggy.rb` points at v0.1.0
  tarball + sha256.
- `brew install damsleth/tap/owa-piggy` (no `--HEAD`) works on a
  fresh machine.
- `README.md` install section leads with the tap install, not
  `--HEAD`.
- `brew audit --strict --new` clean.

## Version bump plan for next releases

- Patch (`v0.1.1`): bugfix only, no CLI changes, formula re-url+sha.
- Minor (`v0.2.0`): new flag or new default; call it out in the
  release notes.
- Major (`v1.0.0`): only if Microsoft breaks the piggyback and we
  rewrite around a new approach. At that point the SECURITY.md
  assumptions change and deserve a rewrite too.
