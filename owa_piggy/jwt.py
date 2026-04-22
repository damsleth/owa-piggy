"""JWT decode helpers.

owa-piggy never validates token signatures - it only parses the claims
locally for the `decode`/`status`/`remaining` subcommands. These are stdlib base64 +
JSON, nothing more.
"""
import base64
import json
import time


def decode_jwt_segment(segment):
    """Decode one base64url segment to a dict. Accepts unpadded input."""
    segment += '=' * ((4 - len(segment) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


def token_minutes_remaining(access_token):
    """Minutes until the token's `exp` claim. Returns None if the token is
    malformed or missing `exp`. Past `exp` returns a non-positive integer."""
    try:
        payload = decode_jwt_segment(access_token.split('.')[1])
        return int((payload.get('exp', 0) - time.time()) / 60)
    except Exception:
        return None


def decode_jwt(access_token):
    """Pretty-print Header + Payload as JSON. Signature segment is ignored."""
    import sys
    parts = access_token.split('.')
    lines = []
    for i, label in enumerate(['Header', 'Payload']):
        if i >= len(parts):
            break
        try:
            decoded = decode_jwt_segment(parts[i])
            lines.append(f'=== {label} ===')
            lines.append(json.dumps(decoded, indent=2))
        except Exception as e:
            print(f'Error decoding {label}: {e}', file=sys.stderr)
    return '\n'.join(lines)
