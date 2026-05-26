"""Shared live-exchange plumbing for token, status, and debug.

The three command paths used to each carry their own copy of:
- extract OWA_REFRESH_TOKEN / OWA_TENANT_ID / OWA_CLIENT_ID from config
- FOCI shape check on the refresh token
- call exchange_token, optionally capturing stderr to keep stdout clean
- detect the AAD codes that auto-reseed can recover (AADSTS70043 /
  AADSTS700084)
- persist a rotated refresh token back to the per-profile config when it
  came from disk in the first place

Drift between those copies is the maintainability risk flagged in the
GPT cohesion review. Centralising the live-exchange step here means each
command keeps only its own cache / output-formatting concerns.

This module does NOT touch the access-token cache, do not call out to
reseed, and does not print rotation NOTEs - those are caller policy.
"""
import io
import sys

from .config import save_config
from .oauth import CLIENT_ID, exchange_token

# AAD error codes the caller can recover from by triggering an automatic
# reseed (sliding-window expiry, hard-cap expiry). Detected from
# captured stderr so a structured return value is available without
# changing exchange_token's signature.
_RECOVERABLE_AAD_CODES = ('AADSTS70043', 'AADSTS700084')


def exchange_fresh(config, scope, *, persist, capture_stderr=False):
    """Live AAD exchange against the profile in `config` for `scope`.

    Returns ``(result, info)``:

    - ``result``: the dict returned by ``exchange_token`` (with
      ``access_token`` / ``refresh_token`` / ``expires_in`` / ``scope``),
      or ``None`` if any precondition failed (missing RT/TID, non-FOCI
      RT shape, or AAD rejected the exchange).
    - ``info``: a dict with the resolved fields and post-exchange state:
        ``rt``, ``tid``, ``cid`` - stripped values pulled from config
            (``cid`` defaults to ``oauth.CLIENT_ID`` when unset)
        ``rt_present``, ``tid_present`` - presence flags
        ``rt_shape_ok`` - True iff RT looks like a FOCI token
            (``1.`` or ``0.`` prefix)
        ``stderr_text`` - captured stderr from ``exchange_token``
            (empty when ``capture_stderr=False`` or no error path)
        ``aad_error`` - one of ``AADSTS70043`` / ``AADSTS700084`` when
            detected in captured stderr, else ``None``
        ``rotated`` - True iff a new refresh token was written back to
            ``config`` (and to disk when ``persist`` is True)

    Side effect: when the response carries a rotated refresh token and
    ``persist`` is True, ``config['OWA_REFRESH_TOKEN']`` is updated and
    ``save_config(config)`` is called. The config dict is mutated in
    place either way so the caller's subsequent reads see the new
    token.
    """
    rt = config.get('OWA_REFRESH_TOKEN', '').strip()
    tid = config.get('OWA_TENANT_ID', '').strip()
    cid = config.get('OWA_CLIENT_ID', CLIENT_ID).strip()
    origin = config.get('OWA_ORIGIN', '').strip() or None
    # Only forward an explicit OWA_ORIGIN override. When unset, let
    # exchange_token pick the per-client default origin — and keep the
    # call 4-positional so existing callers / test mocks are unaffected.
    origin_kw = {'origin': origin} if origin else {}
    info = {
        'rt': rt,
        'tid': tid,
        'cid': cid,
        'rt_present': bool(rt),
        'tid_present': bool(tid),
        'rt_shape_ok': bool(rt) and (rt.startswith('1.') or rt.startswith('0.')),
        'stderr_text': '',
        'aad_error': None,
        'rotated': False,
    }
    if not info['rt_present'] or not info['tid_present'] or not info['rt_shape_ok']:
        return None, info

    if capture_stderr:
        captured = io.StringIO()
        stderr_fd = sys.stderr
        try:
            sys.stderr = captured
            result = exchange_token(rt, tid, cid, scope, **origin_kw)
        finally:
            sys.stderr = stderr_fd
        info['stderr_text'] = captured.getvalue()
        # Note: the helper does NOT replay captured stderr. The cli
        # mint path wants the AAD error to reach the terminal verbatim
        # (callers grep for it); status/debug surface their own hint
        # lines from info['stderr_text'] and would double-print if we
        # echoed here. Replay is one line - leave it to the caller.
    else:
        result = exchange_token(rt, tid, cid, scope, **origin_kw)

    if not result:
        for code in _RECOVERABLE_AAD_CODES:
            if code in info['stderr_text']:
                info['aad_error'] = code
                break
        return None, info

    new_rt = result.get('refresh_token')
    if new_rt and new_rt != rt:
        config['OWA_REFRESH_TOKEN'] = new_rt
        info['rotated'] = True
        if persist:
            save_config(config)
    return result, info
