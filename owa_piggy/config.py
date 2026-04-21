"""Config file I/O and the small helpers that go with it.

Refresh tokens rotate on every call, so a partial write here corrupts the
only live token and forces a browser reseed. All writes go through a temp
file + fsync + rename.

Profile layout (since multi-tenant support landed):
    ~/.config/owa-piggy/
      profiles.conf                     OWA_DEFAULT_PROFILE + OWA_PROFILES
      profiles/
        <alias>/
          config                        per-profile KV (same schema as legacy)
          cache.json                    per-profile access-token cache
          edge-profile/                 per-profile Edge sidecar userdata dir
          refresh.log                   per-profile launchd stderr

`ROOT_DIR` is mutable so tests can redirect it. `CONFIG_PATH` points at the
*currently active* profile's config file and is rebound by
`set_active_profile(alias)`. `cache.py` reads `CONFIG_PATH.parent` at call
time (see cache.py docstring), so flipping `CONFIG_PATH` automatically
redirects the cache without any other plumbing.

Pre-migration (legacy) installs keep `CONFIG_PATH` at the flat
`~/.config/owa-piggy/config`. `migration.migrate_if_needed()` moves that
into `profiles/default/` the first time a profile-aware code path runs.
"""
import os
import re
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path.home() / '.config' / 'owa-piggy'
CONFIG_PATH = ROOT_DIR / 'config'

# Aliases land directly in filesystem paths under profiles/<alias>/ and in
# the OWA_PROFILES list in profiles.conf. Anything permissive lets a caller
# escape the config tree (e.g. --profile ../../outside) or create nested
# directories that list_profiles() cannot round-trip back to the original
# alias (e.g. --profile work/sub). Keep the character set conservative.
_ALIAS_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def validate_alias(alias):
    """Return (ok, err) for a profile alias.

    Accepts only `[A-Za-z0-9._-]+` and rejects '.' / '..' outright so no
    alias can resolve to the current or parent directory. Callers must
    run this before using an alias for path selection or registration.
    """
    if not isinstance(alias, str) or not alias:
        return False, 'profile alias must be a non-empty string'
    if alias in ('.', '..'):
        return False, f'profile alias {alias!r} is reserved'
    if not _ALIAS_RE.match(alias):
        return False, (
            f'invalid profile alias {alias!r}: allowed characters are '
            f'letters, digits, dot, underscore, hyphen'
        )
    return True, ''


def iso_utc_now():
    """UTC ISO8601 with trailing Z. Used to stamp OWA_RT_ISSUED_AT on fresh
    setup/reseed so --status can compute the 24h SPA hard-cap."""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# --- Profile paths -----------------------------------------------------
# Functions (not constants) because ROOT_DIR is mutable; deriving at call
# time means tests that monkeypatch ROOT_DIR get the right paths without
# having to re-patch every downstream constant.

def profiles_dir():
    """Root of the per-profile directories."""
    return ROOT_DIR / 'profiles'


def profiles_conf_path():
    """Path to the profile-registry file."""
    return ROOT_DIR / 'profiles.conf'


def profile_dir(alias):
    """Directory for a single profile. May not exist yet."""
    return profiles_dir() / alias


def profile_config_path(alias):
    """Path to a specific profile's config file."""
    return profile_dir(alias) / 'config'


def profile_edge_dir(alias):
    """Path to a specific profile's Edge sidecar userdata dir."""
    return profile_dir(alias) / 'edge-profile'


def profile_log_path(alias):
    """Path to a specific profile's launchd stderr log."""
    return profile_dir(alias) / 'refresh.log'


def set_active_profile(alias):
    """Rebind CONFIG_PATH to point at `profiles/<alias>/config`.

    No validation - callers who need to guarantee the profile already
    exists should use `resolve_profile()` first. No directory is created;
    the first `save_config()` will mkdir as needed.
    """
    global CONFIG_PATH
    CONFIG_PATH = profile_config_path(alias)
    return CONFIG_PATH


