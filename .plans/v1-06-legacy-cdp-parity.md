# v1-06-legacy-cdp-parity

Status: planned

Problem: the package CDP client and standalone legacy scraper intentionally
duplicate low-level WebSocket/CDP code. That is still useful for compatibility,
but drift is an entropy risk.

Scope:
- Keep the legacy scraper for compatibility.
- Add an explicit shared parity marker to both implementations.
- Add a test that fails if one side is updated without acknowledging the other.
- Document the legacy path as a compatibility backend, not the preferred new
  profile path.

Acceptance:
- Test coverage makes CDP-helper drift visible.
- README describes network capture as the preferred setup path.

