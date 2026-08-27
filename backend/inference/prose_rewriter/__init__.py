"""Local prose rewriter: a purpose-trained SLM that humanises LLM prose.

Three GGUF checkpoints (``prose-rewriter-1.7b/4b-v1.2``) served by a supervised
``llama-server`` child, one paragraph per request, decoded together. Ported from
ProseRewriterWebUI — the prompt contract and the output repairs in ``text.py``
are that project's, verbatim, because they are properties of the weights and the
training corpus rather than settings.

The Editor pass runs this BEFORE its audit, so Orb's scanners see and patch the
rewritten prose rather than text that no longer exists.
"""

from __future__ import annotations

from .catalog import FEATURE, on_disk, resolve
from .rewrite import arewrite, available
from .runtime import runtime_ok
from .server import DEFAULT_SLOTS, HOST, MAX_SLOTS, MIN_SLOTS

DEFAULT_BATCH_SIZE = DEFAULT_SLOTS
MIN_BATCH_SIZE = MIN_SLOTS
MAX_BATCH_SIZE = MAX_SLOTS


def resolve_batch_size(value: object) -> int:
    """A persisted parallel-paragraph count, with old/malformed blobs made safe."""
    if type(value) is int and MIN_BATCH_SIZE <= value <= MAX_BATCH_SIZE:
        return value
    return DEFAULT_BATCH_SIZE


async def shutdown() -> None:
    """Stop the llama-server child. Orb's only managed subprocess — an orphan
    holds the GPU after Orb exits, so the app lifespan must call this."""
    await HOST.shutdown()


def state() -> dict[str, str]:
    """``{"state": idle|loading|ready|failed, "error": …}`` for the panel."""
    return {"state": HOST.state, "error": HOST.error}


__all__ = [
    "FEATURE",
    "DEFAULT_BATCH_SIZE",
    "HOST",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "arewrite",
    "available",
    "on_disk",
    "resolve",
    "resolve_batch_size",
    "runtime_ok",
    "shutdown",
    "state",
]
