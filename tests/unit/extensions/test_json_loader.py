"""Hostile-input corpus for the strict package JSON parser.

Every case here is something ``json.loads`` accepts and Orb must not. The
parser is the first thing an untrusted package touches, so "the default parser
was fine" is not an argument that survives contact with a package that was
built to exploit exactly the defaults.
"""

from __future__ import annotations

import pytest

from backend.features.extensions.errors import PackageLimitExceeded, PackageParseError
from backend.features.extensions.json_loader import decode_text, load_json
from backend.features.extensions.limits import (
    MAX_JSON_DEPTH,
    MAX_JSON_MEMBERS,
    MAX_JSON_STRING_BYTES,
)


def _load(raw: bytes | str, max_bytes: int = 1 << 20):
    return load_json(raw, what="test.json", max_bytes=max_bytes)


# ── things the stdlib parser accepts ─────────────────────────────────────────


def test_duplicate_object_keys_are_rejected():
    # json.loads silently keeps the last value. Two readers of these bytes could
    # legitimately disagree about the meaning, so the digest would stop pinning
    # one -- which is the whole basis of "this is the package you approved".
    with pytest.raises(PackageParseError, match="duplicate object key"):
        _load(b'{"a": 1, "a": 2}')


def test_duplicate_keys_rejected_when_nested():
    with pytest.raises(PackageParseError, match="duplicate object key"):
        _load(b'{"outer": {"b": 1, "b": 2}}')


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_literals_are_rejected(literal):
    with pytest.raises(PackageParseError, match="non-finite"):
        _load(b'{"x": ' + literal + b"}")


def test_float_overflow_to_infinity_is_rejected():
    # The hole parse_constant does not cover: 1e400 never reaches it, it just
    # overflows to inf inside parse_float.
    with pytest.raises(PackageParseError, match="non-finite"):
        _load(b'{"x": 1e400}')


def test_negative_float_overflow_is_rejected():
    with pytest.raises(PackageParseError, match="non-finite"):
        _load(b'{"x": -1e400}')


def test_oversized_integer_is_rejected():
    with pytest.raises(PackageParseError, match="64-bit"):
        _load(b'{"x": 99999999999999999999999999}')


def test_invalid_utf8_is_rejected():
    with pytest.raises(PackageParseError, match="not valid UTF-8"):
        _load(b'{"x": "\xff\xfe"}')


@pytest.mark.parametrize("raw", [b'{"x": "\\ud800"}', '{"x": "\udfff"}'])
def test_lone_unicode_surrogates_are_rejected_cleanly(raw):
    with pytest.raises(PackageParseError, match="invalid Unicode"):
        _load(raw)


def test_utf8_bom_is_rejected():
    # A BOM makes two byte-different files carry the same parsed value, which
    # would let one digest describe two contents.
    with pytest.raises(PackageParseError, match="BOM"):
        _load(b'\xef\xbb\xbf{"x": 1}')


def test_trailing_data_is_rejected():
    with pytest.raises(PackageParseError, match="invalid JSON"):
        _load(b'{"x": 1} {"y": 2}')


# ── limits ───────────────────────────────────────────────────────────────────


def test_nesting_bomb_is_rejected_before_construction():
    depth = MAX_JSON_DEPTH + 5
    with pytest.raises(PackageLimitExceeded, match="depth limit"):
        _load(b"[" * depth + b"]" * depth)


def test_deep_nesting_beyond_python_recursion_is_still_a_clean_rejection():
    # The pre-scan runs before json.loads, so this never reaches the recursive
    # scanner and never surfaces as a RecursionError to the caller.
    with pytest.raises(PackageLimitExceeded):
        _load(b"[" * 20000 + b"]" * 20000)


def test_nesting_at_the_limit_is_accepted():
    depth = MAX_JSON_DEPTH
    assert _load(b"[" * depth + b"]" * depth) is not None


def test_brackets_inside_strings_do_not_count_toward_depth():
    brackets = "[" * (MAX_JSON_DEPTH * 4)
    assert _load(f'{{"x": "{brackets}"}}'.encode()) == {"x": brackets}


def test_escaped_quote_does_not_end_the_string_scan():
    assert _load(rb'{"x": "a\"[[[["}') == {"x": 'a"[[[['}


def test_too_many_object_members_is_rejected():
    payload = "{" + ",".join(f'"k{i}": 1' for i in range(MAX_JSON_MEMBERS + 1)) + "}"
    with pytest.raises(PackageLimitExceeded, match="members"):
        _load(payload.encode())


def test_too_many_array_members_is_rejected():
    payload = "[" + ",".join("1" for _ in range(MAX_JSON_MEMBERS + 1)) + "]"
    with pytest.raises(PackageLimitExceeded, match="members"):
        _load(payload.encode())


def test_oversized_string_value_is_rejected():
    payload = '{"x": "' + "a" * (MAX_JSON_STRING_BYTES + 1) + '"}'
    with pytest.raises(PackageLimitExceeded, match="string value"):
        _load(payload.encode())


def test_string_size_is_measured_in_utf8_bytes_not_characters():
    # A 3-byte character must count as 3. Measuring in characters would let a
    # package ship three times the byte budget it was allowed.
    char_count = (MAX_JSON_STRING_BYTES // 3) + 1
    payload = '{"x": "' + ("一" * char_count) + '"}'
    with pytest.raises(PackageLimitExceeded, match="string value"):
        _load(payload.encode())


def test_document_size_limit_is_enforced_on_bytes():
    with pytest.raises(PackageLimitExceeded, match="exceeds the limit"):
        _load(b'{"x": "' + b"a" * 100 + b'"}', max_bytes=32)


def test_document_size_limit_is_enforced_on_str_input():
    with pytest.raises(PackageLimitExceeded, match="exceeds the limit"):
        _load('{"x": "' + "a" * 100 + '"}', max_bytes=32)


def test_error_messages_name_the_document():
    with pytest.raises(PackageParseError, match=r"^test\.json:"):
        _load(b"{")


# ── the happy path still works ───────────────────────────────────────────────


def test_ordinary_document_parses():
    assert _load(b'{"extension_api": 1, "id": "scene-meter", "tags": ["a", 2, true, null]}') == {
        "extension_api": 1,
        "id": "scene-meter",
        "tags": ["a", 2, True, None],
    }


def test_decode_text_returns_the_decoded_string():
    assert decode_text(b'{"a": 1}', what="t.json", max_bytes=64) == '{"a": 1}'
