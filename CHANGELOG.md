# Changelog

All notable changes to owa-piggy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(0.x: minor = user-visible change, patch = fix/polish).

Releases before v0.12.0 are recorded only in the annotated git tags
(`git tag -n99`).

## [1.0.0] - 2026-06-19

First stable release. No breaking API changes from 0.17.0 — this marks the
project as production-ready after a security-hardening and cleanup pass.

### Security
- Edge CDP now binds to loopback only, and a parity guard keeps the CDP helper
  in sync so the debugging port can't be reached off-host.
- `owa-piggy` audits and repairs config-file permissions on startup, and the
  docs now call out every secret-bearing token surface.

### Changed
- Agent machine commands default to JSON output.
- Modernized packaging metadata.
- launchd schedule state is kept consistent across reseeds.
- Internal cleanup: trimmed over-engineering (~97 fewer LoC, no behavior
  change) and shrank two helpers (`dict.fromkeys` dedup, dropped a redundant
  cache guard).

## [0.17.0] - 2026-06-16

### Added
- `owa-piggy tui`: an interactive token-health dashboard. One screen shows every
  profile with a live freshness column (green `fresh 58m`, yellow `expiring 4m`,
  red with the fix hint when a reseed is needed), driven by the same concurrent
  `status` probe used by `status` with no `--profile`. Carries all the single-key
  registry actions (toggle, set-default, schedule, add/delete, edge) plus reseed
  (`r`/`R`) and a manual refresh (`g`); state-changing actions re-probe
  automatically. Probing is network-bound, so the screen paints a `probing...`
  skeleton first, then fills in results. Falls back to a plain status table when
  stdin/stdout isn't a TTY. The command is interactive, so it's excluded from the
  `--agent`/`--err-json` machine surface.

### Changed
- Bare `owa-piggy profiles` on a TTY now opens the new dashboard instead of the
  old profile picker. The two interactive screens have been consolidated into one
  (`run_dashboard`) — the picker was a strict subset of the dashboard minus the
  freshness column. `profiles list` and the other `profiles` subcommands are
  unchanged; scripts and non-TTY callers still get the offline plain list.

## [0.16.2] - 2026-06-15

### Fixed
- `status` (and every token exchange) no longer hangs for minutes on hosts with
  a broken/blackholed IPv6 route. `login.microsoftonline.com` resolves to IPv6
  addresses first, and Python's `socket.create_connection` tries them strictly
  in order, blocking on each dead address until the OS TCP timeout (~75s).
  Exchanges now connect via a Happy Eyeballs connector (RFC 8305) that races
  IPv4/IPv6 concurrently and uses the first to connect, like curl. TLS cert and
  hostname verification are unchanged. `EXCHANGE_TIMEOUT` caps each attempt.

### Changed
- `status` with no `--profile` now probes all profiles concurrently instead of
  serially, so one slow or failing profile no longer blocks the rest. Output is
  unchanged: stanzas are still printed in configuration order.
- Internal: the JSON (`--json`) and human `status` paths now share a single
  thread-safe probe core (`_probe_profile`), replacing two divergent copies.
  Token-exchange error capture moved from swapping the global `sys.stderr` to a
  thread-local sink so concurrent probes can't clobber each other.

## [0.16.1] - 2026-06-12

### Added
- Capture a non-FOCI client's refresh token off the wire: point the capture
  sidecar at a non-OWA SPA with `OWA_CAPTURE_URL` (e.g. the Azure DevOps app)
  and grab its bound refresh token, which the FOCI client cannot mint itself
  (AADSTS65002 preauth wall). `OWA_CLIENT_ID` / `OWA_ORIGIN` / `OWA_CAPTURE_URL`
  are persisted to the profile config so the token exchange replays under the
  same minting client and origin.

### Fixed
- Scheduled reseed of a non-FOCI profile now navigates to the SPA it was
  captured against (the persisted `OWA_CAPTURE_URL`) and rotates *that*
  client's refresh token instead of OWA's. Previously the silent reseed loaded
  OWA, never touched the non-FOCI client's MSAL cache, and the launchd reseed
  quietly rotted until a manual re-seed.
- Relaxed the refresh-token shape check in `token_flow`: the `1.`/`0.` prefix
  is a FOCI family property, so non-FOCI clients carry an opaque RT. Defer to
  AAD to reject a malformed token rather than failing the shape gate locally.

## [0.16.0] - 2026-06-09

### Added
- Standalone binary releases: each tagged release attaches a per-OS/arch
  tarball (Linux x86_64, macOS x86_64, macOS arm64) with a single PyInstaller
  binary - run owa-piggy with no Python install. Built via
  `packaging/owa-piggy.spec`.
