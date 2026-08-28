"""How the rewriter launches its child: the closed allowlist, and the barrier.

TRANSITIONAL LOCATION. Phase 3 of the local-model split moves this file's
contents into ``features/prose_rewriter/config.py`` unchanged. It sits here
rather than there for one phase because the opposite — creating the feature
module early and importing it from ``inference/`` — is the exact upward edge
the split exists to delete.

EVERY NUMBER THAT REACHES ARGV IS A LITERAL FROM THIS MODULE. A stored or
request-supplied batch size is used only as a key into ``SLOT_ALLOCATION``;
what comes back is code-owned. Same barrier as a range check, minus the part
where the tainted value itself survives to the subprocess sink.
"""

from __future__ import annotations

import os

from ..local_models import assets
from ..local_models.catalog import ModelVariantSpec
from ..local_models.llama_server import LaunchProfile, ManagedLlamaServerHost
from . import catalog

#: Per slot, and the number is the trained envelope plus room to finish a
#: sentence: 512 source tokens is the documented maximum input, the generation
#: budget never exceeds 512, and the prompt's own three blocks are a dozen more.
#: n_ctx is divided by the slot count inside llama.cpp, so this multiplies.
CTX_PER_SLOT = 1280

#: ``batch_size -> (ctx_size, parallel, threads_http)``. Four lanes is the
#: compatibility default and the upper bound exposed in Settings. The
#: multiplication is not free: the KV cache is allocated in full when the model
#: loads, and a 1280-token lane is 140 MB on the 1.7B and 190 MB on the 4B.
#: More than four gives diminishing throughput here while reserving well over a
#: gigabyte before the first request arrives.
SLOT_ALLOCATION: dict[int, tuple[int, int, int]] = {
    1: (1280, 1, 6),
    2: (2560, 2, 8),
    3: (3840, 3, 10),
    4: (5120, 4, 12),
}

MIN_BATCH_SIZE = min(SLOT_ALLOCATION)
MAX_BATCH_SIZE = max(SLOT_ALLOCATION)
DEFAULT_BATCH_SIZE = MAX_BATCH_SIZE

#: What the child calls itself in its own logs and on /v1/models.
ALIAS = "prose-rewriter"

#: Seconds at zero in-flight before the child is stopped and its VRAM released.
#: Matters most when the Writer is also local on the same card.
IDLE_TIMEOUT = float(os.environ.get("ORB_PROSE_REWRITER_IDLE", "300"))

# A request or preset may supply the key, but never the value that reaches the
# child command line. Returning a literal from this closed map is the same
# allowlist barrier CodeQL recommends for command arguments; range-checking and
# returning the original int leaves the taint attached even though 1..4 is safe.
_BATCH_SIZE_ALLOWLIST = {size: size for size in SLOT_ALLOCATION}


def select_batch_size(value: object) -> int | None:
    """A code-owned batch size for an exact supported input, else ``None``."""
    if type(value) is not int:
        return None
    return _BATCH_SIZE_ALLOWLIST.get(value)


def resolve_batch_size(value: object) -> int:
    """A persisted parallel-paragraph count, with old/malformed blobs made safe."""
    return select_batch_size(value) or DEFAULT_BATCH_SIZE


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
        raise ValueError(f"Unregistered prose-rewriter variant {variant.id!r}")
    try:
        ctx_size, parallel, http_threads = SLOT_ALLOCATION[batch_size]
    except (KeyError, TypeError):
        raise ValueError(f"slots must be between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE}") from None
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


#: One host per process. The rewriter is a single-user local feature and a
#: second resident model would double the VRAM for no gain. Constructing it
#: registers it with the shared runtime manager, which is what makes the app
#: lifespan able to stop the child it may be supervising.
HOST = ManagedLlamaServerHost(name="prose_rewriter", idle_timeout=IDLE_TIMEOUT)
