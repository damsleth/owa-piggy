# v1-04-launchd-state-consistency

Status: planned

Problem: `profiles schedule` can write `OWA_SCHEDULED` before the shared
LaunchAgent has actually installed, and profile deletion ignores launchd
unschedule failures.

Scope:
- Roll back `OWA_SCHEDULED` if shared-agent installation fails.
- Stop profile deletion when unscheduling a scheduled profile fails.
- Add tests for both failure paths.

Acceptance:
- A failed schedule install does not leave the profile marked scheduled.
- A failed unschedule does not delete or unregister the profile.

