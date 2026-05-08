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
import shutil
import sys

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
from .launchd import is_installed as launchd_is_installed
from .launchd import run_setup_refresh
from .setup import interactive_setup


def promote_default_if_missing():
    """Promote a remaining profile when the registry has no default."""
    reg = load_profiles_conf()
    if reg['OWA_DEFAULT_PROFILE']:
        return reg
    remaining = list_profiles()
    if remaining:
        promoted = (reg['OWA_PROFILES'] or remaining)[0]
        reg['OWA_DEFAULT_PROFILE'] = promoted
        if promoted not in reg['OWA_PROFILES']:
            reg['OWA_PROFILES'].append(promoted)
        save_profiles_conf(reg)
    return reg


def set_default_profile(alias):
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
            f'profile {alias!r} not found. Available: '
            f'{", ".join(list_profiles()) or "(none)"}'
        )
    reg = load_profiles_conf()
    reg['OWA_DEFAULT_PROFILE'] = alias
    # Re-register so the profile appears in OWA_PROFILES even if this is
    # a pre-registry profile (shouldn't happen post-migration but harmless).
    if alias not in reg['OWA_PROFILES']:
        reg['OWA_PROFILES'].append(alias)
    save_profiles_conf(reg)
    return True, ''


def enable_profile(alias):
    """Add `alias` to OWA_PROFILES (no-op if already there). Sets it as
    the default if no default is set. Thin wrapper around
    ``ensure_profile_registered`` so callers can stay inside this
    module's ``(ok, error)`` convention.
    """
    try:
        ensure_profile_registered(alias, make_default_if_first=True)
    except ValueError as e:
        return False, str(e)
    return True, ''


def create_profile(alias, *, email=None, audience=None, full_banner=False):
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
    if audience and audience != 'graph':
        # Pre-set OWA_DEFAULT_AUDIENCE on the in-memory config so
        # interactive_setup's save_config call writes it alongside the
        # tokens in one disk write.
        config['OWA_DEFAULT_AUDIENCE'] = audience
    if not interactive_setup(config, alias, email=email):
        return 1
    ensure_profile_registered(alias, make_default_if_first=True)
    print(f'\n\tOWA-PIGGY 🐽  CONFIGURED [{alias}]', file=sys.stderr)
    if full_banner:
        print('\n\tENJOY YOUR APP-REG-FREE SCOPES\n', file=sys.stderr)
    return 0


def disable_profile(alias, *, promote_replacement=True):
    """Remove `alias` from OWA_PROFILES. If it was the default, optionally
    promote the first remaining enabled profile as the new default so
    ``resolve_profile`` keeps working without an explicit --profile.

    Pure registry op - does not touch disk or launchd. Returns
    ``(True, '')`` (always succeeds for a missing alias - removing
    something that isn't there is idempotent).
    """
    reg = load_profiles_conf()
    reg['OWA_PROFILES'] = [p for p in reg['OWA_PROFILES'] if p != alias]
    if reg['OWA_DEFAULT_PROFILE'] == alias:
        if promote_replacement and reg['OWA_PROFILES']:
            reg['OWA_DEFAULT_PROFILE'] = reg['OWA_PROFILES'][0]
        else:
            reg['OWA_DEFAULT_PROFILE'] = ''
    save_profiles_conf(reg)
    return True, ''


def delete_profile(alias, *, uninstall_launchd=True, promote_default=True):
    """Delete one profile directory and unregister it.

    Returns ``(ok, error)``. Registry update happens before directory
    removal: a leftover directory with no registry entry is recoverable,
    while a registry pointing at a deleted secret-bearing directory is more
    confusing for the next command.
    """
    if uninstall_launchd and launchd_is_installed(alias):
        run_setup_refresh(alias, install=False)

    try:
        unregister_profile(alias)
    except OSError as e:
        return False, f'profile registry update failed: {e}'

    target = profile_dir(alias)
    try:
        shutil.rmtree(target)
    except OSError as e:
        return False, f'was unregistered but failed to remove {target}: {e}'

    if promote_default:
        try:
            promote_default_if_missing()
        except OSError as e:
            return False, f'deleted profile but failed to promote a default: {e}'
    return True, ''
