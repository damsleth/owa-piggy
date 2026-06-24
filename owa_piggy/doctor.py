"""``owa-piggy --doctor`` - data-class health check.

Schema matches owa-piggy's doctor JSON contract (see conventions.py).
Owa-piggy is the auth surface; doctor MUST NOT print or log tokens.
"""

from __future__ import annotations

import json
import sys

from owa_piggy.conventions import DoctorFinding, DoctorPayload


def run_doctor(*, fix: bool = False) -> DoctorPayload:
    payload = DoctorPayload()

    # --- Config / profiles --------------------------------------------------
    try:
        from owa_piggy.config import list_profiles

        profiles = list_profiles()
        payload.auth = {
            "profile_count": len(profiles),
            "profiles": [{"alias": alias} for alias in profiles],
        }
        if not profiles:
            payload.findings.append(
                DoctorFinding(
                    id="no_profiles",
                    severity="warning",
                    message="No owa-piggy profiles configured.",
                    hint="Run: owa-piggy setup --profile <alias> --email <addr>",
                )
            )
    except Exception as exc:
        payload.findings.append(
            DoctorFinding(
                id="config_unreadable",
                severity="error",
                message=f"Could not list profiles: {exc}",
                hint="Run: owa-piggy setup",
            )
        )

    # --- Config home --------------------------------------------------------
    try:
        import os

        payload.config_path = os.path.expanduser("~/.config/owa-piggy")
    except Exception:
        pass

    try:
        from owa_piggy.config import (
            audit_private_permissions,
            repair_private_permissions,
        )

        if fix:
            repair_private_permissions()
        for item in audit_private_permissions():
            payload.findings.append(
                DoctorFinding(
                    id="insecure_permissions",
                    severity="warning",
                    message=(
                        f"{item['label']} is mode {item['actual']}; expected {item['expected']}"
                    ),
                    hint=f"Run: owa-piggy --doctor --fix ({item['path']})",
                )
            )
    except Exception as exc:
        payload.findings.append(
            DoctorFinding(
                id="permission_audit_failed",
                severity="error",
                message=f"Could not audit config permissions: {exc}",
                hint="Check ~/.config/owa-piggy ownership and permissions.",
            )
        )

    return payload


def _print_human(payload: DoctorPayload) -> None:
    data = payload.to_dict()
    print(f"owa-piggy doctor (v{data['version']})")
    if payload.config_path:
        print(f"  config: {payload.config_path}")
    if payload.auth:
        print(f"  profiles: {payload.auth.get('profile_count', 0)}")
    if not payload.findings:
        print("  status: ok")
        return
    print(f"  findings: {len(payload.findings)}")
    for f in payload.findings:
        marker = {"error": "x", "warning": "!", "info": "."}.get(f.severity, ".")
        print(f"    {marker} [{f.severity}] {f.id}: {f.message}")
        if f.hint:
            print(f"        hint: {f.hint}")


def emit_doctor(as_json: bool, *, fix: bool = False) -> int:
    payload = run_doctor(fix=fix)
    if as_json:
        sys.stdout.write(json.dumps(payload.to_dict(), ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        _print_human(payload)
    return payload.exit_code()