def list_profiles():
    """Sorted list of profile aliases present on disk."""
    d = profiles_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


# --- Profile registry (profiles.conf) ---------------------------------
# Same flat KV format as the per-profile config files, but a separate
# file so the registry never gets confused with a profile's own config.

def load_profiles_conf():
    """Read profiles.conf into {'OWA_DEFAULT_PROFILE': str, 'OWA_PROFILES': list[str]}.

    Missing file returns empty defaults. Unknown keys are ignored so a
    future addition can be forward-compatible with older binaries.
    """
    out = {'OWA_DEFAULT_PROFILE': '', 'OWA_PROFILES': []}
    path = profiles_conf_path()
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == 'OWA_DEFAULT_PROFILE':
            out['OWA_DEFAULT_PROFILE'] = v
        elif k == 'OWA_PROFILES':
            out['OWA_PROFILES'] = [p for p in v.split() if p]
    return out


def save_profiles_conf(data):
    """Atomically write profiles.conf.

    `data` mirrors `load_profiles_conf()`: OWA_DEFAULT_PROFILE is a str,
    OWA_PROFILES is an iterable of alias strings. Aliases are joined
    space-separated on disk to keep parsing trivial.
    """
    path = profiles_conf_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    default = data.get('OWA_DEFAULT_PROFILE', '') or ''
    profiles = data.get('OWA_PROFILES', []) or []
    if isinstance(profiles, str):
        profiles = profiles.split()
    # De-duplicate while preserving first-seen order so the file reads
    # the same way it was written.
    seen = set()
    uniq = []
    for p in profiles:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    lines = [
        f'OWA_DEFAULT_PROFILE="{default}"',
        f'OWA_PROFILES="{" ".join(uniq)}"',
    ]
    payload = '\n'.join(lines) + '\n'

    fd, tmp_path = tempfile.mkstemp(
        prefix='.profiles.', suffix='.tmp', dir=str(path.parent)
    )
    tmp = Path(tmp_path)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def ensure_profile_registered(alias, make_default_if_first=True):
    """Add `alias` to OWA_PROFILES (no-op if already present). If no
    default is set yet and `make_default_if_first` is True, mark this
    alias as the default. Returns the updated registry dict.
    """
    ok, verr = validate_alias(alias)
    if not ok:
        raise ValueError(verr)
    data = load_profiles_conf()
    if alias not in data['OWA_PROFILES']:
        data['OWA_PROFILES'].append(alias)
    if make_default_if_first and not data['OWA_DEFAULT_PROFILE']:
        data['OWA_DEFAULT_PROFILE'] = alias
    save_profiles_conf(data)
    return data


def unregister_profile(alias):
    """Remove `alias` from OWA_PROFILES. If it was the default, clear
    the default pointer (caller decides what to promote next, if any).
    """
    data = load_profiles_conf()
    data['OWA_PROFILES'] = [p for p in data['OWA_PROFILES'] if p != alias]
    if data['OWA_DEFAULT_PROFILE'] == alias:
        data['OWA_DEFAULT_PROFILE'] = ''
    save_profiles_conf(data)
    return data


