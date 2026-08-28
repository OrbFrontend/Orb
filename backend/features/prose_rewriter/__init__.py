"""Local prose rewriter: a purpose-trained SLM that humanises LLM prose.

Three GGUF checkpoints (``prose-rewriter-1.7b/4b-v1.2``) served by a supervised
``llama-server`` child, one paragraph per request, decoded together. Ported from
ProseRewriterWebUI — the prompt contract and the output repairs in ``text.py``
are that project's, verbatim, because they are properties of the weights and the
training corpus rather than settings.

The Editor pass runs this BEFORE its audit, so Orb's scanners see and patch the
rewritten prose rather than text that no longer exists.

WHAT IS NOT HERE: the child process, the binary, and the model files. Those are
``inference/local_models/``, shared with anything else that ever wants a local
model. This slice is the part that is only ever about the rewriter — its
prompt, its paragraph algorithm, its selection, and the tuning its launch
profile is built from.
"""

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
