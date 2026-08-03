"""Small client for ComfyUI's documented HTTP queue/history/view contract."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx

from ..config import MIME_EXTENSIONS
from .contracts import ImageGenerationError, ImageResult, ProgressCallback, emit
from .display_encode import shrink_for_display
from .image_bytes import MAX_IMAGE_BYTES, image_mime

REFERENCE_SUBFOLDER = "orb"

_OBJECT_INFO_TTL = 60.0
_OBJECT_INFO_MAX_ENTRIES = 8
_object_info_cache: dict[str, tuple[float, dict]] = {}


def invalidate_object_info(api_url: str | None = None) -> None:
    """Drop cached node information, for one server or all of them."""
    if api_url is None:
        _object_info_cache.clear()
    else:
        _object_info_cache.pop(api_url.rstrip("/"), None)


def _validation_message(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return "ComfyUI rejected the workflow"
    error = payload.get("error")
    error_type = error.get("type") if isinstance(error, Mapping) else None
    node_errors = payload.get("node_errors")
    input_name = None
    if isinstance(node_errors, Mapping):
        for value in node_errors.values():
            errors = value.get("errors") if isinstance(value, Mapping) else None
            for item in errors if isinstance(errors, list) else []:
                if isinstance(item, Mapping):
                    details = item.get("details")
                    if isinstance(details, str):
                        input_name = details
                    extra = item.get("extra_info")
                    if isinstance(extra, Mapping) and isinstance(extra.get("input_name"), str):
                        input_name = extra["input_name"]
                    if input_name:
                        break
            if input_name:
                break
    if input_name == "ckpt_name":
        return "The selected checkpoint is no longer on the ComfyUI server"
    if error_type:
        return f"ComfyUI rejected the workflow ({error_type})"
    return "ComfyUI rejected the workflow"


class ComfyClient:
    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.transport = transport

    def _http(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.api_url, headers=self.headers, timeout=timeout, transport=self.transport)

    async def _json(self, method: str, path: str, *, timeout: float = 15.0, json_body: Any = None) -> Any:
        try:
            async with self._http(timeout) as client:
                response = await client.request(method, path, json=json_body)
                if response.status_code >= 400:
                    try:
                        payload = response.json()
                    except (json.JSONDecodeError, ValueError):
                        payload = None
                    raise ImageGenerationError(_validation_message(payload))
                return response.json()
        except ImageGenerationError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageGenerationError("Could not communicate with ComfyUI") from exc

    async def system_stats(self) -> dict:
        result = await self._json("GET", "/system_stats")
        if not isinstance(result, dict):
            raise ImageGenerationError("ComfyUI returned malformed system information")
        return result

    async def object_info(self, *, allow_cached: bool = False) -> dict:
        """The server's node catalogue. Cached for `_OBJECT_INFO_TTL` seconds.

        A fresh fetch always refreshes the cache, so `allow_cached=False` is
        both "give me the current truth" and "make the next probe cheap".
        """
        now = time.monotonic()
        if allow_cached:
            cached = _object_info_cache.get(self.api_url)
            if cached and cached[0] > now:
                return cached[1]
        result = await self._json("GET", "/object_info", timeout=30.0)
        if not isinstance(result, dict):
            raise ImageGenerationError("ComfyUI returned malformed node information")
        if len(_object_info_cache) >= _OBJECT_INFO_MAX_ENTRIES:
            _object_info_cache.clear()
        _object_info_cache[self.api_url] = (now + _OBJECT_INFO_TTL, result)
        return result

    async def upload_image(
        self,
        data: bytes,
        mime: str,
        *,
        digest: str,
        timeout: float = 60.0,
        progress: ProgressCallback | None = None,
    ) -> str:
        """Upload one reference image and return the widget value for `LoadImage`.

        ``/upload/image`` takes multipart ``image``/``subfolder``/``type``/``overwrite``
        and answers ``{name, subfolder, type}``; a bare ``"<subfolder>/<name>"`` is what
        ``folder_paths.get_annotated_filepath`` resolves under the input directory, so
        that is what the widget carries. The name is content-addressed off `digest`, so
        repeat renders overwrite one file and a reroll resolves to the same name.
        """
        name = f"orb_{digest[:16]}.{MIME_EXTENSIONS.get(mime, 'png')}"
        await emit(progress, "uploading", {"name": name, "bytes": len(data)})
        try:
            async with self._http(timeout) as client:
                response = await client.post(
                    "/upload/image",
                    files={"image": (name, data, mime or "application/octet-stream")},
                    data={"subfolder": REFERENCE_SUBFOLDER, "type": "input", "overwrite": "true"},
                )
                if response.status_code >= 400:
                    raise ImageGenerationError("ComfyUI rejected the reference image upload")
                payload = response.json()
        except ImageGenerationError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise ImageGenerationError("Could not upload the reference image to ComfyUI") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("name"), str):
            raise ImageGenerationError("ComfyUI did not confirm the reference image upload")
        subfolder = payload.get("subfolder")
        stored = payload["name"]
        return f"{subfolder}/{stored}" if isinstance(subfolder, str) and subfolder else stored

    async def models(self, folder: str = "checkpoints") -> list[str]:
        result = await self._json("GET", f"/models/{folder}")
        if not isinstance(result, list):
            raise ImageGenerationError("ComfyUI returned a malformed model list")
        return sorted(x for x in result if isinstance(x, str) and len(x) <= 512)

    async def queue_ahead(self, number: Any, *, timeout: float = 10.0) -> int | None:
        """How many renders sit ahead of queue entry `number`, or None if unknown.

        A shared server may hold this job behind other clients' renders, and
        "queued behind 2" is the difference between *broken* and *waiting*.
        Position is informational, so every failure mode — an unreachable
        server, a malformed body, an older build without /queue — reports None
        and lets the render proceed rather than raising.
        """
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        try:
            payload = await self._json("GET", "/queue", timeout=timeout)
        except ImageGenerationError:
            return None
        if not isinstance(payload, Mapping):
            return None
        ahead = 0
        for key in ("queue_running", "queue_pending"):
            entries = payload.get(key)
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, (list, tuple)) or not entry:
                    continue
                position = entry[0]
                if isinstance(position, int) and not isinstance(position, bool) and position < number:
                    ahead += 1
        return ahead

    async def generate(
        self,
        graph: dict,
        output_node: str,
        *,
        timeout_seconds: float,
        progress: ProgressCallback | None = None,
    ) -> ImageResult:
        submitted = await self._json(
            "POST",
            "/prompt",
            timeout=min(30.0, timeout_seconds),
            json_body={"prompt": graph, "client_id": uuid.uuid4().hex},
        )
        if not isinstance(submitted, Mapping) or not isinstance(submitted.get("prompt_id"), str):
            raise ImageGenerationError("ComfyUI did not return a prompt id")
        prompt_id = submitted["prompt_id"]
        number = submitted.get("number")
        queue_timeout = min(10.0, timeout_seconds)
        ahead = await self.queue_ahead(number, timeout=queue_timeout)
        waiting = bool(ahead)
        await emit(progress, "queued" if waiting else "rendering", {"number": number, "ahead": ahead})

        deadline = time.monotonic() + timeout_seconds
        record: Mapping[str, Any] | None = None
        while time.monotonic() < deadline:
            history = await self._json("GET", f"/history/{prompt_id}", timeout=min(15.0, timeout_seconds))
            item = history.get(prompt_id) if isinstance(history, Mapping) else None
            if isinstance(item, Mapping):
                status = item.get("status")
                if isinstance(status, Mapping) and status.get("completed"):
                    record = item
                    break
                if isinstance(status, Mapping) and status.get("status_str") == "error":
                    raise ImageGenerationError("ComfyUI could not complete the image")
            if waiting:
                previous, ahead = ahead, await self.queue_ahead(number, timeout=queue_timeout)
                waiting = bool(ahead)
                if not waiting:
                    await emit(progress, "rendering", {"number": number, "ahead": ahead})
                elif ahead != previous:
                    await emit(progress, "queued", {"number": number, "ahead": ahead})
            await asyncio.sleep(1.0)
        if record is None:
            raise ImageGenerationError("Image generation timed out")
        outputs = record.get("outputs")
        output = outputs.get(output_node) if isinstance(outputs, Mapping) else None
        images = output.get("images") if isinstance(output, Mapping) else None
        image = images[0] if isinstance(images, list) and images and isinstance(images[0], Mapping) else None
        if not image or not all(isinstance(image.get(k), str) for k in ("filename", "subfolder", "type")):
            raise ImageGenerationError("ComfyUI completed without the configured image output")
        try:
            async with self._http(min(60.0, timeout_seconds)) as client:
                response = await client.get(
                    "/view",
                    params={k: image[k] for k in ("filename", "subfolder", "type")},
                )
                response.raise_for_status()
                data = response.content
                if not data or len(data) > MAX_IMAGE_BYTES:
                    raise ImageGenerationError("ComfyUI image output is empty or too large")
                mime = image_mime(data)
        except ImageGenerationError:
            raise
        except httpx.HTTPError as exc:
            raise ImageGenerationError("Could not fetch the generated image from ComfyUI") from exc
        data, mime = await asyncio.to_thread(shrink_for_display, data, mime)
        return ImageResult(
            image_bytes=data,
            mime=mime,
            backend_info={"prompt_id": prompt_id, "output_node": output_node},
        )
