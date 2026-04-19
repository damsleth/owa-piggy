"""Config file I/O and the small helpers that go with it.

Refresh tokens rotate on every call, so a partial write here corrupts the
only live token and forces a browser reseed. All writes go through a temp
file + fsync + rename.
"""
import os
import tempfile
import time
from pathlib import Path

CONFIG_PATH = Path.home() / '.config' / 'owa-piggy' / 'config'


def iso_utc_now():
    """UTC ISO8601 with trailing Z. Used to stamp OWA_RT_ISSUED_AT on fresh
    setup/reseed so --status can compute the 24h SPA hard-cap."""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


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
