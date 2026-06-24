"""Property-based tests for the two pure parsers most exposed to arbitrary
input: config `_iter_kv` quote-stripping and `jwt.decode_jwt_segment` padding.

hypothesis is dev-only (it can't install on the 3.8 CI leg), so the whole
module skips cleanly when it is absent. These are the optional property tests
from the v1-08 plan.
"""

import base64
import json

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from owa_piggy.config import _iter_kv  # noqa: E402
from owa_piggy.jwt import decode_jwt_segment  # noqa: E402

# Keys/values for the KV round-trip. Restricted to printable ASCII (codepoints
# 33..126) minus the characters _iter_kv treats specially: '=' (the separator),
# '#' (comment marker), and the quote chars it strips. Excluding codepoint 32
# (space) and all control chars sidesteps both str.strip() whitespace and the
# extra line boundaries str.splitlines() honours (e.g. \x1c-\x1f), so the
# round-trip stays well-defined.
_kv_text = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="=#\"'"),
    min_size=1,
)


@given(key=_kv_text, value=_kv_text)
def test_iter_kv_strips_matching_double_quotes(key, value):
    """A double-quoted value round-trips back to the bare value."""
    ((k, v),) = list(_iter_kv(f'{key}="{value}"'))
    assert k == key
    assert v == value


@given(key=_kv_text, value=_kv_text)
def test_iter_kv_strips_matching_single_quotes(key, value):
    """A single-quoted value round-trips back to the bare value."""
    ((k, v),) = list(_iter_kv(f"{key}='{value}'"))
    assert k == key
    assert v == value


@given(key=_kv_text, value=_kv_text)
def test_iter_kv_unquoted_value_passes_through(key, value):
    """An unquoted value is returned verbatim."""
    ((k, v),) = list(_iter_kv(f"{key}={value}"))
    assert k == key
    assert v == value


# JSON-object payloads of varying length so the base64url encoding lands on
# every possible padding remainder (0, 1, or 2 '=' chars stripped).
_json_obj = st.dictionaries(
    keys=st.text(min_size=0, max_size=8),
    values=st.one_of(
        st.integers(),
        st.booleans(),
        st.none(),
        st.text(max_size=12),
    ),
    max_size=6,
)


@given(payload=_json_obj)
def test_decode_jwt_segment_restores_stripped_padding(payload):
    """decode_jwt_segment recovers the object regardless of how many '='
    padding chars the unpadded base64url segment was missing."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    segment = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    assert decode_jwt_segment(segment) == payload
