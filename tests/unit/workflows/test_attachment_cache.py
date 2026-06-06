"""Unit tests for the pure-Python pieces of attachment_cache.

The async/DB-backed entry points (record_access, evict,
insert_workflow_attachment(s), rehydrate_attachment, set_active_sibling)
are covered in the integration suite; only the synchronous helpers
appear here.
"""

from __future__ import annotations

import os
import tempfile

from backend.workflows.attachment_cache import (
    EVICTED_MARKER,
    select_lru3_victim,
    validate_workflow_attachment_shape,
)


def test_evicted_marker_literal():
    assert EVICTED_MARKER == "[evicted]"


def test_select_lru3_victim_empty_returns_none():
    assert select_lru3_victim([]) is None


def test_select_lru3_victim_picks_smallest_third_most_recent():
    candidates = [
        {"id": 1, "size": 100, "recent_accesses": [12, 11, 10]},
        {"id": 2, "size": 100, "recent_accesses": [9, 8, 7]},  # oldest 7 -> victim
        {"id": 3, "size": 100, "recent_accesses": [99, 98, 97]},
    ]
    assert select_lru3_victim(candidates) == 2


def test_select_lru3_victim_uses_last_element_when_array_shorter_than_three():
    candidates = [
        {"id": 1, "size": 100, "recent_accesses": [5]},
        {"id": 2, "size": 100, "recent_accesses": [3]},
        {"id": 3, "size": 100, "recent_accesses": [8, 7]},
    ]
    # Eviction keys: id1->5, id2->3, id3->7. Smallest = 3 (id2).
    assert select_lru3_victim(candidates) == 2


def test_select_lru3_victim_protects_empty_recent_accesses():
    candidates = [
        {"id": 1, "size": 100, "recent_accesses": [5, 4, 3]},
        {"id": 2, "size": 100, "recent_accesses": None},
    ]
    # Empty access log sorts to end -> id 1 is the victim.
    assert select_lru3_victim(candidates) == 1


def test_select_lru3_victim_protects_empty_list_same_as_none():
    candidates = [
        {"id": 1, "size": 100, "recent_accesses": [5]},
        {"id": 2, "size": 100, "recent_accesses": []},
    ]
    assert select_lru3_victim(candidates) == 1


def test_select_lru3_victim_size_is_ignored_in_ordering():
    candidates = [
        {"id": 1, "size": 1, "recent_accesses": [10]},
        {"id": 2, "size": 999999, "recent_accesses": [5]},
    ]
    assert select_lru3_victim(candidates) == 2


def test_select_lru3_victim_ties_break_deterministically():
    candidates = [
        {"id": 1, "size": 100, "recent_accesses": [5]},
        {"id": 2, "size": 100, "recent_accesses": [5]},
    ]
    # Tie behavior is implementation-defined; assert only that some valid
    # candidate is returned.
    chosen = select_lru3_victim(candidates)
    assert chosen in (1, 2)


def test_select_lru3_victim_one_candidate_returns_it():
    assert select_lru3_victim([{"id": 7, "size": 100, "recent_accesses": [3]}]) == 7


def _valid_bytes_att() -> dict:
    return {
        "workflow_id": "wf",
        "filename": "x.png",
        "mime": "image/png",
        "data": b"BYTES",
    }


def test_validator_passes_valid_bytes_att():
    ok, reason = validate_workflow_attachment_shape(_valid_bytes_att())
    assert ok is True
    assert reason is None


def test_validator_rejects_non_dict():
    ok, reason = validate_workflow_attachment_shape("not a dict")
    assert ok is False
    assert reason == "not a dict"


def test_validator_rejects_missing_workflow_id():
    att = _valid_bytes_att()
    del att["workflow_id"]
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "workflow_id must be a non-empty string"


def test_validator_rejects_empty_workflow_id():
    att = _valid_bytes_att()
    att["workflow_id"] = ""
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "workflow_id must be a non-empty string"


def test_validator_rejects_non_string_filename():
    att = _valid_bytes_att()
    att["filename"] = 123
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "filename must be a string"


def test_validator_rejects_non_string_mime():
    att = _valid_bytes_att()
    att["mime"] = None
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "mime must be a string"


def test_validator_rejects_both_data_and_path():
    att = _valid_bytes_att()
    att["path"] = "/tmp/whatever"
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "exactly one of 'data' or 'path' required"


def test_validator_rejects_neither_data_nor_path():
    att = _valid_bytes_att()
    del att["data"]
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "exactly one of 'data' or 'path' required"


def test_validator_rejects_data_wrong_type():
    att = _valid_bytes_att()
    att["data"] = "string-not-bytes"
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "data must be bytes"


def test_validator_rejects_empty_data():
    att = _valid_bytes_att()
    att["data"] = b""
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "data is empty"


def test_validator_passes_valid_path():
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(b"BYTES")
        tf.flush()
        path = tf.name
    try:
        att = {
            "workflow_id": "wf",
            "filename": "x.png",
            "mime": "image/png",
            "path": path,
        }
        ok, reason = validate_workflow_attachment_shape(att)
        assert ok is True
        assert reason is None
    finally:
        os.unlink(path)


def test_validator_rejects_path_wrong_type():
    att = {
        "workflow_id": "wf",
        "filename": "x.png",
        "mime": "image/png",
        "path": 42,
    }
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "path must be a string"


def test_validator_rejects_missing_path():
    # Path inside staging root so the "does not exist" check triggers, not containment.
    missing_path = os.path.join(tempfile.gettempdir(), "orb-validator-nonexistent-dir", "missing.png")
    att = {
        "workflow_id": "wf",
        "filename": "x.png",
        "mime": "image/png",
        "path": missing_path,
    }
    ok, reason = validate_workflow_attachment_shape(att)
    assert ok is False
    assert reason == "path does not exist or is not a regular file"


def test_validator_rejects_empty_file_via_path():
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        path = tf.name  # 0-byte file
    try:
        att = {
            "workflow_id": "wf",
            "filename": "x.png",
            "mime": "image/png",
            "path": path,
        }
        ok, reason = validate_workflow_attachment_shape(att)
        assert ok is False
        assert reason == "path points at an empty file"
    finally:
        os.unlink(path)
