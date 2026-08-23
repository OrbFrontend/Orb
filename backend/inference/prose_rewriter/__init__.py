"""Local prose rewriter: a purpose-trained SLM that humanises LLM prose.

Three GGUF checkpoints (``prose-rewriter-1.7b/4b-v1.2``) served by a supervised
``llama-server`` child, one paragraph per request, decoded together. Ported from
ProseRewriterWebUI — the prompt contract and the four output repairs in
``text.py`` are that project's, verbatim, because they are properties of the
weights and the training corpus rather than settings.

The Editor pass runs this BEFORE its audit, so Orb's scanners see and patch the
rewritten prose rather than text that no longer exists.

Facade:
    ``arewrite`` — one draft in, one rewritten draft out
    ``available`` — is a variant selected, downloaded, and is there a binary?
    ``runtime_ok`` — the llama-server row on the Settings panel
    ``shutdown`` — stop the child; registered in the FastAPI lifespan
"""

from __future__ import annotations

from .catalog import (
    DEFAULT_ID,
    FEATURE,
    Variant,
    get,
    on_disk,
    resolve,
    variant_path,
    variants,
)
from .rewrite import arewrite, available, runtime_ok
from .server import HOST


async def shutdown() -> None:
    """Stop the llama-server child. Orb's only managed subprocess — an orphan
    holds the GPU after Orb exits, so the app lifespan must call this."""
    await HOST.shutdown()


def state() -> dict[str, str]:
    """``{"state": idle|loading|ready|failed, "error": …}`` for the panel."""
    return {"state": HOST.state, "error": HOST.error}


__all__ = [
    "DEFAULT_ID",
    "FEATURE",
    "HOST",
    "Variant",
    "arewrite",
    "available",
    "get",
    "on_disk",
    "resolve",
    "runtime_ok",
    "shutdown",
    "state",
    "variant_path",
    "variants",
]
