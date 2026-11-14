"""Bridge the TTS workflow to the built-in ZONOS3 server; also the Local ML management hooks of both ZONOS3 features."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ..core.settings import Settings
from ..database import get_settings, set_local_ml_config
from ..inference.local_models import zonos_server
from ..inference.local_models.zonos_server import binary
from .errors import WorkflowUnavailableError

FEATURE = zonos_server.FEATURE
FEATURE_REFERENCE = zonos_server.FEATURE_REFERENCE


def use_gpu(settings: Settings) -> bool:
    """Whether the server is asked to run on the GPU. Defaults on."""
    configs = settings.get("local_ml_config")
    stored = configs.get(FEATURE) if isinstance(configs, Mapping) else None
    return bool(stored.get("gpu", True)) if isinstance(stored, Mapping) else True


def ready(settings: Settings, *, enroll: bool = False) -> tuple[bool, str]:
    """Whether the built-in server can speak, or with *enroll* read a clip, and why not."""
    enabled = settings.get("local_ml_enabled")
    enabled = enabled if isinstance(enabled, Mapping) else {}
    for feature, name in ((FEATURE, "voice model"), (FEATURE_REFERENCE, "reference reader"))[: 2 if enroll else 1]:
        if enabled.get(feature, True) is False:
            return False, f"The ZONOS3 {name} is switched off. Turn it on in TTS settings."
    return zonos_server.ready(enroll=enroll)


@contextlib.asynccontextmanager
async def server(settings: Settings, *, enroll: bool = False) -> AsyncIterator[str]:
    """Yield the built-in server's base URL and keep it running for the block.

    Raises ``WorkflowUnavailableError`` with a message for the user when the model is not set up or the server will not start.
    """
    ok, reason = ready(settings, enroll=enroll)
    if not ok:
        raise WorkflowUnavailableError(reason)
    try:
        async with zonos_server.HOST.use(zonos_server.launch(gpu=use_gpu(settings))) as running:
            yield running.url
    except (zonos_server.ZonosServerFailed, zonos_server.ZonosServerMissing) as exc:
        raise WorkflowUnavailableError(str(exc)) from exc


async def status_extra(settings: Settings) -> dict:
    host = zonos_server.HOST
    return {
        "gpu": use_gpu(settings),
        "gpu_capable": binary.gpu_capable(),
        "state": host.state,
        "error": host.error,
        "runtime_mb": binary.ARCHIVE_MB.get(binary.release_archive.system(), 0),
    }


async def apply_config(body: Mapping[str, Any]) -> dict:
    await set_local_ml_config(FEATURE, {"gpu": bool(body.get("gpu", True))})
    return (await get_settings()).get("local_ml_config", {})


async def on_enabled(enabled: bool) -> None:
    if not enabled:
        await zonos_server.HOST.release()


def model_deletion() -> contextlib.AbstractAsyncContextManager[None]:
    return zonos_server.HOST.exclusive_release()
