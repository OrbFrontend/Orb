"""0036_endpoint_completion_mode -- add the per-endpoint transport selector.

'chat' (default) keeps the OpenAI-compatible /chat/completions transport; 'text' switches to llama.cpp's native /apply-template
+ /completion path.
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration(
    "endpoints", "completion_mode TEXT NOT NULL DEFAULT 'chat' CHECK (completion_mode IN ('chat', 'text'))", migration="0036"
)
