"""Mine fresh suggestions on request without ever blocking generation.

A run takes about a minute of CPU on a large library, so it happens in a spawned child process: nothing in the server's event
loop or GIL waits on it, and a chat streams normally while it runs. One run at a time; a failed or skipped run keeps the
previous suggestions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime

from ...database import (
    current_db_path,
    get_phrase_bank,
    get_settings,
    list_slop_dismissals,
    list_slop_suggestion_keys,
    replace_slop_suggestions,
)
from ...database.queries.dataset import get_dataset_epoch
from .miner import mine

logger = logging.getLogger(__name__)

_task: asyncio.Task[None] | None = None


def refreshing() -> bool:
    """Whether a run is in progress."""
    return _task is not None and not _task.done()


def refresh() -> None:
    """Start a background run unless one is already in progress."""
    global _task
    if not refreshing():
        _task = asyncio.create_task(_run(), name="slop-suggestions")


def _terminate(pool: ProcessPoolExecutor) -> None:
    """Stop a worker mid-run, so a cancelled run never leaves a minute of mining behind."""
    terminate = getattr(pool, "terminate_workers", None)  # Python 3.14+
    if callable(terminate):
        terminate()
        return
    for process in list((getattr(pool, "_processes", None) or {}).values()):
        process.terminate()


async def _run() -> None:
    ran_at = datetime.now(UTC).isoformat()
    try:
        epoch = await get_dataset_epoch()
        bank = await get_phrase_bank()
        toggles = dict((await get_settings())["editor_audit_toggles"] or {})
        dismissed_keys, dismissed_patterns = await list_slop_dismissals()
        keys_at_start = await list_slop_suggestion_keys()
        pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                pool, mine, current_db_path(), bank, toggles, dismissed_keys, dismissed_patterns
            )
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
            result["suggestions"], status=result["status"], mined_at=ran_at, keys_at_start=keys_at_start
        )
        logger.info("Slop suggestions: %s", result["status"])
    except Exception as exc:
        logger.exception("Slop suggestion run failed")
        with contextlib.suppress(Exception):
            await replace_slop_suggestions(None, status=f"error: {type(exc).__name__}: {exc}", mined_at=ran_at)


async def shutdown() -> None:
    """Cancel a run in progress; the run stops its own worker and records nothing."""
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
