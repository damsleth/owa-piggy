"""Fetch FOCI refresh tokens from a tailnet-side trough appliance.

trough (https://github.com/damsleth/trough) is a network capture
appliance: a Tailscale exit node that MITMs M365 traffic from devices
routed through it and stores every JWT or refresh token it sees. This
module pulls the freshest FOCI refresh token for a given tenant from a
trough's tailnet-only HTTP API and returns it in the shape
``interactive_setup`` expects.

Opt-in plumbing - imported only when the user invokes
``owa-piggy setup --from-trough <url>``. No other owa-piggy code path
imports this module, so a vanilla install pays nothing for it.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


def _http_get_json(url, *, timeout):
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')[:200]
        raise RuntimeError(f'trough HTTP {e.code} from {url}: {body}') from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'trough unreachable at {url}: {e.reason}') from e


def fetch_foci(trough_url, *, tenant=None, sub=None, timeout=10, limit=50):
    """Return ``(refresh_token, tid, info)`` for the freshest FOCI RT in the
    trough matching the filter.

    Filters are applied client-side because trough's HTTP API does not
    index by tid or sub. ``info`` carries diagnostic context (capture
    host, age, payload-reported lifetime) so the caller can print it.

    Raises ``RuntimeError`` if the trough is unreachable, returns no
    tokens, or has none that match the filter.
    """
    base = trough_url.rstrip('/')
    qs = urllib.parse.urlencode({
        'foci': 'true',
        'host': 'login.microsoftonline.com',
        'include_token': 'true',
        'limit': limit,
    })
    body = _http_get_json(f'{base}/tokens?{qs}', timeout=timeout)
    tokens = body.get('tokens') or []
    if not tokens:
        raise RuntimeError(f'no FOCI refresh tokens at {base}')

    matches = []
    for t in tokens:
        if (t.get('kind') or '') != 'refresh':
            continue
        if not t.get('token'):
            continue
        try:
            payload = json.loads(t.get('payload_json') or '{}')
        except (json.JSONDecodeError, TypeError):
            payload = {}
        t_tid = payload.get('tid') or ''
        t_sub = t.get('sub') or payload.get('sub') or ''
        if tenant and t_tid != tenant:
            continue
        if sub and t_sub != sub:
            continue
        matches.append((t, payload, t_tid, t_sub))

    if not matches:
        criteria = []
        if tenant:
            criteria.append(f'tenant={tenant}')
        if sub:
            criteria.append(f'sub={sub}')
        filt = ' '.join(criteria) if criteria else '(no filter)'
        raise RuntimeError(
            f'no FOCI refresh token in trough matched {filt}; '
            f'inspected {len(tokens)} candidate(s)'
        )

    # Trough already returns last_seen DESC, but be defensive.
    matches.sort(key=lambda m: m[0].get('last_seen') or 0, reverse=True)
    top, payload, tid, sub_oid = matches[0]
    info = {
        'tid': tid,
        'sub': sub_oid,
        'src_host': top.get('src_host'),
        'last_seen': top.get('last_seen'),
        'expires_in_at_capture': payload.get('expires_in'),
        'total_candidates': len(tokens),
        'matched': len(matches),
        'token_len': len(top['token']),
    }
    return top['token'], tid, info
