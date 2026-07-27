"""Content digest and package-path rules.

The digest is what "the package you approved" means: an install consents to a
digest, and everything afterwards -- activation, startup reconciliation,
update, rollback, artifact recovery -- compares against it. So the properties
worth pinning are the ones that would let two different contents share a
digest, or one content produce two.
"""

from __future__ import annotations

import pytest

from backend.features.extensions.digest import (
    DIGEST_DOMAIN,
    PackageContent,
    canonical_json_bytes,
    content_digest,
)
from backend.features.extensions.errors import PackageValidationError
from backend.features.extensions.limits import MAX_PATH_BYTES
from backend.features.extensions.paths import (
    assert_no_case_collisions,
    normalize_package_path,
)

# ── canonical encoding ───────────────────────────────────────────────────────


def test_key_order_does_not_change_the_encoding():
    assert canonical_json_bytes({"b": 1, "a": 2}) == canonical_json_bytes({"a": 2, "b": 1})


def test_non_ascii_is_emitted_literally_not_escaped():
    assert canonical_json_bytes({"k": "é"}) == '{"k":"é"}'.encode()


def test_no_insignificant_whitespace():
    assert canonical_json_bytes({"a": [1, 2]}) == b'{"a":[1,2]}'


def test_non_finite_values_cannot_be_encoded():
    with pytest.raises(ValueError):
        canonical_json_bytes({"a": float("inf")})


# ── digest ───────────────────────────────────────────────────────────────────


def test_digest_is_stable_and_hex():
    files = {"orb-extension.json": PackageContent.json({"id": "a"})}
    first = content_digest(files)
    assert first == content_digest(files)
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)


def test_reformatting_a_json_file_does_not_change_the_digest():
    # Same meaning, different source bytes: whitespace and key order. A package
    # that only got prettier is the same package.
    a = content_digest({"f.json": PackageContent.json({"a": 1, "b": [2, 3]})})
    b = content_digest({"f.json": PackageContent.json({"b": [2, 3], "a": 1})})
    assert a == b


def test_changing_a_json_value_changes_the_digest():
    a = content_digest({"f.json": PackageContent.json({"a": 1})})
    b = content_digest({"f.json": PackageContent.json({"a": 2})})
    assert a != b


def test_binary_content_is_hashed_by_exact_bytes():
    a = content_digest({"a.webp": PackageContent.binary(b"\x00\x01")})
    b = content_digest({"a.webp": PackageContent.binary(b"\x00\x02")})
    assert a != b


def test_path_and_content_cannot_be_confused_across_the_boundary():
    # Without length-delimiting, {"a/b": "c"} and {"a": "b/c"} would concatenate
    # to the same bytes and collide.
    a = content_digest({"a/b": PackageContent.binary(b"c")})
    b = content_digest({"a": PackageContent.binary(b"b/c")})
    assert a != b


def test_renaming_a_file_changes_the_digest():
    a = content_digest({"ui/a.json": PackageContent.json({})})
    b = content_digest({"ui/b.json": PackageContent.json({})})
    assert a != b


def test_adding_an_empty_file_changes_the_digest():
    a = content_digest({"a.json": PackageContent.json({})})
    b = content_digest({"a.json": PackageContent.json({}), "b.txt": PackageContent.binary(b"")})
    assert a != b


def test_file_order_does_not_affect_the_digest():
    files_a = {"z.json": PackageContent.json({}), "a.json": PackageContent.json({})}
    files_b = {"a.json": PackageContent.json({}), "z.json": PackageContent.json({})}
    assert content_digest(files_a) == content_digest(files_b)


def test_domain_separator_is_versioned():
    assert DIGEST_DOMAIN.startswith(b"orb-extension-content-v1")


def test_case_folding_collision_is_rejected_before_hashing():
    with pytest.raises(PackageValidationError, match="case is folded"):
        content_digest(
            {
                "ui/View.json": PackageContent.json({}),
                "ui/view.json": PackageContent.json({}),
            }
        )


# ── paths ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../secrets.json",
        "ui/../../etc/passwd",
        "ui/./view.json",
        "ui//view.json",
        "ui/view.json/",
        "ui\\view.json",
        "C:/windows/system32",
        "ui/view\x00.json",
        "ui/view\n.json",
        " ui/view.json",
        "ui/ view.json",
        "",
    ],
)
def test_hostile_paths_are_rejected(bad):
    with pytest.raises(PackageValidationError):
        normalize_package_path(bad)


def test_non_string_path_is_rejected():
    with pytest.raises(PackageValidationError, match="must be a string"):
        normalize_package_path(42)


def test_overlong_path_is_rejected():
    with pytest.raises(PackageValidationError, match="UTF-8 bytes"):
        normalize_package_path("a/" + "b" * MAX_PATH_BYTES)


def test_path_length_is_measured_in_utf8_bytes():
    # 3 bytes per character: a path that is comfortably short in characters can
    # still blow a byte budget.
    with pytest.raises(PackageValidationError, match="UTF-8 bytes"):
        normalize_package_path("一" * ((MAX_PATH_BYTES // 3) + 1))


def test_normalization_is_idempotent():
    once = normalize_package_path("ui/inspector.json")
    assert normalize_package_path(once) == once


def test_unicode_is_nfc_normalized():
    # "é" as e + combining acute must land on the same key as precomposed é,
    # or the same file would have two package identities.
    assert normalize_package_path("ui/e\u0301.json") == normalize_package_path("ui/\u00e9.json")


def test_distinct_paths_pass_the_collision_check():
    assert_no_case_collisions(["ui/a.json", "ui/b.json", "flows/a.json"])


def test_repeated_identical_path_is_not_a_collision():
    assert_no_case_collisions(["ui/a.json", "ui/a.json"])
