# v1-02-agent-schema-contract

Status: planned

Problem: the schema advertises machine JSON for commands such as `token`,
`status`, `version`, and `profiles`, but `--agent <cmd>` currently fails for
plain `token` and `version` unless the caller already knows to add `--json`.

Scope:
- Make `--agent` add the command's JSON flag when the caller did not choose a
  different output mode.
- Leave explicit non-JSON choices such as `token --env` alone.
- Add tests for the self-sufficient agent path.

Acceptance:
- `owa-piggy --agent version` succeeds.
- `owa-piggy --agent token` succeeds when the profile can mint a token.
- The schema remains truthful for agent consumers.

