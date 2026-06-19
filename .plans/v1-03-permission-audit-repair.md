# v1-03-permission-audit-repair

Status: planned

Problem: new config writes use private file modes, but existing config roots,
profile directories, and Edge sidecar directories are not audited or repaired.

Scope:
- Add a small config permission audit for known token-bearing paths.
- Add a repair helper that chmods only those known paths.
- Wire the audit into `--doctor`.
- Add `--doctor --fix` to apply the repair helper before reporting.

Acceptance:
- Doctor warns on group/world-accessible config paths.
- `--doctor --fix` repairs the known paths.
- No recursive chmod of the full Edge profile tree.

