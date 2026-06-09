# Changelog

All notable changes to owa-piggy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(0.x: minor = user-visible change, patch = fix/polish).

Releases before v0.12.0 are recorded only in the annotated git tags
(`git tag -n99`).

## [0.15.0] - 2026-06-09

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

## [0.12.0] - machine surface for hugr-suite agents

- `schema` / `schema <command>` subcommands; `--help --json` returns the
  full command schema.
- `--agent <cmd>` wraps JSON stdout in an `{_owa, data}` envelope
  (`OWA_AGENT=1`); `--err-json` emits structured errors (`OWA_ERR_JSON=1`).
- Fixes: `--agent` no longer replays raw stdout on parse failure (token
  leak guard); `audiences`/`decode`/`remaining` declared text-only.
- Internal: token-flow extracted into `token_flow.py` (no behavior change).

[0.15.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.15.0
[0.14.1]: https://github.com/damsleth/owa-piggy/releases/tag/v0.14.1
[0.14.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.14.0
[0.13.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.13.0
[0.12.1]: https://github.com/damsleth/owa-piggy/releases/tag/v0.12.1
[0.12.0]: https://github.com/damsleth/owa-piggy/releases/tag/v0.12.0
