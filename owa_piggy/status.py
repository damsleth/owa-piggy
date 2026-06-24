"""`status` (compact ISO8601 health summary) and `debug` (full diagnostics).

Both do a live exchange probe against AAD, which rotates the refresh token
as a side effect. That's fine - a normal invocation would do the same.
The exchange step itself goes through ``token_flow.exchange_fresh`` so
status, debug, and the main mint path share scope resolution, FOCI
shape checking, stderr capture, and rotated-RT persistence.

Both take an `alias` parameter so output can be labeled with the active
profile and the launchd plist we probe is the right one for that profile.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config as _config
from .config import (
    list_profiles,
    load_config,
    load_profiles_conf,
    profile_edge_dir,
    profiles_conf_path,
)
from .jwt import decode_jwt_segment
from .launchd import (
    SHARED_LABEL,
    shared_plist_path,
)
from .launchd import (
    is_scheduled as launchd_is_scheduled,
)
from .oauth import CLIENT_ID
from .scopes import KNOWN_AUDIENCES, resolve_audience
from .scripts import find_reseed_script
from .token_flow import exchange_fresh


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _humanize_minutes(m: int) -> str:
    """Compact human-readable duration: 29m, 1h29m, 2d3h."""
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h}h{mm}m"
    d, h = divmod(h, 24)
    return f"{d}d{h}h"


def _parse_iso(s: str) -> datetime | None:
    """Parse an `%Y-%m-%dT%H:%M:%SZ` UTC string, or None if malformed."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _minutes_until(dt: datetime) -> int:
    """Whole minutes from now until `dt`, floored at 0."""
    return max(0, int((dt - datetime.now(timezone.utc)).total_seconds() / 60))


