"""Higher-level profile registry and lifecycle operations.

``config.py`` owns low-level file parsing and path helpers. This module
keeps multi-step profile mutations in one place so the plain CLI and the
interactive picker do not grow different correctness rules.

Most functions return ``(ok, error)`` where ``ok`` is a bool and
``error`` is a human-readable message on failure. Callers decide how to
present the error (CLI prints to stderr; the TUI surfaces it as a status
line). ``create_profile`` returns the int rc convention (0/1) instead,
because it embeds a setup banner that already goes to stderr.
"""

from __future__ import annotations

import shutil
import sys
from typing import Any

from .cache import clear_cache
from .config import (
    ensure_profile_registered,
    list_profiles,
    load_config,
    load_profiles_conf,
    profile_dir,
    save_profiles_conf,
    set_active_profile,
    unregister_profile,
    validate_alias,
)
from .launchd import is_scheduled as launchd_is_scheduled
from .launchd import unschedule as launchd_unschedule
from .setup import interactive_setup


def promote_default_if_missing() -> dict[str, Any]:
    """Promote a remaining profile when the registry has no default."""
    reg = load_profiles_conf()
    if reg["OWA_DEFAULT_PROFILE"]:
        return reg
    remaining = list_profiles()
    if remaining:
        promoted = (reg["OWA_PROFILES"] or remaining)[0]
        reg["OWA_DEFAULT_PROFILE"] = promoted
        if promoted not in reg["OWA_PROFILES"]:
            reg["OWA_PROFILES"].append(promoted)
        save_profiles_conf(reg)
    return reg


def set_default_profile(alias: str) -> tuple[bool, str]:
    """Mark `alias` as the default profile and ensure it's enabled.

    Validates the alias and that the profile exists on disk before
    mutating profiles.conf. Returns ``(True, '')`` on success or
    ``(False, error)`` on failure.
    """
    ok, verr = validate_alias(alias)
    if not ok:
        return False, verr
    if alias not in list_profiles():
        return False, (
            f"profile {alias!r} not found. Available: {', '.join(list_profiles()) or '(none)'}"
        )
    reg = load_profiles_conf()
    reg["OWA_DEFAULT_PROFILE"] = alias
    # Re-register so the profile appears in OWA_PROFILES even if this is
    # a pre-registry profile (shouldn't happen post-migration but harmless).
    if alias not in reg["OWA_PROFILES"]:
        reg["OWA_PROFILES"].append(alias)
    save_profiles_conf(reg)
    return True, ""


def enable_profile(alias: str) -> tuple[bool, str]:
    """Add `alias` to OWA_PROFILES (no-op if already there). Sets it as
    the default if no default is set. Thin wrapper around
    ``ensure_profile_registered`` so callers can stay inside this
    module's ``(ok, error)`` convention.
    """
    try:
        ensure_profile_registered(alias, make_default_if_first=True)
    except ValueError as e:
        return False, str(e)
    return True, ""


def create_profile(
    alias: str,
    *,
    email: str | None = None,
    audience: str | None = None,
    full_banner: bool = False,
    trough_url: str | None = None,
    trough_tenant: str | None = None,
    trough_sub: str | None = None,
    user_agent: str | None = None,
    sharepoint_tenant: str | None = None,
    google: bool = False,
    google_client_id: str | None = None,
    google_client_secret: str | None = None,
    with_client: list[str] | None = None,
) -> int:
    """Run interactive_setup for a profile, persist its preferred audience,
    and register the profile in profiles.conf.

    Used by both `owa-piggy setup` (which never has an audience to set,
    so ``full_banner=True`` for the original "ENJOY YOUR APP-REG-FREE
    SCOPES" banner line) and the TUI's add-profile flow (audience comes
    from the interactive prompt; the second banner line is dropped
    because the picker redraws over it anyway).

    Returns the int rc convention: 0 on success, 1 on failure.
    """
    set_active_profile(alias)
    # The user is explicitly re-identifying; any cached AT belongs to
    # the pre-setup identity and must not leak past this point.
    clear_cache()
    config, _ = load_config()
    if audience and audience != "graph":
        # Pre-set OWA_DEFAULT_AUDIENCE on the in-memory config so
        # interactive_setup's save_config call writes it alongside the
        # tokens in one disk write.
        config["OWA_DEFAULT_AUDIENCE"] = audience
    if sharepoint_tenant and sharepoint_tenant.strip():
        # Same one-write piggyback as the audience: persist the SharePoint
        # tenant name so `--audience sharepoint` works on this profile
        # without re-passing --sharepoint-tenant every call.
        config["OWA_SHAREPOINT_TENANT"] = sharepoint_tenant.strip()
    if not interactive_setup(
        config,
        alias,
        email=email,
        trough_url=trough_url,
        trough_tenant=trough_tenant,
        trough_sub=trough_sub,
        user_agent=user_agent,
        google=google,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
    ):
        return 1
    if not google:
        _seed_bound_clients(alias, with_client, user_agent=user_agent)
    ensure_profile_registered(alias, make_default_if_first=True)
    print(f"\n\tOWA-PIGGY 🐽  CONFIGURED [{alias}]", file=sys.stderr)
    # The app-reg-free banner is about the MSAL piggyback trick specifically -
    # a Google profile uses a real app registration, so it doesn't apply.
    if full_banner and not google:
        print("\n\tENJOY YOUR APP-REG-FREE SCOPES\n", file=sys.stderr)
    return 0


