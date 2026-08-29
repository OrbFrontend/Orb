"""What the rewriter is configured to be, and how that becomes a child process.

Three readings of one question. :class:`ProseRewriteConfig` is what the user
chose, resolved out of ``settings.local_ml_config``; ``SLOT_ALLOCATION`` is the
tuning that choice picks among; ``LaunchProfile`` is the command line that
comes out the other end.

EVERY NUMBER THAT REACHES ARGV IS A LITERAL FROM THIS MODULE. A stored or
request-supplied batch size is used only as a key into ``SLOT_ALLOCATION``;
what comes back is code-owned. Same barrier as a range check, minus the part
where the tainted value itself survives to the subprocess sink.

THE TWO ERRORS BELOW ARE THE FEATURE'S, NOT HTTP'S. A slice does not import
FastAPI to say "404": it raises something specific and the route owns the
mapping.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, TypedDict

from ...inference.local_models import assets, llama_server
from ...inference.local_models.catalog import ModelVariantSpec
from ...inference.local_models.llama_server import LaunchProfile
from . import catalog

#: Per slot, and the number is the trained envelope plus room to finish a
#: sentence: 512 source tokens is the documented maximum input, the generation
#: budget never exceeds 512, and the prompt's own three blocks are a dozen more.
#: n_ctx is divided by the slot count inside llama.cpp, so this multiplies.
CTX_PER_SLOT = 1280

#: ``batch_size -> (ctx_size, parallel, threads_http)``. Four lanes remains the
#: compatibility default; Settings exposes up to eight for machines with enough
#: memory. The multiplication is not free: the KV cache is allocated in full
#: when the model loads, and a 1280-token lane is 140 MB on the 1.7B and 190 MB
#: on the 4B.
SLOT_ALLOCATION: dict[int, tuple[int, int, int]] = {
    1: (1280, 1, 6),
    2: (2560, 2, 8),
    3: (3840, 3, 10),
    4: (5120, 4, 12),
    8: (10240, 8, 20),
}

MIN_BATCH_SIZE = min(SLOT_ALLOCATION)
MAX_BATCH_SIZE = max(SLOT_ALLOCATION)
DEFAULT_BATCH_SIZE = 4


class ProseRewriteConfig(TypedDict):
    """Resolved prose-rewriter config. A non-None value means enabled."""

    variant_id: str
    gpu: bool
    batch_size: int


class UnknownVariant(ValueError):
    """A selection that names no registered checkpoint. The route answers 404."""


class UnsupportedBatchSize(ValueError):
    """A lane count outside the closed allocation. The route answers 400."""


#: What the child calls itself in its own logs and on /v1/models.
ALIAS = "prose-rewriter"

#: Seconds at zero in-flight before the child is stopped and its VRAM released.
#: Matters most when the Writer is also local on the same card.
IDLE_TIMEOUT = float(os.environ.get("ORB_PROSE_REWRITER_IDLE", "300"))

# A request or preset may supply the key, but never the value that reaches the
# child command line. Returning a literal from this closed map is the same
# allowlist barrier CodeQL recommends for command arguments; range-checking and
# returning the original int leaves the taint attached even though the supported
# 1, 2, 3, 4, and 8 values are safe.
_BATCH_SIZE_ALLOWLIST = {size: size for size in SLOT_ALLOCATION}


def select_batch_size(value: object) -> int | None:
    """A code-owned batch size for an exact supported input, else ``None``."""
    if type(value) is not int:
        return None
    return _BATCH_SIZE_ALLOWLIST.get(value)


def resolve_batch_size(value: object) -> int:
    """A persisted parallel-paragraph count, with old/malformed blobs made safe."""
    return select_batch_size(value) or DEFAULT_BATCH_SIZE


def resolve_config(settings: Mapping[str, Any]) -> ProseRewriteConfig | None:
    """Resolve the rewriter config from *settings*, or ``None`` when it can't run.

    Four things must hold, and all four are cheap: the Local ML toggle is on,
    a variant is selected, that variant's GGUF is on disk, and a llama-server
    binary resolves. Checked here rather than at the seam so a turn never pays
    a filesystem walk twice and the gating at the call site is one boolean.

    Unlike the Agent's own passes this is **not** agent-gated: the rewriter is
    a local model on its own Local ML toggle and has nothing to do with whether
    the remote Agent passes are on.
    """
    if settings.get("local_ml_enabled", {}).get(catalog.FEATURE, True) is False:
        return None
    config = (settings.get("local_ml_config") or {}).get(catalog.FEATURE) or {}
    variant_id = str(config.get("variant") or "")
    if not runnable(variant_id):
        return None
    # `gpu` defaults on: someone who fetched the Vulkan build meant to use it,
    # and the checkbox is how they say otherwise.
    return {
        "variant_id": variant_id,
        "gpu": bool(config.get("gpu", True)),
        "batch_size": resolve_batch_size(config.get("batch_size")),
    }


def runnable(variant_id: str | None) -> bool:
    """Is there a selected variant, on disk, *and* a runtime binary?

    Pure filesystem facts; says nothing about the settings toggle, which is
    :func:`resolve_config`'s first line.
    """
    variant = catalog.resolve(variant_id)
    return variant is not None and catalog.on_disk(variant) and llama_server.runtime_ok()


def launch_profile(config: ProseRewriteConfig) -> LaunchProfile:
    """The resolved-config convenience wrapper, for the rewrite event stream."""
    variant = catalog.resolve(config["variant_id"])
    if variant is None:  # raced with a registry change since resolve_config
        raise UnknownVariant(f"Model {config['variant_id']!r} is no longer registered.")
    return launch_profile_for(variant, config["gpu"], config["batch_size"])


def launch_profile_for(variant: ModelVariantSpec, gpu: bool, batch_size: int) -> LaunchProfile:
    """The one constructor of a prose ``LaunchProfile`` — and the trust barrier.

    Proves the variant it was handed is the registered record before its path
    is allowed onto a command line, rejects a batch size outside the closed
    allocation, and resolves the model path through the shared asset store. The
    generic client cannot do any of this: it has no feature catalog to check
    against, which is why the check lives here rather than travelling with the
    argv assembly.
    """
    trusted = catalog.resolve(variant.id)
    if trusted is None or trusted != variant:
        raise UnknownVariant(f"Unregistered prose-rewriter variant {variant.id!r}")
    try:
        ctx_size, parallel, http_threads = SLOT_ALLOCATION[batch_size]
    except (KeyError, TypeError):
        supported = ", ".join(str(size) for size in SLOT_ALLOCATION)
        raise UnsupportedBatchSize(f"slots must be one of {supported}") from None
    path = assets.variant_path(trusted)
    if not os.path.exists(path):
        raise RuntimeError(f"{trusted.label} is not downloaded — {trusted.local_name} is missing.")
    return LaunchProfile(
        model_id=trusted.id,
        model_path=path,
        alias=ALIAS,
        # GPU vs CPU is this one number. Vulkan is a property of which binary
        # was fetched, not a runtime switch.
        gpu_layers=999 if gpu else 0,
        ctx_size=ctx_size,
        parallel=parallel,
        http_threads=http_threads,
        label=trusted.label,
        size_mb=trusted.size_mb,
    )


def profile_for_selection(variant: ModelVariantSpec | None, gpu: bool, batch_size: int) -> LaunchProfile | None:
    """A profile for a selection that can actually be loaded, else ``None``.

    The settings paths need "the selection changed" to be expressible even when
    the selection names nothing loadable — a variant with no file behind it is
    a stale host, not an error to raise at whoever pressed Save.
    """
    if variant is None or not catalog.on_disk(variant):
        return None
    return launch_profile_for(variant, gpu, batch_size)
