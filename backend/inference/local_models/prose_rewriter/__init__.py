"""Local prose-rewriter model execution and text processing."""

from __future__ import annotations

from .catalog import FEATURE, on_disk, resolve, variant_path, variants
from .config import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    ProseRewriteConfig,
    UnknownVariant,
    UnsupportedBatchSize,
    launch_profile,
    launch_profile_for,
    resolve_batch_size,
    select_batch_size,
)
from .service import HOST, available, rewrite_events, shutdown, state
