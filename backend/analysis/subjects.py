"""The subject tagger's input and cache identity: pure, shared by tagging at save time and by the audit."""

from __future__ import annotations

import hashlib

from .text.markup import classify_axes, narration_only

# Bump when subjects_input changes: stored tags carry it, so a shaping change re-tags instead of mixing two input distributions.
SUBJECTS_INPUT_VERSION = "subjects-input-v1"


def subjects_input(text: str) -> str:
    """The narration the tagger reads, built exactly as ../ettin-subjects/src/orb.py built its training input."""
    return narration_only(text, classify_axes(text).dialogue).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
