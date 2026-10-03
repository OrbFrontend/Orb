"""Keep the stored suggestions current without ever blocking generation.

A run takes about a minute of CPU on a large library, so it happens in a
spawned child process: nothing in the server's event loop or GIL waits on it,
and a chat streams normally while it runs. One run at a time; a failed or
skipped run keeps the previous suggestions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime

from ...database import (
    connection,
    count_model_replies,
    get_phrase_bank,
    get_settings,
    get_slop_replies_at_run,
    list_slop_dismissed_keys,
    list_slop_suggestion_keys,
    replace_slop_suggestions,
)
from ...database.queries.dataset import get_dataset_epoch
from .miner import mine

logger = logging.getLogger(__name__)

#: Model replies added, or removed, since the last run that make the suggestions stale.
REFRESH_REPLIES = 200
STARTUP_DELAY_SECONDS = 60

_task: asyncio.Task[None] | None = None


def refreshing() -> bool:
    """Whether a run is in progress."""
    return _task is not None and not _task.done()


def stale(replies_at_run: int | None, replies: int) -> bool:
    """Whether a run is due. The count moving either way counts: deleted chats
    or a restore that shrinks the library leave suggestions quoting sentences
    that are gone."""
    return replies_at_run is None or abs(replies - replies_at_run) >= REFRESH_REPLIES


async def refresh_if_stale() -> bool:
    """Start a background run if the data is stale. Returns whether a run is in progress."""
    global _task
    if refreshing():
        return True
    replies = await count_model_replies()
    if not stale(await get_slop_replies_at_run(), replies):
        return False
    if refreshing():  # another caller started one while this one awaited
        return True
    _task = asyncio.create_task(_run(replies), name="slop-suggestions")
    return True


async def refresh_after_startup(delay: float = STARTUP_DELAY_SECONDS) -> None:
    """The startup check: the same staleness test, once the server has settled."""
    await asyncio.sleep(delay)
    try:
        await refresh_if_stale()
    except Exception:
        logger.exception("Slop suggestion startup check failed")


def _terminate(pool: ProcessPoolExecutor) -> None:
    """Stop a worker mid-run, so a cancelled run never leaves a minute of mining behind."""
    terminate = getattr(pool, "terminate_workers", None)  # Python 3.14+
    if callable(terminate):
        terminate()
        return
    for process in list((getattr(pool, "_processes", None) or {}).values()):
        process.terminate()


async def _run(replies: int) -> None:
    ran_at = datetime.now(UTC).isoformat()
    try:
        epoch = await get_dataset_epoch()
        bank = await get_phrase_bank()
        toggles = dict((await get_settings())["editor_audit_toggles"] or {})
        dismissed = await list_slop_dismissed_keys()
        keys_at_start = await list_slop_suggestion_keys()
        pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        try:
            result = await asyncio.get_running_loop().run_in_executor(pool, mine, connection.DB_PATH, bank, toggles, dismissed)
        except BaseException:
            # Cancellation included: the worker would otherwise mine on unobserved.
            _terminate(pool)
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if await get_dataset_epoch() != epoch:
            # A restore replaced the dataset mid-run; these results describe the old one.
            logger.info("Discarding slop suggestions mined from a replaced dataset")
            return
        await replace_slop_suggestions(
            result["suggestions"],
            replies_at_run=replies,
            status=result["status"],
            mined_at=ran_at,
            keys_at_start=keys_at_start,
        )
        logger.info("Slop suggestions: %s", result["status"])
    except Exception as exc:
        logger.exception("Slop suggestion run failed")
        # Recorded with the reply count, so a failing run retries once the count
        # moves by REFRESH_REPLIES rather than on every Phrase Bank visit.
        with contextlib.suppress(Exception):
            await replace_slop_suggestions(
                None, replies_at_run=replies, status=f"error: {type(exc).__name__}: {exc}", mined_at=ran_at
            )


async def shutdown() -> None:
    """Cancel a run in progress; the run stops its own worker. Nothing is recorded,
    so the next startup check runs it again."""
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
