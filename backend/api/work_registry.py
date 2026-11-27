"""Process-local ownership of chat, document and assistant work.

Phases overlap: media can render beside a stream, deletion fences admission before
waiting for existing work, and Stop retains ownership until persistence settles.
All admission checks and reservations run without yielding on the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, TypeVar

from ..inference import AbortToken

_T = TypeVar("_T")


class WorkPhase(StrEnum):
    STREAMING = "streaming"
    MUTATING = "mutating"
    RENDERING = "rendering"
    COMMITTING = "committing"
    DELETING = "deleting"
    SETTLING = "settling"


class ChatUnavailable(Exception):
    """Admission failed because deletion or existing lane ownership blocks it."""

    def __init__(self, reason: Literal["deleting", "busy"], key: str):
        self.reason = reason
        self.key = key
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class WorkSnapshot:
    """An empty phase set means idle; this observation is not a reservation."""

    phases: frozenset[WorkPhase] = frozenset()
    lane_busy: bool = False

    @property
    def busy(self) -> bool:
        return bool(self.phases)


@dataclass(slots=True)
class ActiveStream:
    """One token for a turn; settled confirms generator cleanup and lane release."""

    abort_token: AbortToken | None
    operation_id: str | None = None
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    settling: bool = False


@dataclass(slots=True)
class _WorkflowJob:
    name: str | None
    message_id: int | None
    committing: int = 0


@dataclass(slots=True)
class _ResourceWork:
    lane: asyncio.Lock = field(default_factory=asyncio.Lock)
    lane_claims: int = 0  # Owner plus queued mutations; no idle gap during handoff.
    stream: ActiveStream | None = None
    jobs: dict[asyncio.Task[Any], _WorkflowJob] = field(default_factory=dict)
    deleting: bool = False
    deleting_messages: set[int] = field(default_factory=set)

    def snapshot(self) -> WorkSnapshot:
        phases: set[WorkPhase] = set()
        if self.stream is not None:
            stream = self.stream
            phases.add(
                WorkPhase.SETTLING
                if stream.settling or (stream.abort_token is not None and stream.abort_token.is_aborted)
                else WorkPhase.STREAMING
            )
        elif self.lane_claims:
            phases.add(WorkPhase.MUTATING)
        for task, job in self.jobs.items():
            if job.committing:
                phases.add(WorkPhase.COMMITTING)
            elif not task.cancelling():
                phases.add(WorkPhase.RENDERING)
            if task.cancelling():
                phases.add(WorkPhase.SETTLING)
        if self.deleting or self.deleting_messages:
            phases.add(WorkPhase.DELETING)
        return WorkSnapshot(frozenset(phases), lane_busy=bool(self.lane_claims))


class WorkRegistry:
    def __init__(self) -> None:
        self._resources: dict[str, _ResourceWork] = {}

    def _resource(self, key: str) -> _ResourceWork:
        if key not in self._resources:
            self._resources[key] = _ResourceWork()
        return self._resources[key]

    def _discard_idle(self, key: str, state: _ResourceWork) -> None:
        # lane_claims includes queued waiters, so a live lane cannot be replaced.
        if not state.snapshot().busy and self._resources.get(key) is state:
            del self._resources[key]

    def snapshot(self, key: str) -> WorkSnapshot:
        state = self._resources.get(key)
        return state.snapshot() if state is not None else WorkSnapshot()

    def stream(self, key: str) -> ActiveStream | None:
        state = self._resources.get(key)
        return state.stream if state is not None else None

    def jobs(
        self, key: str | None = None, *, job: str | None = None, message_ids: set[int] | None = None
    ) -> tuple[asyncio.Task[Any], ...]:
        return tuple(
            task
            for resource, state in self._resources.items()
            if key is None or resource == key
            for task, info in state.jobs.items()
            if (job is None or info.name == job) and (message_ids is None or info.message_id in message_ids)
        )

    def resource_deleting(self, key: str) -> bool:
        state = self._resources.get(key)
        return state is not None and state.deleting

    def message_deleting(self, message_id: int) -> bool:
        return any(message_id in state.deleting_messages for state in self._resources.values())

    def message_busy(self, key: str, message_id: int) -> bool:
        state = self._resources.get(key)
        return state is not None and any(job.message_id == message_id for job in state.jobs.values())

    def stream_keys(self) -> list[str]:
        """The resources with a stream running or settling."""
        return [key for key, state in self._resources.items() if state.stream is not None]

    def active_work(self) -> list[str]:
        labels = {
            WorkPhase.STREAMING: "Generation",
            WorkPhase.MUTATING: "Mutation",
            WorkPhase.RENDERING: "Media",
            WorkPhase.COMMITTING: "Media commit",
            WorkPhase.DELETING: "Deletion",
            WorkPhase.SETTLING: "Settling work",
        }
        work: list[str] = []
        for key, state in self._resources.items():
            phases = state.snapshot().phases
            work.extend(f"{label} in {key}" for phase, label in labels.items() if phase in phases)
        return work

    @contextmanager
    def deletion_fence(self, keys: Sequence[str], *, require_idle: bool = False) -> Iterator[None]:
        # Validate the entire set before claiming any resource. In particular,
        # an existing deletion is a conflict, never a fence we can borrow.
        unique_keys = dict.fromkeys(keys)
        for key in unique_keys:
            if self.resource_deleting(key):
                raise ChatUnavailable("deleting", key)
            if require_idle and self.snapshot(key).busy:
                raise ChatUnavailable("busy", key)
        states = {key: self._resource(key) for key in unique_keys}
        for state in states.values():
            state.deleting = True
        try:
            yield
        finally:
            for key, state in states.items():
                state.deleting = False
                self._discard_idle(key, state)

    @contextmanager
    def message_deletion_fence(self, key: str, message_ids: set[int]) -> Iterator[None]:
        state = self._resource(key)
        if state.deleting_messages & message_ids:
            raise ChatUnavailable("deleting", key)
        state.deleting_messages.update(message_ids)
        try:
            yield
        finally:
            state.deleting_messages.difference_update(message_ids)
            self._discard_idle(key, state)

    @asynccontextmanager
    async def hold_lane(self, key: str) -> AsyncIterator[None]:
        state = self._resource(key)
        state.lane_claims += 1
        try:
            async with state.lane:
                yield
        finally:
            state.lane_claims -= 1
            self._discard_idle(key, state)

    async def _take_idle_lane(self, key: str) -> _ResourceWork:
        state = self._resource(key)
        if state.deleting:
            raise ChatUnavailable("deleting", key)
        if state.lane_claims:
            raise ChatUnavailable("busy", key)
        # No owner or queued mutation: acquire cannot suspend. Counting queued
        # claims also closes asyncio.Lock's unlocked window before a waiter wakes.
        state.lane_claims += 1
        try:
            await state.lane.acquire()
        except BaseException:
            state.lane_claims -= 1
            self._discard_idle(key, state)
            raise
        return state

    @asynccontextmanager
    async def idle_lane(self, key: str) -> AsyncIterator[None]:
        state = await self._take_idle_lane(key)
        try:
            yield
        finally:
            state.lane.release()
            state.lane_claims -= 1
            self._discard_idle(key, state)

    async def begin_stream(self, key: str, token: AbortToken | None, operation_id: str | None) -> ActiveStream:
        state = await self._take_idle_lane(key)
        stream = ActiveStream(token, operation_id)
        state.stream = stream
        return stream

    def settling_stream(self, stream: ActiveStream) -> None:
        stream.settling = True

    def finish_stream(self, key: str, stream: ActiveStream) -> None:
        state = self._resource(key)
        if state.stream is not stream:
            raise RuntimeError("Stream no longer owns its resource lane")
        state.stream = None
        state.lane.release()
        state.lane_claims -= 1
        stream.settled.set()
        self._discard_idle(key, state)

    async def stop_stream(self, key: str, *, operation_id: str | None, timeout: float) -> dict[str, bool]:
        stream = self.stream(key)
        if stream is None or stream.abort_token is None or (operation_id is not None and stream.operation_id != operation_id):
            return {"active": False, "settled": True}
        self.settling_stream(stream)
        stream.abort_token.abort()
        try:
            await asyncio.wait_for(stream.settled.wait(), timeout)
        except TimeoutError:
            return {"active": True, "settled": False}
        return {"active": True, "settled": True}

    def start_job(
        self, key: str, coro: Coroutine[Any, Any, _T], *, job: str | None, message_id: int | None
    ) -> asyncio.Task[_T]:
        state = self._resource(key)
        if state.deleting or message_id in state.deleting_messages:
            coro.close()
            raise ChatUnavailable("deleting", key)
        task = asyncio.create_task(coro)
        state.jobs[task] = _WorkflowJob(job, message_id)

        def forget(done: asyncio.Task[Any]) -> None:
            state.jobs.pop(done, None)
            self._discard_idle(key, state)

        task.add_done_callback(forget)
        return task

    def _job(self, task: asyncio.Task[Any] | None) -> _WorkflowJob | None:
        return next((state.jobs[task] for state in self._resources.values() if task in state.jobs), None)

    @contextmanager
    def committing_job(self) -> Iterator[None]:
        job = self._job(asyncio.current_task())
        if job is None:
            yield
            return
        job.committing += 1
        try:
            yield
        finally:
            job.committing -= 1

    def cancel_job(self, task: asyncio.Task[Any]) -> None:
        """Request cancellation once, preserving cleanup and any final write."""
        job = self._job(task)
        if job is not None and not job.committing and not task.cancelling():
            task.cancel()

    async def stop_jobs(self, key: str, *, job: str | None, message_ids: set[int] | None, timeout: float) -> dict[str, Any]:
        jobs = self.jobs(key, job=job, message_ids=message_ids)
        if not jobs:
            return {"stopped": 0, "settled": True}
        for task in jobs:
            self.cancel_job(task)
        _, pending = await asyncio.wait(jobs, timeout=timeout)
        return {"stopped": len(jobs), "settled": not pending}


work_registry = WorkRegistry()
