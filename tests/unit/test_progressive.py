"""Unit tests for the director-local progressive-fragment helpers."""

from __future__ import annotations

from backend.pipeline.passes.director import progressive

# ── branch_baseline ──────────────────────────────────────────────────────────


class TestBranchBaseline:
    def test_no_grandparent_returns_empty(self):
        assert progressive.branch_baseline([]) == {}
        assert progressive.branch_baseline([{"role": "user", "content": "hi"}]) == {}

    def test_picks_last_assistant_message(self):
        history = [
            {"role": "assistant", "progressive_fields": {"tension": "calm"}},
            {"role": "user", "content": "go on"},
            {"role": "assistant", "progressive_fields": {"tension": "uneasy"}},
            {"role": "user", "content": "and then"},
        ]
        assert progressive.branch_baseline(history) == {"tension": "uneasy"}

    def test_assistant_without_progressive_fields_returns_empty(self):
        history = [{"role": "assistant", "content": "hi"}]
        assert progressive.branch_baseline(history) == {}

    def test_null_progressive_fields_returns_empty(self):
        history = [{"role": "assistant", "progressive_fields": None}]
        assert progressive.branch_baseline(history) == {}