def resolve_profile(cli_profile=None, allow_missing=False):
    """Pick which profile this invocation should target.

    Precedence (highest wins):
      1. `cli_profile`   - the value of an explicit `--profile <alias>` flag.
      2. OWA_PROFILE env - lets shells scope an invocation without touching args.
      3. profiles.conf's OWA_DEFAULT_PROFILE pointer.
      4. If exactly one profile exists on disk, use it.
      5. Fresh install (no profiles at all) - return 'default' and let the
         first --setup create it.
      6. Multiple profiles exist but none is marked default and none was
         requested - return an ambiguity error so we never silently pick
         the wrong tenant.

    Returns `(alias, err)`. On success err is ''. On ambiguity alias is ''
    and err is a human-readable message listing available aliases.

    `allow_missing=True` skips the "profile must already exist" validation
    for step 1 (used by --setup, which is the path that creates it).
    """
    available = list_profiles()

    # 1. Explicit CLI flag.
    if cli_profile:
        ok, verr = validate_alias(cli_profile)
        if not ok:
            return '', verr
        if allow_missing or cli_profile in available or not available:
            return cli_profile, ''
        return '', (
            f'profile {cli_profile!r} not found. Available: '
            f'{", ".join(available) if available else "(none)"}. '
            f'Create it with: owa-piggy --setup --profile {cli_profile}'
        )

    # 2. Env var.
    env_profile = os.environ.get('OWA_PROFILE', '').strip()
    if env_profile:
        ok, verr = validate_alias(env_profile)
        if not ok:
            return '', f'OWA_PROFILE: {verr}'
        if allow_missing or env_profile in available or not available:
            return env_profile, ''
        return '', (
            f'OWA_PROFILE={env_profile!r} not found. Available: '
            f'{", ".join(available) if available else "(none)"}'
        )

    # 3. Registry default pointer.
    reg = load_profiles_conf()
    default = reg['OWA_DEFAULT_PROFILE']
    if default and (default in available or allow_missing):
        return default, ''

    # 4. Single profile on disk.
    if len(available) == 1:
        return available[0], ''

    # 5. Fresh install.
    if not available:
        return 'default', ''

    # 6. Ambiguity.
    return '', (
        'multiple profiles configured and no default set. '
        f'Available: {", ".join(available)}. '
        'Pass --profile <alias>, export OWA_PROFILE=<alias>, or run '
        'owa-piggy --set-default <alias>.'
    )


# --- Main config I/O --------------------------------------------------

def parse_kv_stream(text):
    """Parse KEY=value lines. Only recognises known OWA_* keys to avoid
    writing arbitrary junk to the config file."""
    allowed = {'OWA_REFRESH_TOKEN', 'OWA_TENANT_ID', 'OWA_CLIENT_ID'}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k in allowed and v:
            out[k] = v
    return out


def load_config():
    """Returns (config, persist). persist is True only when the *effective*
    OWA_REFRESH_TOKEN came from the on-disk config - i.e. the file has the
    key AND no environment override is shadowing it. When env overrides,
    the env value is what we send to AAD and the rotated token belongs to
    that env-driven session; writing it back would silently clobber the
    unrelated file token."""
    config = {}
    file_keys = set()
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                k = k.strip()
                config[k] = v.strip().strip('"')
                file_keys.add(k)
    # Environment overrides file
    for key in ('OWA_REFRESH_TOKEN', 'OWA_TENANT_ID', 'OWA_CLIENT_ID'):
        if key in os.environ:
            config[key] = os.environ[key]
    persist = (
        'OWA_REFRESH_TOKEN' in file_keys
        and 'OWA_REFRESH_TOKEN' not in os.environ
    )
    return config, persist


def save_config(config):
    """Atomically rewrite the config file.

    Refresh tokens rotate on every successful exchange, so a partial write here
    would corrupt the only live token and force the user to reseed from the
    browser. Write the new contents to a sibling temp file, fsync, chmod, then
    rename over the target - rename within a filesystem is atomic on POSIX, so
    either the old or the new file is visible, never a truncated mix.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = []
    if CONFIG_PATH.exists():
        # Preserve existing lines, update known keys
        existing_keys = set()
        for line in CONFIG_PATH.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith('#') and '=' in stripped:
                k = stripped.split('=', 1)[0].strip()
                if k in config:
                    lines.append(f'{k}="{config[k]}"')
                    existing_keys.add(k)
                    continue
            lines.append(line)
        for k, v in config.items():
            if k not in existing_keys:
                lines.append(f'{k}="{v}"')
    else:
        for k, v in config.items():
            lines.append(f'{k}="{v}"')
    payload = '\n'.join(lines) + '\n'

    fd, tmp_path = tempfile.mkstemp(
        prefix='.config.', suffix='.tmp', dir=str(CONFIG_PATH.parent)
    )
    tmp = Path(tmp_path)
    try:
        os.chmod(tmp, 0o600)  # apply perms before the file holds any secret
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
