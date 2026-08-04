"""Cross-consumer contracts for the canonical non-workflow backend scanner."""

from __future__ import annotations

import json
from pathlib import Path

from backend.core.text_segmentation import HARD_LINE_BREAK_RE, split_sentences


def test_core_sentence_contract_cases():
    fixture = Path(__file__).parents[1] / "fixtures" / "text_segmentation_cases.json"
    for case in json.loads(fixture.read_text(encoding="utf-8")):
        actual = split_sentences(case["text"])
        assert actual == case["sentences"], case["name"]
        assert all(HARD_LINE_BREAK_RE.search(sentence) is None for sentence in actual), case["name"]