def _rt_expires_at(config: dict[str, str]) -> str | None:
    dt = _parse_iso(config.get("OWA_RT_ISSUED_AT", "").strip())
    if dt is None:
        return None
    return (dt + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(token_ok: bool, minutes_remaining: int | None) -> str:
    if not token_ok:
        return "fail"
    if minutes_remaining is not None and minutes_remaining < 10:
        return "warn"
    return "ok"


def _profile_is_disabled(alias: str) -> bool:
    """Return True when profiles.conf exists and omits `alias`.

    Missing profiles.conf means a legacy/test layout that predates the
    registry, so all on-disk profiles remain active for compatibility.
    A present-but-empty registry means the user disabled every profile.
    """
    if not profiles_conf_path().exists():
        return False
    registered = load_profiles_conf().get("OWA_PROFILES", [])
    return alias not in registered


def _probe_profile(
    alias: str,
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
) -> dict[str, Any]:
    """Live exchange probe for one profile, with no global side effects.

    Reads the profile's config by explicit path and persists any rotated RT
    back to that same path, so several profiles can be probed concurrently
    without racing the module-global CONFIG_PATH / sys.stderr. Returns a
    normalised result dict that both the JSON (_status_json) and human
    (_status_human) formatters render - the single source of truth that
    replaced the two divergent probe copies.
    """
    config_path = _config.profile_config_path(alias)
    config, persist = load_config(config_path)
    rt = config.get("OWA_REFRESH_TOKEN", "").strip()
    probe: dict[str, Any] = {
        "alias": alias,
        "rt_present_cfg": bool(rt),
        "rt_expires_at": _rt_expires_at(config),
        "rt_issued_at": config.get("OWA_RT_ISSUED_AT", "").strip(),
        "default_audience": audience or config.get("OWA_DEFAULT_AUDIENCE", "").strip() or "graph",
        "disabled": False,
        "resolve_error": None,
        "info": None,
        "result": None,
        "exchange_error": None,
        "payload": None,
        "decode_failed": False,
        "scheduled": launchd_is_scheduled(alias),
    }
    if _profile_is_disabled(alias):
        probe["disabled"] = True
        return probe

    probe_scope, err = resolve_audience(
        audience,
        scope,
        profile_default=config.get("OWA_DEFAULT_AUDIENCE", "").strip(),
        sharepoint_tenant=sharepoint_tenant,
        profile_sharepoint_tenant=config.get("OWA_SHAREPOINT_TENANT", "").strip(),
    )
    if err:
        probe["resolve_error"] = err
        return probe

    # exchange_fresh handles config field extraction, FOCI shape check,
    # thread-local stderr capture (so we can surface the AAD error instead
    # of leaking it to stdout), and rotated-RT persistence to config_path.
    result, info = exchange_fresh(
        config, probe_scope, persist=persist, capture_stderr=True, config_path=config_path
    )
    probe["result"] = result
    probe["info"] = info
    if not info["rt_present"] or not info["tid_present"] or not info["rt_shape_ok"]:
        return probe
    if not result or not result.get("access_token"):
        probe["exchange_error"] = next(
            (line for line in info["stderr_text"].splitlines() if line.startswith("ERROR: ")), ""
        )
        return probe

    at = result["access_token"]
    try:
        probe["payload"] = decode_jwt_segment(at.split(".")[1])
    except Exception:
        probe["decode_failed"] = True
    return probe


def _probe_all(
    profiles: list[str],
    audience: str | None,
    scope: str | None,
    sharepoint_tenant: str | None,
) -> list[dict[str, Any]]:
    """Probe every profile, returning results in the same order as `profiles`.

    The probes are network-bound (one AAD exchange each) and independent, so
    they run concurrently on a thread pool; a single bad profile no longer
    serialises the whole run behind it. ThreadPoolExecutor.map preserves
    input order, so formatting downstream stays deterministic.
    """
    if len(profiles) <= 1:
        return [_probe_profile(a, audience, scope, sharepoint_tenant) for a in profiles]
    with ThreadPoolExecutor(max_workers=min(len(profiles), 8)) as pool:
        return list(
            pool.map(lambda a: _probe_profile(a, audience, scope, sharepoint_tenant), profiles)
        )


def _status_json(probe: dict[str, Any]) -> dict[str, Any]:
    """Render a probe result as the token-health report dict (no token values)."""
    report: dict[str, Any] = {
        "profile": probe["alias"],
        "state": "fail",
        "audience": probe["default_audience"],
        "access_token": {
            "present": False,
            "expires_at": None,
            "minutes_remaining": None,
        },
        "refresh_token": {
            "present": probe["rt_present_cfg"],
            "expires_at": probe["rt_expires_at"],
            "minutes_remaining": None,
        },
        "hints": [],
    }
    if probe["disabled"]:
        report["state"] = "disabled"
        report["hints"].append("profile is disabled")
        return report
    exp_dt = _parse_iso(report["refresh_token"]["expires_at"])
    if exp_dt is not None:
        report["refresh_token"]["minutes_remaining"] = _minutes_until(exp_dt)

    if probe["resolve_error"]:
        report["hints"].append(probe["resolve_error"])
        return report

    info = probe["info"]
    if not info["rt_present"]:
        report["hints"].append(f"run owa-piggy setup --profile {probe['alias']}")
        return report
    if not info["tid_present"]:
        report["hints"].append(f"profile {probe['alias']} is missing OWA_TENANT_ID")
        return report
    if not info["rt_shape_ok"]:
        report["hints"].append("refresh token is not an AAD FOCI token; reseed from Microsoft Edge")
        return report

    result = probe["result"]
    if not result or not result.get("access_token"):
        err_line = probe["exchange_error"]
        report["hints"].append(err_line[len("ERROR: ") :] if err_line else "token exchange failed")
        return report

    if probe["decode_failed"] or probe["payload"] is None:
        report["hints"].append("access token decode failed")
        return report

    payload = probe["payload"]
    exp_ts = int(payload.get("exp", 0))
    minutes = max(0, int((exp_ts - time.time()) / 60)) if exp_ts else None
    raw_aud = payload.get("aud", "")
    aud = raw_aud[0] if isinstance(raw_aud, list) and raw_aud else raw_aud
    report["audience"] = aud if isinstance(aud, str) and aud else report["audience"]
    report["access_token"] = {
        "present": True,
        "expires_at": _iso(exp_ts) if exp_ts else None,
        "minutes_remaining": minutes,
    }
    report["state"] = _state(True, minutes)
    return report


def status_report(
    alias: str,
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
) -> dict[str, Any]:
    """Return a token health report for profile <alias> without token values."""
    return _status_json(_probe_profile(alias, audience, scope, sharepoint_tenant))


def status_all_report(
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
) -> dict[str, Any]:
    profiles = list_profiles()
    reports = [
        _status_json(probe) for probe in _probe_all(profiles, audience, scope, sharepoint_tenant)
    ]
    summary = {"ok": 0, "warn": 0, "fail": 0}
    for report in reports:
        summary[report.get("state", "fail")] = summary.get(report.get("state", "fail"), 0) + 1
    return {"profiles": reports, "summary": summary}


def _status_human(probe: dict[str, Any], multi: bool = False, verbose: bool = False) -> int:
    """Render a probe result as the compact ISO8601 health block. Prints the
    lines and returns the per-profile exit code (0 healthy, 1 otherwise).

    The per-profile header (`profile: <alias>`) goes to stdout in
    multi-profile mode so concatenated output is self-describing, and to
    stderr in single-profile mode so scripts that only consume stdout don't
    have to filter it out (the `no valid token` / ISO8601 lines remain the
    script-friendly stdout payload)."""
    alias = probe["alias"]
    label_stream = sys.stdout if multi else sys.stderr
    print(f"profile:      {alias}", file=label_stream)

    # Disabled profiles (on disk but not in OWA_PROFILES) get a one-line
    # status and no probe - the core probe already skipped the exchange.
    if probe["disabled"]:
        print("status:       disabled")
        return 0

    if probe["resolve_error"]:
        print(f"ERROR: {probe['resolve_error']}", file=sys.stderr)
        return 1

    info = probe["info"]
    if not info["rt_present"] or not info["tid_present"] or not info["rt_shape_ok"]:
        print("no valid token")
        return 1

    result = probe["result"]
    if not result or not result.get("access_token"):
        print("no valid token")
        # Send the AAD error to the same stream as the [profile=...]
        # label so single-profile mode keeps its strict stdout contract
        # (stdout == 'no valid token') while multi-profile output stays
        # self-describing when scanning several profiles at once.
        if probe["exchange_error"]:
            print(probe["exchange_error"], file=label_stream)
        return 1

    if probe["decode_failed"] or probe["payload"] is None:
        print("no valid token")
        return 1

    payload = probe["payload"]
    exp_ts = int(payload.get("exp", 0))
    at_minutes = max(0, int((exp_ts - time.time()) / 60)) if exp_ts else None
    scp = payload.get("scp") or payload.get("roles") or ""
    # aud can be a string (v1) or an array (v2 spec allows it). Normalise.
    raw_aud = payload.get("aud", "")
    aud = raw_aud[0] if isinstance(raw_aud, list) and raw_aud else raw_aud
    aud = aud if isinstance(aud, str) else str(aud)

    # Map the aud claim back to a KNOWN_AUDIENCES short name (reverse
    # lookup). Graph uses a GUID audience in some flows, so accept either
    # the URL or the well-known Graph GUID.
    aud_name = None
    if aud == "00000003-0000-0000-c000-000000000000":
        aud_name = "graph"
    else:
        for name, entry in KNOWN_AUDIENCES.items():
            url = entry[0]
            if aud == url or aud.rstrip("/") == url.rstrip("/"):
                aud_name = name
                break
    audience_line = f"{aud_name} ({aud})" if aud_name else (aud or "unknown")

    # Rotated RT persistence is handled by the core probe via exchange_fresh.

    # Refresh token hard-cap: issued_at + 24h. Parse OWA_RT_ISSUED_AT if set.
    rt_expires = "unknown (run `owa-piggy reseed` to establish)"
    dt = _parse_iso(probe["rt_issued_at"])
    if dt is not None:
        exp_dt = dt + timedelta(hours=24)
        rt_expires = (
            f"{exp_dt.strftime('%Y-%m-%dT%H:%M:%SZ')} ({_humanize_minutes(_minutes_until(exp_dt))})"
        )

    # OWA-issued access tokens always carry the same dense scope set, so
    # spelling out three names and a count was pure noise. Collapse to
    # `default(N)`. Non-string scp (rare) falls back to its raw repr.
    if isinstance(scp, str):
        parts = scp.split()
        scopes_line = f"default({len(parts)})" if parts else ""
    else:
        scopes_line = str(scp)

    at_expires = _iso(exp_ts)
    if at_minutes is not None:
        at_expires = f"{at_expires} ({_humanize_minutes(at_minutes)})"

    scheduled_state = "true" if probe["scheduled"] else "false"
    print(f"authtoken:    expires {at_expires}")
    print(f"refreshtoken: expires {rt_expires}")
    # audience and scopes are stable noise (OWA always mints the same dense
    # scope set against the same audience), so they're verbose-only.
    if verbose:
        print(f"audience:     {audience_line}")
        print(f"scopes:       {scopes_line}")
    print(f"scheduled:    {scheduled_state}")
    return 0


def do_status(
    alias: str,
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
    multi: bool = False,
    verbose: bool = False,
) -> int:
    """Compact health summary for profile <alias>. Does a live exchange
    probe to verify the RT actually works (rotates it as a side effect,
    which is fine - the RT rotates on every use anyway). Prints three
    ISO8601 lines or the single line 'no valid token' if anything is
    missing or the probe fails.

    Refresh-token expiry uses OWA_RT_ISSUED_AT + 24h if it's in the config
    (set by `setup` and `reseed`). That's the SPA hard-cap, which is the
    binding constraint since hourly rotation keeps the sliding window
    permanently fresh. If the field is missing (pre-existing setups from
    before this flag landed) we fall back to 'unknown'.

    `multi=True` is set by do_status_all() when iterating every profile."""
    probe = _probe_profile(alias, audience, scope, sharepoint_tenant)
    return _status_human(probe, multi=multi, verbose=verbose)


def do_status_all(
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
    verbose: bool = False,
) -> int:
    """Run the status probe against every configured profile.

    Used when `status` is invoked with no explicit --profile / no
    OWA_PROFILE env var. The probes run concurrently (see _probe_all); each
    profile then gets its own labeled block, separated by a blank line, in
    configuration order. Exit code is the max of the per-profile return codes
    so any unhealthy profile is still surfaced to scripts.
    """
    profiles = list_profiles()
    if not profiles:
        print("no profiles configured. Run: owa-piggy setup --profile <alias>", file=sys.stderr)
        return 1
    probes = _probe_all(profiles, audience, scope, sharepoint_tenant)
    rc = 0
    for i, probe in enumerate(probes):
        if i:
            print()
        rc = max(rc, _status_human(probe, multi=True, verbose=verbose))
    return rc


def do_debug(
    alias: str,
    audience: str | None = None,
    scope: str | None = None,
    sharepoint_tenant: str | None = None,
) -> int:
    """Dump everything useful to diagnose a broken setup for profile <alias>:
    config file, refresh-token shape, live exchange probe, access-token
    claims, launchd agent status, PATH install, sidecar profile. Also
    lists all configured profiles for context. Read-mostly: the probe
    exchange does rotate the refresh token as a side effect (same as a
    normal invocation), because that's the only honest way to prove the
    token is currently valid."""

    config, persist = load_config()

    # Resolve scope up front and bail on argument errors, matching the
    # rest of the CLI. Previously debug silently ignored resolve's error
    # and probed with a None scope, which masked what was actually a fatal
    # arg error with misleading AAD output.
    debug_scope, scope_err = resolve_audience(
        audience,
        scope,
        profile_default=config.get("OWA_DEFAULT_AUDIENCE", "").strip(),
        sharepoint_tenant=sharepoint_tenant,
        profile_sharepoint_tenant=config.get("OWA_SHAREPOINT_TENANT", "").strip(),
    )
    if scope_err:
        print(f"ERROR: {scope_err}", file=sys.stderr)
        return 1

    def row(status: str, label: str, detail: str = "") -> None:
        print(f"  [{status}] {label}" + (f": {detail}" if detail else ""))

    print(f"owa-piggy debug [profile={alias}]\n")

    # --- Profile registry ---
    reg = load_profiles_conf()
    profiles = list_profiles()
    print("Profiles:")
    if profiles:
        for p in profiles:
            marker = "*" if p == reg["OWA_DEFAULT_PROFILE"] else " "
            active = "  (active)" if p == alias else ""
            print(f"  {marker} {p}{active}")
    else:
        row("no", "no profiles registered")

    # --- Config file ---
    cfg_path = _config.CONFIG_PATH
    print(f"\nConfig file ({cfg_path}):")
    if cfg_path.exists():
        st = cfg_path.stat()
        mode = oct(st.st_mode & 0o777)
        age_min = int((time.time() - st.st_mtime) / 60)
        row("ok", "present", f"perms {mode}, modified {age_min}min ago")
    else:
        row("no", "missing")

    rt = config.get("OWA_REFRESH_TOKEN", "").strip()
    tid = config.get("OWA_TENANT_ID", "").strip()
    cid = config.get("OWA_CLIENT_ID", CLIENT_ID).strip()
    source = "config file" if persist else ("env only" if rt else "")
    row("ok" if rt else "no", "OWA_REFRESH_TOKEN", f"{len(rt)} bytes, {source}" if rt else "unset")
    row("ok" if tid else "no", "OWA_TENANT_ID", tid or "unset")
    row(
        "..",
        "OWA_CLIENT_ID",
        f"{cid}{' (default OWA first-party)' if cid == CLIENT_ID else ' (override)'}",
    )

    # --- Refresh token shape + live probe ---
    print("\nRefresh token:")
    if not rt:
        row(
            "no",
            f"absent; run `owa-piggy setup --profile {alias}` or "
            f"`owa-piggy reseed --profile {alias}`",
        )
    else:
        shape_ok = rt.startswith("1.") or rt.startswith("0.")
        row(
            "ok" if shape_ok else "no",
            f"FOCI shape ({rt[:2]}...)"
            if shape_ok
            else f"NOT FOCI (starts {rt[:4]!r}); AAD will reject as malformed",
        )

        if shape_ok and tid:
            print("  probing live exchange against AAD...")
            # exchange_fresh handles persistence of any rotated RT when
            # persist=True; we let stderr flow through (capture_stderr=False)
            # so AAD error text reaches the user via the same path the
            # `exchange failed - see error above` row points at.
            result, _info = exchange_fresh(
                config, debug_scope, persist=persist, capture_stderr=False
            )
            if result and result.get("access_token"):
                row("ok", "exchange succeeded")
                at = result["access_token"]
                try:
                    payload = decode_jwt_segment(at.split(".")[1])
                    aud = payload.get("aud", "?")
                    scp = payload.get("scp", payload.get("roles", "?"))
                    exp = payload.get("exp", 0)
                    iat = payload.get("iat", 0)
                    now = time.time()
                    row("..", "access token aud", str(aud))
                    if isinstance(scp, str) and len(scp) > 80:
                        # OWA scopes are legion (~100 space-separated entries).
                        # Show count and the first few so `debug` stays useful.
                        parts = scp.split()
                        preview = ", ".join(parts[:3])
                        row("..", "access token scp", f"{len(parts)} scopes ({preview}, ...)")
                    else:
                        row("..", "access token scp", str(scp))
                    row(
                        "..",
                        "access token exp",
                        f"in {int((exp - now) / 60)} min ({time.strftime('%H:%M:%S', time.localtime(exp))})",  # noqa: E501
                    )
                    row("..", "access token iat", f"{int((now - iat) / 60)} min ago")
                except Exception as e:
                    row("no", "access token decode failed", str(e))

                if _info["rotated"]:
                    if persist:
                        row("ok", "refresh token rotated and persisted")
                    else:
                        row("..", "refresh token rotated (env-only, not persisted)")
            else:
                row("no", "exchange failed - see error above")

    # --- Launchd agent ---
    # One shared agent reseeds every profile in OWA_SCHEDULED; whether THIS
    # profile is actually rotated by it is a registry-membership question,
    # separate from whether the shared plist is installed/loaded.
    label = SHARED_LABEL
    print(f"\nLaunchd refresh agent ({label}):")
    row("ok" if launchd_is_scheduled(alias) else "no", f"{alias!r} in schedule (OWA_SCHEDULED)")
    plist_path = shared_plist_path()
    row("ok" if plist_path.exists() else "no", "shared plist file", str(plist_path))

    uid = os.getuid()
    target = f"gui/{uid}/{label}"
    try:
        proc = subprocess.run(
            ["launchctl", "print", target], capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            row("ok", "bootstrapped", target)
            # Surface the handful of fields that actually tell you if it's
            # healthy: runs, last exit, state, pid. launchctl print is a
            # nested tree so `state` appears multiple times - take only the
            # first occurrence of each wanted key (the top-level scope).
            wanted = ("state", "runs", "last exit code", "last exit reason", "pid")
            seen = set()
            for line in proc.stdout.splitlines():
                s = line.strip()
                for w in wanted:
                    if w in seen:
                        continue
                    if s.startswith(w + " =") or s.startswith(w + ":"):
                        print(f"      {s}")
                        seen.add(w)
                        break
        else:
            row("no", "not loaded", (proc.stderr.strip().splitlines() or [""])[0][:120])
    except FileNotFoundError:
        row("..", "launchctl not found on PATH (non-macOS?)")
    except subprocess.TimeoutExpired:
        row("no", "launchctl print timed out")

    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if "owa-piggy" in proc.stdout:
            row(
                "!!", "legacy cron entry still present", "run ./scripts/setup-refresh.sh to migrate"
            )
    except Exception:
        pass

    # --- Installation / PATH ---
    print("\nInstallation:")
    import shutil

    installed = shutil.which("owa-piggy")
    if installed:
        installed_path = Path(installed)
        detail = str(installed_path)
        if installed_path.is_symlink():
            detail += f" -> {os.readlink(installed_path)}"
        row("ok", "owa-piggy on PATH", detail)
    else:
        row("no", "owa-piggy not on PATH", "run ./scripts/add-to-path.sh or pipx install .")

    sidecar = profile_edge_dir(alias)
    row(
        "ok" if sidecar.is_dir() else "no",
        "Edge sidecar profile",
        str(sidecar) if sidecar.is_dir() else f"{sidecar} (missing; `reseed` needs this)",
    )

    reseed = find_reseed_script()
    row(
        "ok" if reseed else "no",
        "reseed script",
        str(reseed)
        if reseed
        else "not found in any standard location (OWA_RESEED_SCRIPT overrides)",
    )

    return 0
