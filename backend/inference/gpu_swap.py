"""Keep a local LLM server and ComfyUI from holding VRAM at the same time."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

ServerKind = Literal["llama_router", "llama_single", "ollama", "other"]
UNLOADABLE: frozenset[ServerKind] = frozenset({"llama_router", "ollama"})
Owner = Literal["llm", "comfy"]
Notify = Callable[[str], Awaitable[None] | None]
Stop = Callable[[], Awaitable[object]]

PROBE_TIMEOUT = 3.0
POLL_INTERVAL = 0.1
UNLOAD_CAP = 30.0
SETTLE_CAP = 5.0
SETTLE_RISE = 256 * 1024 * 1024
SETTLE_AGREE = 64 * 1024 * 1024
_ROUTER_RESIDENT = frozenset({"loaded", "loading"})


class GpuSwapError(RuntimeError):
    """A swap step that could not finish; the call it guarded does not run."""


@dataclass(frozen=True)
class LlmTarget:
    root: str
    model: str
    kind: ServerKind
    headers: Mapping[str, str] = field(default_factory=dict)
    proxy: str | None = None


@dataclass(frozen=True)
class SwapPlan:
    enabled: bool
    llm_targets: tuple[LlmTarget, ...] = ()
    comfy_url: str = ""
    comfy_key: str = ""
    wait_cap: float = 180.0

    def target_for(self, root: str) -> LlmTarget | None:
        root = root.rstrip("/")
        return next((t for t in self.llm_targets if t.root == root), None)

    def identity(self) -> tuple:
        return (tuple((t.root, t.model, t.kind) for t in self.llm_targets), self.comfy_url)


DISABLED = SwapPlan(enabled=False)


def _client(headers: Mapping[str, str] | None, proxy: str | None, timeout: float = PROBE_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, proxy=proxy or None, headers=dict(headers or {}))


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


_kinds: dict[str, ServerKind] = {}


def _forget(root: str) -> None:
    _kinds.pop(root.rstrip("/"), None)


async def probe(
    root: str, headers: Mapping[str, str] | None = None, proxy: str | None = None, *, fresh: bool = False
) -> ServerKind:
    """What kind of server answers at *root* (the base URL without ``/v1``); never loads a model.

    A kind is cached per root once a server answered. A root nothing answers at is ``other`` and stays uncached.
    """
    root = root.rstrip("/")
    if not fresh and root in _kinds:
        return _kinds[root]
    answered = False
    kind: ServerKind = "other"
    async with _client(headers, proxy) as client:
        try:
            resp = await client.get(f"{root}/props")
        except httpx.HTTPError:
            resp = None
        if resp is not None:
            answered = True
            data = _json(resp) if resp.status_code < 400 else None
            if isinstance(data, dict) and data.get("role") == "router":
                kind = "llama_router"
            elif "llama.cpp" in resp.headers.get("server", ""):
                kind = "llama_single"
        if kind == "other":
            try:
                resp = await client.get(f"{root}/api/version")
            except httpx.HTTPError:
                resp = None
            if resp is not None:
                answered = True
                data = _json(resp) if resp.status_code < 400 else None
                if isinstance(data, dict) and "version" in data:
                    kind = "ollama"
    if answered:
        _kinds[root] = kind
    return kind


def _ollama_name(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


async def _router_entries(client: httpx.AsyncClient, root: str) -> list[tuple[set[str], str, str]]:
    """``(names, id, status)`` per router listing entry, matched by id or alias, never by position."""
    resp = await client.get(f"{root}/models")
    resp.raise_for_status()
    data = _json(resp)
    entries: list[tuple[set[str], str, str]] = []
    for item in data.get("data", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        aliases = item.get("aliases")
        names = {item["id"], *(a for a in aliases if isinstance(a, str))} if isinstance(aliases, list) else {item["id"]}
        status = item.get("status")
        value = status.get("value") if isinstance(status, dict) else status
        entries.append((names, item["id"], value if isinstance(value, str) else ""))
    return entries


async def _ollama_resident(client: httpx.AsyncClient, root: str) -> set[str]:
    resp = await client.get(f"{root}/api/ps")
    resp.raise_for_status()
    data = _json(resp)
    names: set[str] = set()
    for item in data.get("models", []) if isinstance(data, dict) else []:
        if isinstance(item, dict):
            names.update(v for v in (item.get("name"), item.get("model")) if isinstance(v, str))
    return names


async def _resident(client: httpx.AsyncClient, target: LlmTarget) -> set[str]:
    if target.kind == "llama_router":
        return {mid for _, mid, status in await _router_entries(client, target.root) if status in _ROUTER_RESIDENT}
    if target.kind == "ollama":
        return await _ollama_resident(client, target.root)
    raise GpuSwapError(f"{target.root} cannot unload models")


async def loaded_models(target: LlmTarget) -> set[str]:
    """Models the server holds in memory (``loading`` counts; a router's ``sleeping`` does not)."""
    async with _client(target.headers, target.proxy) as client:
        try:
            return await _resident(client, target)
        except httpx.ConnectError:
            _forget(target.root)
            raise


async def model_loaded(target: LlmTarget, model: str) -> bool:
    """Whether *model* is resident on *target*'s server."""
    async with _client(target.headers, target.proxy) as client:
        try:
            if target.kind == "llama_router":
                entries = await _router_entries(client, target.root)
                return any(model in names and status in _ROUTER_RESIDENT for names, _, status in entries)
            return _ollama_name(model) in await _resident(client, target)
        except httpx.ConnectError:
            _forget(target.root)
            raise


async def unload(target: LlmTarget, model: str) -> None:
    """Unload *model* and return once the server no longer lists it as resident."""
    async with _client(target.headers, target.proxy) as client:
        if target.kind == "llama_router":
            resp = await client.post(f"{target.root}/models/unload", json={"model": model})
        elif target.kind == "ollama":
            resp = await client.post(f"{target.root}/api/generate", json={"model": model, "keep_alive": 0})
        else:
            raise GpuSwapError(f"{target.root} cannot unload models")
        resp.raise_for_status()
        deadline = time.monotonic() + UNLOAD_CAP
        while model in await _resident(client, target):
            if time.monotonic() >= deadline:
                raise GpuSwapError(f"{model} was still loaded on {target.root} after {UNLOAD_CAP:.0f}s")
            await asyncio.sleep(POLL_INTERVAL)


async def _vram_free(client: httpx.AsyncClient, url: str) -> int | None:
    resp = await client.get(f"{url}/system_stats")
    resp.raise_for_status()
    data = _json(resp)
    devices = data.get("devices") if isinstance(data, dict) else None
    first = devices[0] if isinstance(devices, list) and devices and isinstance(devices[0], dict) else {}
    value = first.get("vram_free")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def comfy_free(url: str, api_key: str = "", *, queue_cap: float = 180.0) -> None:
    """Ask ComfyUI to drop its models and wait until the VRAM is back.

    ``/free`` has no acknowledgement, so the wait reads ``vram_free``: it must rise past the pre-``/free`` baseline and then hold
    still. The rise bound is capped; a ComfyUI that already held nothing logs and moves on.
    """
    url = url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with _client(headers, None) as client:
        try:
            baseline = await _vram_free(client, url)
            resp = await client.post(f"{url}/free", json={"unload_models": True, "free_memory": True})
            resp.raise_for_status()
        except httpx.ConnectError:
            logger.info("gpu swap: ComfyUI at %s is not running; nothing to free", url)
            return
        deadline = time.monotonic() + queue_cap
        while True:
            resp = await client.get(f"{url}/queue")
            resp.raise_for_status()
            queue = _json(resp)
            if not (isinstance(queue, dict) and queue.get("queue_running")):
                break
            if time.monotonic() >= deadline:
                raise GpuSwapError(f"ComfyUI was still rendering after {queue_cap:.0f}s; the LLM was not reloaded")
            await asyncio.sleep(POLL_INTERVAL)
        if baseline is None:
            return
        deadline = time.monotonic() + SETTLE_CAP
        previous: int | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            current = await _vram_free(client, url)
            if current is None:
                return
            if current - baseline >= SETTLE_RISE and previous is not None and abs(current - previous) <= SETTLE_AGREE:
                return
            previous = current
        logger.info("gpu swap: ComfyUI VRAM did not settle within %.0fs of /free; continuing", SETTLE_CAP)


class _Arbiter:
    """Who holds the GPU, and how many calls on each side are using it.

    Counters change synchronously, so a lease released by a cancelled task still lands. Waiters watch ``_changed``, which is
    swapped for a fresh event on every change.
    """

    def __init__(self) -> None:
        self.owner: Owner | None = None
        self.llm_inflight = 0
        self.comfy_inflight = 0
        self.holds = 0
        self.switching = False
        self.plan_identity: tuple | None = None
        self._changed: asyncio.Event | None = None

    def changed(self) -> None:
        if self._changed is not None:
            self._changed.set()
        self._changed = None

    async def wait_until(self, ready: Callable[[], bool], cap: float, busy: str, stop: Stop | None = None) -> bool:
        """Wait for *ready*; False when *stop* fired first, :class:`GpuSwapError` after *cap* seconds."""
        if ready():
            return True
        deadline = time.monotonic() + cap
        stopped = asyncio.ensure_future(stop()) if stop is not None else None
        change: asyncio.Future | None = None
        try:
            while not ready():
                if stopped is not None and stopped.done():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GpuSwapError(f"Timed out waiting for {busy} to finish with the GPU ({cap:.0f}s)")
                if self._changed is None:
                    self._changed = asyncio.Event()
                change = asyncio.ensure_future(self._changed.wait())
                waiting = {change} if stopped is None else {change, stopped}
                await asyncio.wait(waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                change.cancel()
            return True
        finally:
            for pending in (change, stopped):
                if pending is not None:
                    pending.cancel()

    def observe(self, plan: SwapPlan) -> None:
        """Forget the owner when the plan turns on or points somewhere new."""
        identity = plan.identity() if plan.enabled else None
        if identity != self.plan_identity:
            self.plan_identity = identity
            self.owner = None


_arbiter = _Arbiter()
_provider: Callable[[], Awaitable[SwapPlan]] | None = None


def set_plan_provider(provider: Callable[[], Awaitable[SwapPlan]] | None) -> None:
    """Install the callable that answers the current plan; it must be cheap, as every lease asks."""
    global _provider
    _provider = provider


async def current_plan() -> SwapPlan:
    if _provider is None:
        return DISABLED
    plan = await _provider()
    _arbiter.observe(plan)
    return plan


async def _notify(notify: Notify | None, stage: str) -> None:
    if notify is not None:
        maybe = notify(stage)
        if maybe is not None:
            await maybe


async def _unload_all(plan: SwapPlan, notify: Notify | None) -> None:
    for target in {t.root: t for t in plan.llm_targets}.values():
        for model in sorted(await loaded_models(target)):
            await _notify(notify, "unloading")
            logger.info("gpu swap: unloading %s from %s before a ComfyUI render", model, target.root)
            await unload(target, model)


@contextlib.asynccontextmanager
async def comfy_lease(notify: Notify | None = None) -> AsyncIterator[None]:
    """Hold the GPU for one ComfyUI render, unloading the planned LLMs first.

    Waits for live turns and LLM calls to finish. *notify* hears ``waiting`` before a wait and ``unloading`` before each unload.
    """
    plan = await current_plan()
    if not plan.enabled:
        yield
        return
    arb = _arbiter

    def ready() -> bool:
        return not arb.switching and arb.llm_inflight == 0 and arb.holds == 0

    if not ready():
        await _notify(notify, "waiting")
    await arb.wait_until(ready, plan.wait_cap, "the LLM")
    if arb.comfy_inflight == 0:
        arb.switching = True
        try:
            await _unload_all(plan, notify)
            arb.owner = "comfy"
        except httpx.HTTPError as exc:
            raise GpuSwapError(f"Could not unload the LLM before rendering: {exc}") from exc
        finally:
            arb.switching = False
            arb.changed()
    arb.comfy_inflight += 1
    try:
        yield
    finally:
        arb.comfy_inflight -= 1
        arb.changed()


async def _claim_for_llm(plan: SwapPlan, target: LlmTarget, model: str) -> None:
    try:
        loaded = await model_loaded(target, model)
    except httpx.HTTPError:
        # The call itself reports an unreachable LLM server better than the swap can; freeing ComfyUI is always safe.
        loaded = False
    if not loaded:
        logger.info("gpu swap: freeing ComfyUI before %s reloads on %s", model, target.root)
        await comfy_free(plan.comfy_url, plan.comfy_key, queue_cap=plan.wait_cap)
    _arbiter.owner = "llm"


@contextlib.asynccontextmanager
async def llm_lease(root: str, model: str, *, stop: Stop | None = None) -> AsyncIterator[bool]:
    """Hold the GPU for one LLM call to *root*; yields False when *stop* fired while waiting.

    A no-op that yields True unless the plan is on and names *root*. A held lease never waits on another LLM lease, so a call
    made inside one cannot deadlock against a ComfyUI render waiting for both to end.
    """
    plan = await current_plan()
    target = plan.target_for(root) if plan.enabled else None
    if target is None:
        yield True
        return
    arb = _arbiter
    if not await arb.wait_until(lambda: not arb.switching and arb.comfy_inflight == 0, plan.wait_cap, "ComfyUI", stop):
        yield False
        return
    if arb.owner != "llm":
        arb.switching = True
        try:
            await _claim_for_llm(plan, target, model)
        except httpx.HTTPError as exc:
            raise GpuSwapError(f"Could not free ComfyUI before the LLM reloads: {exc}") from exc
        finally:
            arb.switching = False
            arb.changed()
    arb.llm_inflight += 1
    try:
        yield True
    finally:
        arb.llm_inflight -= 1
        arb.changed()


@contextlib.asynccontextmanager
async def turn_hold() -> AsyncIterator[None]:
    """Keep ComfyUI renders waiting for the whole turn, gaps between passes included."""
    plan = await current_plan()
    if not plan.enabled:
        yield
        return
    _arbiter.holds += 1
    try:
        yield
    finally:
        _arbiter.holds -= 1
        _arbiter.changed()
