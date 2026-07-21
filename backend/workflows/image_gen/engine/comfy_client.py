"""Small client for ComfyUI's documented HTTP queue/history/view contract."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

from .contracts import ImageGenerationError, ImageResult

MAX_IMAGE_BYTES = 20 * 1024 * 1024
ProgressCallback = Callable[[str, Mapping[str, Any]], Optional[Awaitable[None]]]


async def _emit(progress: "ProgressCallback | None", stage: str, detail: Mapping[str, Any]) -> None:
    if not progress:
        return
    maybe = progress(stage, detail)
    if maybe is not None:
        await maybe


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


def _image_mime(data: bytes, content_type: str = "") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageGenerationError("ComfyUI returned data that is not a supported image")


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

    async def _json(self, method: str, path: str, *, timeout: float = 15.0, json_body: Any = None) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.api_url,
                headers=self.headers,
                timeout=timeout,
                transport=self.transport,
            ) as client:
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

    async def object_info(self) -> dict:
        result = await self._json("GET", "/object_info", timeout=30.0)
        if not isinstance(result, dict):
            raise ImageGenerationError("ComfyUI returned malformed node information")
        return result

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
                # Entries are [number, prompt_id, prompt, extra_data, outputs].
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
        # An unknown position (None) reads as "not waiting on anyone": the render
        # is under way as far as the caller can tell, which is the honest label.
        waiting = bool(ahead)
        await _emit(progress, "queued" if waiting else "rendering", {"number": number, "ahead": ahead})

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
            # Re-poll the queue only while something is still ahead; once this
            # job reaches the front the extra request per second stops.
            if waiting:
                previous, ahead = ahead, await self.queue_ahead(number, timeout=queue_timeout)
                waiting = bool(ahead)
                if not waiting:
                    await _emit(progress, "rendering", {"number": number, "ahead": ahead})
                elif ahead != previous:
                    await _emit(progress, "queued", {"number": number, "ahead": ahead})
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
            async with httpx.AsyncClient(
                base_url=self.api_url,
                headers=self.headers,
                timeout=min(60.0, timeout_seconds),
                transport=self.transport,
            ) as client:
                response = await client.get(
                    "/view",
                    params={k: image[k] for k in ("filename", "subfolder", "type")},
                )
                response.raise_for_status()
                data = response.content
                if not data or len(data) > MAX_IMAGE_BYTES:
                    raise ImageGenerationError("ComfyUI image output is empty or too large")
                mime = _image_mime(data, response.headers.get("content-type", ""))
        except ImageGenerationError:
            raise
        except httpx.HTTPError as exc:
            raise ImageGenerationError("Could not fetch the generated image from ComfyUI") from exc
        return ImageResult(
            image_bytes=data,
            mime=mime,
            backend_info={"prompt_id": prompt_id, "output_node": output_node},
        )
