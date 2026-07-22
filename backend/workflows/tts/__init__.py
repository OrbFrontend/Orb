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
from .config import CONFIG_DEFAULTS, CONFIG_SCHEMA, normalize_config

tts_workflow = Workflow(
    id="tts",
    display_name="Text-to-Speech",
    produces_artifacts=True,
    config_schema=CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    config_normalizer=normalize_config,
)
