"""Text-to-speech workflow.

The synthesis engine -- backend adapters, the adapter router, and the
dialogue extractor -- lives in the ``engine`` subpackage and depends only on
the standard library, ``httpx``, and ``edge_tts``; it carries no reference to
the workflow framework or the rest of the backend. Workflow binding
(registration metadata and pipeline hooks) lives at this package level so the
engine stays independently importable and testable.
"""

from __future__ import annotations

from ..registry import Workflow

_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "auto_play": {
            "type": "boolean",
            "title": "Play generated speech automatically",
        },
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
        "show_karaoke": {
            "type": "boolean",
            "title": "Highlight words during playback (karaoke)",
        },
    },
}

tts_workflow = Workflow(
    id="tts",
    display_name="Text-to-Speech",
    produces_artifacts=True,
    config_schema=_CONFIG_SCHEMA,
    config_defaults={
        "auto_play": False,
        "volume": 0.75,
        "click_granularity": "block",
        "click_play_scope": "unit",
        "show_karaoke": True,
    },
)
