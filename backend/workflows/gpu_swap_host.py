"""Answer the GPU swap plan from settings and the image-gen config."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from ..core.settings import Settings
from ..database import get_settings
from ..inference import EndpointConfigError, LLMClient, agent_client_from_settings, client_from_settings, gpu_swap
from ..inference import separate_agent_lane_configured as _separate_agent_lane_configured
from .enablement import effective_workflow_enabled
from .errors import WorkflowUnavailableError
from .registry import get_workflow_config

logger = logging.getLogger(__name__)

IMAGE_GEN = "image_gen"
PLAN_TTL = 5.0

_cached: tuple[float, gpu_swap.SwapPlan] | None = None


def invalidate() -> None:
    """Drop the cached plan so the next lease reads the saved option."""
    global _cached
    _cached = None


def _lane_clients(settings: Settings) -> list[tuple[str, LLMClient, str]]:
    """``(lane, client, model)`` for each configured HTTP lane."""
    lanes: list[tuple[str, LLMClient, str]] = []
    with contextlib.suppress(EndpointConfigError, ValueError):
        lanes.append(("writer", client_from_settings(settings), str(settings.get("model_name") or "")))
    try:
        separate = _separate_agent_lane_configured(settings)
    except EndpointConfigError:
        separate = False
    if separate:
        with contextlib.suppress(ValueError):
            lanes.append(("agent", agent_client_from_settings(settings), str(settings.get("agent_model_name") or "")))
    return [(lane, client, model) for lane, client, model in lanes if not client.base_url.lower().startswith("claude-code:")]


async def probe_lanes(settings: Settings, *, fresh: bool = False) -> list[tuple[str, gpu_swap.LlmTarget]]:
    """Each lane's server, probed without loading a model."""
    out: list[tuple[str, gpu_swap.LlmTarget]] = []
    for lane, client, model in _lane_clients(settings):
        root, headers = client.server_root(), client.request_headers()
        kind = await gpu_swap.probe(root, headers, client.proxy, fresh=fresh)
        out.append((lane, gpu_swap.LlmTarget(root=root, model=model, kind=kind, headers=headers, proxy=client.proxy)))
    return out


def _comfy(config: Mapping[str, Any]) -> Mapping[str, Any]:
    external = config.get("external_comfy")
    return external if isinstance(external, Mapping) else {}


def _wait_cap(config: Mapping[str, Any]) -> float:
    try:
        return min(900.0, max(10.0, float(config.get("timeout_seconds", 180.0))))
    except (TypeError, ValueError):
        return 180.0


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:  # a malformed port fails the calls themselves; it must not turn the whole plan off
        return scheme, url, None
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


def _hosted_comfy(comfy_url: str, targets: list[gpu_swap.LlmTarget]) -> tuple[str, str]:
    """``(llama-swap root, model ID)`` when ComfyUI is served through a lane's llama-swap, else empty strings."""
    for target in targets:
        if target.kind != "llama_swap" or _origin(comfy_url) != _origin(target.root):
            continue
        base, path = urlsplit(target.root).path.rstrip("/"), urlsplit(comfy_url).path.rstrip("/")
        if not path.startswith(base):
            continue
        rest = path[len(base) :]
        if rest == "/comfyui":
            return target.root, "comfyui_auto"
        if rest.startswith("/upstream/") and rest != "/upstream/":
            return target.root, unquote(rest[len("/upstream/") :])
    return "", ""


async def _build() -> gpu_swap.SwapPlan:
    settings = await get_settings()
    config = await get_workflow_config(IMAGE_GEN)
    external = _comfy(config)
    if external.get("gpu_swap") is not True or not effective_workflow_enabled(IMAGE_GEN, settings):
        return gpu_swap.DISABLED
    targets: list[gpu_swap.LlmTarget] = []
    for _, target in await probe_lanes(settings):
        if target.kind in gpu_swap.UNLOADABLE and all((t.root, t.model) != (target.root, target.model) for t in targets):
            targets.append(target)
    comfy_url = str(external.get("api_url") or "").rstrip("/")
    comfy_host, comfy_model = _hosted_comfy(comfy_url, targets)
    return gpu_swap.SwapPlan(
        enabled=bool(targets),
        llm_targets=tuple(targets),
        comfy_url=comfy_url,
        comfy_key=str(external.get("api_key") or ""),
        wait_cap=_wait_cap(config),
        slot_restore=external.get("gpu_swap_restore") is True,
        comfy_host=comfy_host,
        comfy_model=comfy_model,
    )


async def plan() -> gpu_swap.SwapPlan:
    """The current plan, rebuilt at most every :data:`PLAN_TTL` seconds; any failure reads as off."""
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < PLAN_TTL:
        return _cached[1]
    try:
        built = await _build()
    except Exception as exc:
        logger.debug("gpu swap: plan unavailable (%r); swapping is off", exc)
        built = gpu_swap.DISABLED
    _cached = (now, built)
    return built


gpu_swap.set_plan_provider(plan)


@contextlib.asynccontextmanager
async def comfy_lease(notify: gpu_swap.Notify | None = None) -> AsyncIterator[None]:
    """Hold the GPU for one ComfyUI render; a swap that cannot finish is a user-facing error."""
    try:
        async with gpu_swap.comfy_lease(notify):
            yield
    except gpu_swap.GpuSwapError as exc:
        raise WorkflowUnavailableError(str(exc)) from exc


async def support(settings: Settings) -> dict:
    """Which lanes run on a server Orb can unload, probed fresh."""
    lanes = await probe_lanes(settings, fresh=True)
    available = any(target.kind in gpu_swap.UNLOADABLE for _, target in lanes)
    locked = next((lane for lane, target in lanes if target.kind == "needs_admin"), None)
    if available:
        reason = ""
    elif locked is not None:
        reason = f"The {locked.capitalize()} endpoint's key can't load or unload models; use the server's admin key."
    elif any(target.kind == "koboldcpp" for _, target in lanes):
        reason = "KoboldCpp can't unload its model over the API. Run it under llama-swap to share the GPU."
    else:
        reason = "No Writer or Agent endpoint is a llama-server router, llama-swap, Ollama, tabbyAPI, or text-generation-webui."
    return {
        "available": available,
        "lanes": [{"lane": lane, "kind": target.kind, "model": target.model} for lane, target in lanes],
        "reason": reason,
    }