- `-v` is now accepted as a short alias for the top-level `--version` flag.
  (The `status` subcommand's `-v`/`--verbose` is unaffected.)

### Changed
- Decoupled from the internal "hugr" suite framing; owa-piggy is documented as
  a standalone auth broker for the `owa-*` tools. No behavior change.

## [0.15.1] - 2026-06-09

First public release of the SharePoint work. (v0.15.0 was tagged but never
released - a CI shellcheck gate failed on a pre-existing line - so the
SharePoint feature ships under 0.15.1.)

### Fixed
- `scripts/setup-refresh.sh`: rewrite the best-effort `lsregister` call as an
  explicit `if`-block (newer shellcheck flagged the `A && B || C` form,
  SC2015); behavior unchanged.

### Added
- Tenant-templated SharePoint audiences: `--audience sharepoint`
  (`https://<tenant>.sharepoint.com`, site collections / content) and
  `--audience sharepoint-admin` (`https://<tenant>-admin.sharepoint.com`,
  tenant admin CSOM / REST). The FOCI refresh token captured from the
  Outlook sign-in works for these resources unchanged - no separate sign-in.
- Automatic SharePoint tenant resolution: on first use of a templated
  audience with no tenant configured, owa-piggy mints a Graph token, reads
  the hostname from `GET /sites/root`, and persists it as
  `OWA_SHAREPOINT_TENANT` on the profile so later calls skip the round-trip.
  New module `owa_piggy/sharepoint.py` (stdlib only).
- `--sharepoint-tenant <name>` flag (on the token path plus `setup` /
  `profiles new`) and `OWA_SHAREPOINT_TENANT` config/env key to set or
  override the SharePoint tenant explicitly.
- `owa-piggy audiences` now lists the tenant-templated audiences.
- README: SharePoint section with a PnP PowerShell (`Connect-PnPOnline
  -AccessToken`) walkthrough; `CHANGELOG.md` added.

### Notes
- Whether a token carries tenant-admin capability (e.g. `Sites.FullControl.All`)
  depends on the FOCI client's pre-consented delegated permissions and your
  directory roles - inspect with `owa-piggy debug --audience sharepoint-admin`.

## [0.14.1] - quieter status output

- `status`: audience + scopes lines now gated behind `--verbose`/`-v`
  (they were stable noise - OWA always mints the same scope set).
- `status --json` output unchanged (always carries audience).

## [0.14.0] - trough seeding and User-Agent spoofing

- `setup`: seed from a tailnet-side trough appliance (`--from-trough <url>`,
  `--trough-tenant`, `--trough-sub`, `OWA_TROUGH_URL`).
- `capture`/`reseed`: spoof the Edge sidecar User-Agent
  (`setup --user-agent <ua>`, `OWA_USER_AGENT`); persisted per-profile and
  re-applied on every silent reseed.
- New module `owa_piggy/trough.py` (stdlib only, lazily imported).

## [0.13.0] - single shared launchd reseed agent

- Replaced the per-profile LaunchAgent model with one shared agent
  (`com.damsleth.owa-piggy.scheduled`) driven by `OWA_SCHEDULED` in
  `profiles.conf`; macOS Login Items shows a single owa-piggy row.
- New `OWA_SCHEDULED` registry key; `owa-piggy reseed --scheduled` reseeds
  that set. New `profiles schedule|unschedule <alias>` commands and TUI keys.
- `scheduled` boolean in `profiles list --json`; status prints `scheduled:`.
- **Breaking:** old per-profile plists are no longer created - re-run
  `owa-piggy profiles schedule <alias>` for each profile you want rotated.

## [0.12.1] - profiles new subcommand

- `owa-piggy profiles new <alias>` as a thin alias for
  `owa-piggy setup --profile <alias>`, with `--email` for Edge capture.

## [0.12.0] - machine surface for agents

- `schema` / `schema <command>` subcommands; `--help --json` returns the
  full command schema.
- `--agent <cmd>` wraps JSON stdout in an `{_owa, data}` envelope
  (`OWA_AGENT=1`); `--err-json` emits structured errors (`OWA_ERR_JSON=1`).
- Fixes: `--agent` no longer replays raw stdout on parse failure (token
  leak guard); `audiences`/`decode`/`remaining` declared text-only.
- Internal: token-flow extracted into `token_flow.py` (no behavior change).

[0.17.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.17.0
[0.16.2]: https://github.com/damsleth/owa-piggy/releases/tag/v0.16.2
[0.16.1]: https://github.com/damsleth/owa-piggy/releases/tag/v0.16.1
[0.16.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.16.0
[0.15.1]: https://github.com/damsleth/owa-piggy/releases/tag/v0.15.1
[0.15.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.15.0
[0.14.1]: https://github.com/damsleth/owa-piggy/releases/tag/v0.14.1
[0.14.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.14.0
[0.13.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.13.0
[0.12.1]: https://github.com/damsleth/owa-piggy/releases/tag/v0.12.1
[0.12.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.12.0
