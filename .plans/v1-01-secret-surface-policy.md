# v1-01-secret-surface-policy

Status: planned

Decision: do not broaden secret redaction. `owa-piggy` is an auth broker,
so human and agent token surfaces intentionally emit usable secrets. Highest
possible usability and lowest possible friction is the goal.

Scope:
- Document that token, JSON, env, and agent surfaces may carry full tokens.
- Explain that redaction is not a security boundary.
- Keep best-effort avoidance for shell history and persistent logs where it
  does not make normal token use harder.
- Remove doctor wording/tests that imply redaction is a required health check.

Acceptance:
- README and SECURITY clearly state the policy and rationale.
- `--doctor` no longer reports redaction health.
- Tests reflect the policy without broadening redaction.

