"""Local prose rewriter configuration, service, and text helpers."""

from __future__ import annotations

from . import integration
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
    resolve_config,
    select_batch_size,
)
from .service import HOST, available, rewrite_events, shutdown, state

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "FEATURE",
    "HOST",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "ProseRewriteConfig",
    "UnknownVariant",
    "UnsupportedBatchSize",
    "available",
    "integration",
    "launch_profile",
    "launch_profile_for",
    "on_disk",
    "resolve",
    "resolve_batch_size",
    "resolve_config",
    "rewrite_events",
    "select_batch_size",
    "shutdown",
    "state",
    "variant_path",
    "variants",
]
