"""Local prose rewriter: a purpose-trained SLM that humanises LLM prose.

Three GGUF checkpoints (``prose-rewriter-1.7b/4b-v1.2``) served by a supervised
``llama-server`` child, one paragraph per request, decoded together. Ported from
ProseRewriterWebUI — the prompt contract and the output repairs in ``text.py``
are that project's, verbatim, because they are properties of the weights and the
training corpus rather than settings.

The Editor pass runs this BEFORE its audit, so Orb's scanners see and patch the
rewritten prose rather than text that no longer exists.

The child process, the binary and the asset store are NOT this feature's: they
live in ``inference/local_models/`` and are shared. What is here is the part
that is only ever about the rewriter — its prompt, its paragraph algorithm, its
selection, and the tuning its launch profile is built from.
"""

from __future__ import annotations

from ..local_models.llama_server import runtime_ok
from .catalog import FEATURE, on_disk, resolve, variants
from .profile import (
    DEFAULT_BATCH_SIZE,
    HOST,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    launch_profile_for,
    profile_for_selection,
    resolve_batch_size,
    select_batch_size,
)
from .rewrite import arewrite, available


async def shutdown() -> None:
    """Stop the llama-server child. Orb's only managed subprocess — an orphan
    holds the GPU after Orb exits, so the app lifespan must call this."""
    await HOST.shutdown()


def state() -> dict[str, str]:
    """``{"state": idle|loading|ready|failed, "error": …}`` for the panel."""
    return {"state": HOST.state, "error": HOST.error}


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "FEATURE",
    "HOST",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "arewrite",
    "available",
    "launch_profile_for",
    "on_disk",
    "profile_for_selection",
    "resolve",
    "resolve_batch_size",
    "runtime_ok",
    "select_batch_size",
    "shutdown",
    "state",
    "variants",
]
