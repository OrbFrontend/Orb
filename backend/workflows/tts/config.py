"""Strict global configuration contract for the TTS workflow."""

from __future__ import annotations

import math
from typing import Any, Mapping

CONFIG_DEFAULTS = {
    "auto_play": False,
    "volume": 0.75,
    "click_granularity": "block",
    "click_play_scope": "unit",
    "show_karaoke": True,
}

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "auto_play": {"type": "boolean", "title": "Play generated speech automatically"},
        "volume": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "title": "Playback volume",
        },
        "click_granularity": {
            "type": "string",
            "enum": ["none", "message", "block"],
            "title": "Click-to-speak granularity",
        },
        "click_play_scope": {
            "type": "string",
            "enum": ["whole", "unit"],
            "title": "Click playback scope",
        },
        "show_karaoke": {"type": "boolean", "title": "Highlight words during playback (karaoke)"},
    },
}


def _enum(raw: Mapping[str, Any], key: str, allowed: set[str]) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) and value in allowed else CONFIG_DEFAULTS[key]


def normalize_config(raw: Mapping[str, Any] | None) -> dict:
    """Return the complete, bounded configuration shape used by UI and hooks."""
    source = raw if isinstance(raw, Mapping) else {}

    volume_raw = source.get("volume")
    if isinstance(volume_raw, (int, float)) and not isinstance(volume_raw, bool):
        volume = float(volume_raw)
        if not math.isfinite(volume):
            volume = CONFIG_DEFAULTS["volume"]
        else:
            volume = min(1.0, max(0.0, volume))
    else:
        volume = CONFIG_DEFAULTS["volume"]

    auto_play = source.get("auto_play")
    show_karaoke = source.get("show_karaoke")
    return {
        "auto_play": auto_play if isinstance(auto_play, bool) else CONFIG_DEFAULTS["auto_play"],
        "volume": volume,
        "click_granularity": _enum(source, "click_granularity", {"none", "message", "block"}),
        "click_play_scope": _enum(source, "click_play_scope", {"whole", "unit"}),
        "show_karaoke": show_karaoke if isinstance(show_karaoke, bool) else CONFIG_DEFAULTS["show_karaoke"],
    }
