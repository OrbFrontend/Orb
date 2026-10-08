"""The subject tagger's input and cache identity: pure, shared by tagging at save time and by the audit."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .text.markup import classify_axes, narration_only

# Bump when subjects_input changes: stored tags carry it, so a shaping change re-tags instead of mixing two input distributions.
SUBJECTS_INPUT_VERSION = "subjects-input-v1"

# Shared subject scopes for the Judge and Editor; these follow the tagger's categories.
SUBJECT_DESCRIPTIONS: Mapping[str, str] = {
    "eyes": "eyes or gaze",
    "hair": "hair on the head (including braids; excluding body or pubic hair)",
    "face": "face, expression, cheeks, blush, brows or jaw (excluding eyes and lips)",
    "mouth": "mouth, lips, smile, smirk, teeth or tongue (excluding genital lips)",
    "voice": "a character's voice (how it sounds, excluding the words spoken)",
    "breath": "breathing, heartbeat or pulse",
    "scent": "scent or smell",
    "hands": "hands, fingers or nails",
    "skin": "skin, complexion, texture, freckles, scars or flushing",
    "chest": "chest or breasts",
    "lower_body": "hips, thighs, legs or backside",
    "neck": "neck, throat or shoulders",
    "build": "overall body size, height, physique or figure (excluding individual body parts)",
    "clothing": "clothes a character wears",
    "accessory": "jewelry or accessories a character wears",
    "object": "items a character holds or carries (excluding clothes, jewelry, furniture and the room)",
    "nonhuman": "non-human features, such as horns, wings, a tail, animal ears, scales or a halo",
    "light": "the scene's light, glow or shadows",
    "sound": "background sounds, echoes or noises (excluding spoken words)",
    "weather": "the scene's weather, air or temperature",
}


def subjects_input(text: str) -> str:
    """The narration the tagger reads, shaped as its training input was."""
    return narration_only(text, classify_axes(text).dialogue).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
