# v1-05-cdp-loopback-binding

Status: planned

Problem: Edge CDP launchers pass `--remote-debugging-port` but do not state the
intended loopback-only binding explicitly.

Scope:
- Add `--remote-debugging-address=127.0.0.1` to Python capture launches.
- Add the same flag to the legacy shell reseed launcher.
- Add tests for the Python launch args and the shell script flag.

Acceptance:
- Both CDP launchers explicitly bind to loopback.
- Existing capture/reseed behavior remains unchanged.

