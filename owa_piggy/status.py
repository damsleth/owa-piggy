"""--status (compact ISO8601 health summary) and --debug (full diagnostics).

Both do a live exchange probe against AAD, which rotates the refresh token
as a side effect. That's fine - a normal invocation would do the same.

Both take an `alias` parameter so output can be labeled with the active
profile and the launchd plist we probe is the right one for that profile.
"""
import io
import os
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as _config
from .config import (
    list_profiles,
    load_config,
    load_profiles_conf,
    profile_edge_dir,
    save_config,
)
from .jwt import decode_jwt_segment
from .oauth import CLIENT_ID, exchange_token
from .reseed import find_reseed_script
from .scopes import KNOWN_SCOPES, resolve_scope

LAUNCHD_LABEL_PREFIX = 'com.damsleth.owa-piggy'


def _launchd_label(alias):
    """Per-profile plist label used by `setup-refresh.sh --profile <alias>`."""
    return f'{LAUNCHD_LABEL_PREFIX}.{alias}'


def do_status(alias, multi=False):
    """Compact health summary for profile <alias>. Does a live exchange
    probe to verify the RT actually works (rotates it as a side effect,
    which is fine - the RT rotates on every use anyway). Prints three
    ISO8601 lines or the single line 'no valid token' if anything is
    missing or the probe fails.

    Refresh-token expiry uses OWA_RT_ISSUED_AT + 24h if it's in the config
    (set by --setup and --reseed). That's the SPA hard-cap, which is the
    binding constraint since hourly rotation keeps the sliding window
    permanently fresh. If the field is missing (pre-existing setups from
    before this flag landed) we fall back to 'unknown'.

    `multi=True` is set by do_status_all() when iterating every profile.
    In that mode the [profile=...] label is written to stdout so the
    output is self-describing when scanning several profiles at once;
    single-profile mode keeps the label on stderr to preserve the
    script-friendly stdout contract."""
    # set_active_profile rebinds CONFIG_PATH so load_config / save_config
    # and the access-token cache all target the right profile. The
    # single-profile path goes through cli.py which already calls this,
    # but do_status_all() calls us directly per alias so we own it here.
    _config.set_active_profile(alias)
    config, persist = load_config()
    rt = config.get('OWA_REFRESH_TOKEN', '').strip()
    tid = config.get('OWA_TENANT_ID', '').strip()
    cid = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()

    label_stream = sys.stdout if multi else sys.stderr
    print(f'[profile={alias}]', file=label_stream)
    if not rt or not tid or not (rt.startswith('1.') or rt.startswith('0.')):
        print('no valid token')
        return 1

    # Resolve scope BEFORE capturing stderr so any OWA_DEFAULT_AUDIENCE
    # misconfiguration warning still reaches the user. --status honors the
    # same flags as the main path, so `owa-piggy --outlook --status`
    # probes the outlook audience.
    scope, err = resolve_scope(sys.argv[1:])
    if err:
        print(f'ERROR: {err}', file=sys.stderr)
        return 1
    # Silence exchange_token's own stderr prints for --status; we surface a
    # single-line status on failure instead of a multi-line AAD dump.
    stderr_fd = sys.stderr
    try:
        sys.stderr = io.StringIO()
        result = exchange_token(rt, tid, cid, scope)
    finally:
        sys.stderr = stderr_fd

    if not result or not result.get('access_token'):
        print('no valid token')
        return 1

    at = result['access_token']
    try:
        payload = decode_jwt_segment(at.split('.')[1])
    except Exception:
        print('no valid token')
        return 1

    exp_ts = int(payload.get('exp', 0))
    scp = payload.get('scp') or payload.get('roles') or ''
    # aud can be a string (v1) or an array (v2 spec allows it). Normalise.
    raw_aud = payload.get('aud', '')
    aud = raw_aud[0] if isinstance(raw_aud, list) and raw_aud else raw_aud
    aud = aud if isinstance(aud, str) else str(aud)

    # Map the aud claim back to a KNOWN_SCOPES short name (reverse lookup).
    # Graph uses a GUID audience in some flows, so accept either the URL or
    # the well-known Graph GUID.
    aud_name = None
    if aud == '00000003-0000-0000-c000-000000000000':
        aud_name = 'graph'
    else:
        for name, entry in KNOWN_SCOPES.items():
            url = entry[0]
            if aud == url or aud.rstrip('/') == url.rstrip('/'):
                aud_name = name
                break
    audience_line = f'{aud_name} ({aud})' if aud_name else (aud or 'unknown')

    # Persist rotated RT (matches main-flow behavior).
    new_rt = result.get('refresh_token')
    if new_rt and new_rt != rt and persist:
        config['OWA_REFRESH_TOKEN'] = new_rt
        save_config(config)

    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Refresh token hard-cap: issued_at + 24h. Parse OWA_RT_ISSUED_AT if set.
    rt_expires = 'unknown (run `owa-piggy --reseed` to establish)'
    issued_at = config.get('OWA_RT_ISSUED_AT', '').strip()
    if issued_at:
        try:
            dt = datetime.strptime(issued_at, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            rt_expires = (dt + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            pass

    if isinstance(scp, str):
        parts = scp.split()
        scopes_line = (f'{", ".join(parts[:3])}, ... ({len(parts)} scopes)'
                       if len(parts) > 3 else ', '.join(parts))
    else:
        scopes_line = str(scp)

    print(f'authtoken:    expires {iso(exp_ts)}')
    print(f'audience:     {audience_line}')
    print(f'scope(s):     {scopes_line}')
    print(f'refreshtoken: expires {rt_expires}')
    return 0


def do_status_all():
    """Run do_status() against every configured profile.

    Used when `--status` is invoked with no explicit --profile / no
    OWA_PROFILE env var. Each profile gets its own labeled block,
    separated by a blank line. Exit code is the max of the per-profile
    return codes so any unhealthy profile is still surfaced to scripts.
    """
    profiles = list_profiles()
    if not profiles:
        print('no profiles configured. Run: owa-piggy --setup --profile <alias>',
              file=sys.stderr)
        return 1
    rc = 0
    for i, alias in enumerate(profiles):
        if i:
            print()
        rc = max(rc, do_status(alias, multi=True))
    return rc


def do_debug(alias):
    """Dump everything useful to diagnose a broken setup for profile <alias>:
    config file, refresh-token shape, live exchange probe, access-token
    claims, launchd agent status, PATH install, sidecar profile. Also
    lists all configured profiles for context. Read-mostly: the probe
    exchange does rotate the refresh token as a side effect (same as a
    normal invocation), because that's the only honest way to prove the
    token is currently valid."""

    # Resolve scope up front and bail on argument errors, matching the
    # rest of the CLI. Previously --debug silently ignored resolve_scope's
    # error and probed with a None scope, which masked what was actually
    # a fatal arg error with misleading AAD output.
    debug_scope, scope_err = resolve_scope(sys.argv[1:])
    if scope_err:
        print(f'ERROR: {scope_err}', file=sys.stderr)
        return 1

    def row(status, label, detail=''):
        print(f'  [{status}] {label}' + (f': {detail}' if detail else ''))

    print(f'owa-piggy --debug [profile={alias}]\n')

    # --- Profile registry ---
    reg = load_profiles_conf()
    profiles = list_profiles()
    print('Profiles:')
    if profiles:
        for p in profiles:
            marker = '*' if p == reg['OWA_DEFAULT_PROFILE'] else ' '
            active = '  (active)' if p == alias else ''
            print(f'  {marker} {p}{active}')
    else:
        row('no', 'no profiles registered')

    # --- Config file ---
    cfg_path = _config.CONFIG_PATH
    print(f'\nConfig file ({cfg_path}):')
    if cfg_path.exists():
        st = cfg_path.stat()
        mode = oct(st.st_mode & 0o777)
        age_min = int((_time.time() - st.st_mtime) / 60)
        row('ok', 'present', f'perms {mode}, modified {age_min}min ago')
    else:
        row('no', 'missing')

    config, persist = load_config()
    rt = config.get('OWA_REFRESH_TOKEN', '').strip()
    tid = config.get('OWA_TENANT_ID', '').strip()
    cid = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()
    source = 'config file' if persist else ('env only' if rt else '')
    row('ok' if rt else 'no', 'OWA_REFRESH_TOKEN',
        f'{len(rt)} bytes, {source}' if rt else 'unset')
    row('ok' if tid else 'no', 'OWA_TENANT_ID', tid or 'unset')
    row('..', 'OWA_CLIENT_ID',
        f'{cid}{" (default OWA first-party)" if cid == CLIENT_ID else " (override)"}')

    # --- Refresh token shape + live probe ---
    print('\nRefresh token:')
    if not rt:
        row('no', f'absent; run `owa-piggy --setup --profile {alias}` or '
                  f'`owa-piggy --reseed --profile {alias}`')
    else:
        shape_ok = rt.startswith('1.') or rt.startswith('0.')
        row('ok' if shape_ok else 'no',
            f'FOCI shape ({rt[:2]}...)' if shape_ok else
            f'NOT FOCI (starts {rt[:4]!r}); AAD will reject as malformed')

        if shape_ok and tid:
            print('  probing live exchange against AAD...')
            result = exchange_token(rt, tid, cid, debug_scope)
            if result and result.get('access_token'):
                row('ok', 'exchange succeeded')
                at = result['access_token']
                try:
                    payload = decode_jwt_segment(at.split('.')[1])
                    aud = payload.get('aud', '?')
                    scp = payload.get('scp', payload.get('roles', '?'))
                    exp = payload.get('exp', 0)
                    iat = payload.get('iat', 0)
                    now = _time.time()
                    row('..', 'access token aud', str(aud))
                    if isinstance(scp, str) and len(scp) > 80:
                        # OWA scopes are legion (~100 space-separated entries).
                        # Show count and the first few so --debug stays useful.
                        parts = scp.split()
                        preview = ', '.join(parts[:3])
                        row('..', 'access token scp',
                            f'{len(parts)} scopes ({preview}, ...)')
                    else:
                        row('..', 'access token scp', str(scp))
                    row('..', 'access token exp',
                        f'in {int((exp-now)/60)} min ({_time.strftime("%H:%M:%S", _time.localtime(exp))})')
                    row('..', 'access token iat',
                        f'{int((now-iat)/60)} min ago')
                except Exception as e:
                    row('no', 'access token decode failed', str(e))

                new_rt = result.get('refresh_token', '')
                if new_rt and new_rt != rt:
                    if persist:
                        config['OWA_REFRESH_TOKEN'] = new_rt
                        save_config(config)
                        row('ok', 'refresh token rotated and persisted')
                    else:
                        row('..', 'refresh token rotated (env-only, not persisted)')
            else:
                row('no', 'exchange failed - see error above')

    # --- Launchd agent ---
    label = _launchd_label(alias)
    print(f'\nLaunchd refresh agent ({label}):')
    plist_path = Path.home() / 'Library' / 'LaunchAgents' / f'{label}.plist'
    row('ok' if plist_path.exists() else 'no', 'plist file', str(plist_path))

    uid = os.getuid()
    target = f'gui/{uid}/{label}'
    try:
        proc = subprocess.run(['launchctl', 'print', target],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            row('ok', 'bootstrapped', target)
            # Surface the handful of fields that actually tell you if it's
            # healthy: runs, last exit, state, pid. launchctl print is a
            # nested tree so `state` appears multiple times - take only the
            # first occurrence of each wanted key (the top-level scope).
            wanted = ('state', 'runs', 'last exit code', 'last exit reason', 'pid')
            seen = set()
            for line in proc.stdout.splitlines():
                s = line.strip()
                for w in wanted:
                    if w in seen:
                        continue
                    if s.startswith(w + ' =') or s.startswith(w + ':'):
                        print(f'      {s}')
                        seen.add(w)
                        break
        else:
            row('no', 'not loaded',
                (proc.stderr.strip().splitlines() or [''])[0][:120])
    except FileNotFoundError:
        row('..', 'launchctl not found on PATH (non-macOS?)')
    except subprocess.TimeoutExpired:
        row('no', 'launchctl print timed out')

    # Warn if the legacy suffix-less plist is still around; setup-refresh.sh
    # cleans it up on first --all run, but --debug should surface it for
    # anyone who hasn't re-run install yet.
    legacy_label = LAUNCHD_LABEL_PREFIX
    legacy_plist = Path.home() / 'Library' / 'LaunchAgents' / f'{legacy_label}.plist'
    if legacy_plist.exists():
        row('!!', 'legacy single-profile plist still present',
            f'{legacy_plist} - run scripts/setup-refresh.sh --all to replace')

    try:
        proc = subprocess.run(['crontab', '-l'], capture_output=True,
                              text=True, timeout=5)
        if 'owa-piggy' in proc.stdout:
            row('!!', 'legacy cron entry still present',
                'run ./scripts/setup-refresh.sh to migrate')
    except Exception:
        pass

    # --- Installation / PATH ---
    print('\nInstallation:')
    import shutil
    installed = shutil.which('owa-piggy')
    if installed:
        p = Path(installed)
        detail = str(p)
        if p.is_symlink():
            detail += f' -> {os.readlink(p)}'
        row('ok', 'owa-piggy on PATH', detail)
    else:
        row('no', 'owa-piggy not on PATH',
            'run ./scripts/add-to-path.sh or pipx install .')

    sidecar = profile_edge_dir(alias)
    row('ok' if sidecar.is_dir() else 'no',
        'Edge sidecar profile', str(sidecar) if sidecar.is_dir() else
        f'{sidecar} (missing; --reseed needs this)')

    reseed = find_reseed_script()
    row('ok' if reseed else 'no', 'reseed script',
        str(reseed) if reseed else
        'not found in any standard location (OWA_RESEED_SCRIPT overrides)')

    return 0