def _seed_bound_clients(
    alias: str, with_client: list[str] | None, *, user_agent: str | None = None
) -> None:
    """Sign the fresh profile in to its other SPAs, same identity.

    The Edge sidecar just completed an interactive sign-in, so its cookies
    are as warm as they will ever be - the cheapest moment to pick up each
    extra client's refresh token. `--with-client` names them explicitly;
    the defaults are tried anyway because a Teams token is what makes
    chatsvc / authsvc reachable at all and its sign-in URL is the same for
    every tenant.

    A default that fails to mint is un-declared again: the tenant probably
    has no Teams, and a declaration nobody can capture would cost an Edge
    launch plus a capture timeout on every hourly reseed from now on. An
    explicitly requested client keeps its declaration, so the next reseed
    retries it.
    """
    from . import capture
    from . import clients as clients_mod

    requested: list[str] = []
    defaulted: list[str] = []
    for spec in with_client or ():
        client_id, url, err = clients_mod.parse_spec(spec)
        if err or client_id is None:
            print(f"WARNING: --with-client {spec!r}: {err}", file=sys.stderr)
            continue
        entry, derr = clients_mod.declare_client(alias, client_id, capture_url=url)
        if derr:
            print(f"WARNING: {derr}", file=sys.stderr)
            continue
        requested.append(client_id)
    for client_id in clients_mod.DEFAULT_CLIENTS:
        if client_id in requested or client_id in clients_mod.load_clients(alias):
            continue
        entry, derr = clients_mod.declare_client(alias, client_id)
        if not derr:
            defaulted.append(client_id)

    targets = requested + defaulted
    if not targets:
        return
    names = ", ".join(clients_mod.client_name(c) for c in targets)
    print(f"\n[{alias}] signing in to {names} under the same identity...", file=sys.stderr)
    _, failed = capture.capture_bound_clients(alias, user_agent=user_agent, only=targets)
    for client_id in defaulted:
        if clients_mod.client_name(client_id) in failed:
            clients_mod.forget_client(alias, client_id)
            print(
                f"[{alias}] {clients_mod.client_name(client_id)} not "
                f"available for this tenant; not added to the profile",
                file=sys.stderr,
            )


def disable_profile(alias: str, *, promote_replacement: bool = True) -> tuple[bool, str]:
    """Remove `alias` from OWA_PROFILES. If it was the default, optionally
    promote the first remaining enabled profile as the new default so
    ``resolve_profile`` keeps working without an explicit --profile.

    Pure registry op - does not touch disk or launchd. Returns
    ``(True, '')`` (always succeeds for a missing alias - removing
    something that isn't there is idempotent).
    """
    reg = load_profiles_conf()
    reg["OWA_PROFILES"] = [p for p in reg["OWA_PROFILES"] if p != alias]
    if reg["OWA_DEFAULT_PROFILE"] == alias:
        if promote_replacement and reg["OWA_PROFILES"]:
            reg["OWA_DEFAULT_PROFILE"] = reg["OWA_PROFILES"][0]
        else:
            reg["OWA_DEFAULT_PROFILE"] = ""
    save_profiles_conf(reg)
    return True, ""


def delete_profile(
    alias: str, *, uninstall_launchd: bool = True, promote_default: bool = True
) -> tuple[bool, str]:
    """Delete one profile directory and unregister it.

    Returns ``(ok, error)``. Registry update happens before directory
    removal: a leftover directory with no registry entry is recoverable,
    while a registry pointing at a deleted secret-bearing directory is more
    confusing for the next command.
    """
    if uninstall_launchd and launchd_is_scheduled(alias):
        rc = launchd_unschedule(alias)
        if rc != 0:
            return False, f"failed to unschedule launchd for {alias!r}"

    try:
        unregister_profile(alias)
    except OSError as e:
        return False, f"profile registry update failed: {e}"

    target = profile_dir(alias)
    try:
        shutil.rmtree(target)
    except OSError as e:
        return False, f"was unregistered but failed to remove {target}: {e}"

    if promote_default:
        try:
            promote_default_if_missing()
        except OSError as e:
            return False, f"deleted profile but failed to promote a default: {e}"
    return True, ""
