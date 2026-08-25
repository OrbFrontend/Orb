"""One draft in, one rewritten draft out.

``plan`` splits the draft into slots BEFORE anything runs. Paragraphs generate
simultaneously for throughput, but progress is published in document order:
the reader should see the top of the draft settle before a later paragraph
changes. The layout puts each rewrite back where it belongs and lets a partial
assembly be emitted while the rest are still decoding.

EVERY LIMIT HERE CLAMPS RATHER THAN RAISES — see :func:`_admissible`. A rewrite
that declines part of a draft is a worse rewrite; one that fails the turn is a
bug.

SAMPLING IS FIXED at the reference defaults. ``temperature`` and ``top_p`` are
properties of how these weights were tuned, not preferences, and exposing them
is an invitation to degrade the model in a way nothing reports.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import catalog, runtime
from . import text as T
from .catalog import Variant
from .server import HOST, ModelHost

logger = logging.getLogger(__name__)

TEMPERATURE = 0.9
TOP_P = 0.9


def budget(n_tokens: int) -> int:
    """How many tokens a paragraph of *n_tokens* is allowed.

    1.6x the source plus a floor, capped at 512: the model is trained to land
    near the source's length, so a budget proportional to it stops a runaway
    from spending a slot on four hundred tokens of drift while another
    paragraph waits.
    """
    return max(96, min(512, int(n_tokens * 1.6) + 32))


def assemble(layout: list[tuple[str, str]], done: dict[int, str]) -> str:
    """The draft as it stands: every finished rewrite in place, the rest as-is.

    Called after each paragraph completes, so the mid-rewrite ``draft_update``
    is always a coherent whole document rather than a fragment.
    """
    out: list[str] = []
    index = 0
    for kind, piece in layout:
        if kind == "keep":
            out.append(piece)
        else:
            out.append(done.get(index, piece))
            index += 1
    return "".join(out)


async def arewrite(
    draft: str,
    variant: Variant,
    *,
    gpu: bool = True,
    host: ModelHost | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Rewrite *draft* paragraph-by-paragraph and return the reassembled text.

    *on_progress* is awaited with the whole current assembly after each
    contiguous top-to-bottom run of completed paragraphs — never a delta, which
    would be meaningless when generation is concurrent.

    Raises on anything that stops the rewrite happening at all (no binary, no
    GGUF, boot failure, HTTP error). The Editor-pass caller turns that into a
    pass-through plus a warning; this layer does not decide policy.
    """
    host = host or HOST
    server = await host.ensure(variant, gpu)

    layout = T.plan(draft)
    jobs = _admissible(layout)
    if not jobs:
        return draft

    done: dict[int, str] = {}
    completed: set[int] = set()
    # ``jobs`` is in source order. A later paragraph may finish first, but its
    # snapshot waits here until every preceding job has settled (whether it
    # rewrote successfully or correctly passed through unchanged).
    next_progress = 0
    last_snapshot = draft
    # Twice the slot count keeps the scheduler fed at all times — there is
    # always a request waiting to fill a slot the moment one frees — without
    # opening ninety-six connections for a ninety-six-paragraph draft.
    admit = asyncio.Semaphore(max(2, host.slots * 2))
    lock = asyncio.Lock()

    async def run(index: int, source: str) -> None:
        nonlocal last_snapshot, next_progress
        async with admit:
            result = ""
            n = await server.count_tokens(source)
            if n > T.MAX_SOURCE_TOKENS:
                # Out past the trained envelope. Passing it through is the
                # honest answer; the reference errors because a human can split it.
                logger.info("Prose rewriter: paragraph %d is %d tokens (>%d); left unchanged", index, n, T.MAX_SOURCE_TOKENS)
            else:
                raw, stopped = await server.generate(
                    T.serve_prompt(source), n_predict=budget(n), temperature=TEMPERATURE, top_p=TOP_P
                )
                result = T.finish(raw, stopped)
            async with lock:
                if result:
                    done[index] = result
                completed.add(index)

                # Awaiting a callback while holding this small bookkeeping lock
                # serializes its delivery too. In production it is an unbounded
                # Queue.put (no wait), and this keeps a slow custom callback from
                # letting a newer snapshot overtake an older one.
                advanced = False
                while next_progress < len(jobs) and jobs[next_progress][0] in completed:
                    next_progress += 1
                    advanced = True
                snapshot = assemble(layout, done) if advanced else ""
                if on_progress is not None and snapshot and snapshot != last_snapshot:
                    last_snapshot = snapshot
                    await on_progress(snapshot)

    async with host.acquire():
        # Cancel-on-failure, which a bare ``gather`` does not do: it propagates
        # the first failure but leaves the others RUNNING, holding llama-server
        # slots after this call released its in-flight count — which then lets a
        # swap or the idle unload stop the child underneath them. A dead child
        # fails every paragraph at once, so that is the ordinary case. A
        # ``TaskGroup`` would wrap the exception in an ``ExceptionGroup`` and
        # cost the warning its message, so the tasks are tracked by hand.
        tasks = [asyncio.create_task(run(i, source)) for i, source in jobs]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    return assemble(layout, done)


def _admissible(layout: list[tuple[str, str]]) -> list[tuple[int, str]]:
    """``(slot index, source)`` for every paragraph this run will actually rewrite.

    The caps clamp by declining to rewrite rather than by dropping text: a piece
    past either limit keeps the writer's words and stays in the layout, so the
    reassembled draft is always the whole draft. The reference rejects the
    request instead — right for a person pasting into a text box, wrong for a
    turn already in flight with nobody to ask.
    """
    jobs: list[tuple[int, str]] = []
    chars = 0
    index = 0
    for kind, piece in layout:
        if kind != "rewrite":
            continue
        slot = index
        index += 1
        chars += len(piece)
        if len(jobs) >= T.MAX_PARAGRAPHS or chars > T.MAX_CHARS:
            continue
        jobs.append((slot, piece))
    return jobs


def available(variant_id: str | None) -> bool:
    """Is there a selected variant, on disk, *and* a runtime binary?

    Pure filesystem facts; says nothing about the settings toggle, which is the
    caller's business.
    """
    variant = catalog.resolve(variant_id)
    return variant is not None and catalog.on_disk(variant) and runtime.runtime_ok()
