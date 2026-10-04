"""0048_reasoning_prefill -- add the per-pass reasoning-prefill column: the text mode-only seed text prepended inside each
pass's thought channel. Mirrors reasoning_enabled_passes (same three keys, same JSON-blob shape).
"""

from __future__ import annotations

from .helpers import column_migration

_DEFAULT = '{"director":"","writer":"","editor":""}'


migrate = column_migration("settings", f"reasoning_prefill_passes TEXT NOT NULL DEFAULT '{_DEFAULT}'", migration="0048")
