# v1-07-packaging-metadata

Status: implemented

Problem: package builds succeed, but setuptools warns that current license
metadata is deprecated. The classifier also still marks the project beta.

Scope:
- Switch to SPDX license metadata and `license-files`.
- Remove the deprecated license classifier.
- Move the development status classifier to a v1-ready state.

Acceptance:
- `uv build` succeeds without setuptools license deprecation warnings.
- Wheel and sdist still include `LICENSE`.
