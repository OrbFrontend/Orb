"""HTTP client for the OpenAI-shaped `POST /v1/images/*` contract.

Mirrors `comfy_client.py`: one class, a `transport=` seam so every path is
testable under `httpx.MockTransport`, and **one error funnel** with a fixed
vocabulary so no server internal reaches the user.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .contracts import ImageGenerationError
from .display_encode import shrink_for_display
from .image_bytes import MAX_IMAGE_BYTES, image_mime

# Raised as an ordinary ImageGenerationError, but flagged so the adapter can tell
# "this model is gone" apart from every other 404 and degrade with disclosure.
MODEL_NOT_FOUND = "model_not_found"

_URL_RE = re.compile(r"https?://\S+")
_PATH_RE = re.compile(r"(?<![\w.])/[\w.\-/]+")
_WHITESPACE_RE = re.compile(r"\s+")
_EXCERPT_LIMIT = 200

# Words a provider uses when it objected to the *content*, not the request. For
# roleplay imagery on a commercial API this is the dominant failure mode, and the
# whole difference between "Orb is broken" and "the provider said no".
_MODERATION_MARKERS = (
    "moderation",
    "content_policy",
    "content policy",
    "safety",
    "safety_violation",
    "flagged",
    "blocked",
    "violat",
    "nsfw",
    "prohibited",
    "not allowed",
)


class CloudImageError(ImageGenerationError):
    """A provider failure, already sanitized, tagged with what kind it was."""

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class CloudImage:
    data: bytes
    mime: str
    # In the provider's own unit; None when it said nothing, since inventing a zero
    # would read as "this was free".
    cost: dict | None


def _scrub(text: str, secret: str = "") -> str:
    """A provider message with URLs, paths and the key removed, capped hard.

    Less opaque than ComfyUI's funnel, because provider 400s like *"Argument not
    supported: size"* are genuinely actionable. What must never survive is anything
    naming the server's internals -- or the credential.
    """
    if secret:
        text = text.replace(secret, "")
    text = _URL_RE.sub("", text)
    text = _PATH_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()[:_EXCERPT_LIMIT]


def _body_text(payload: Any) -> str:
    """The human half of an error body, whatever shape the provider chose."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            for key in ("message", "detail", "code", "type"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        elif isinstance(error, str) and error:
            return error
        for key in ("message", "detail", "code"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


class OpenAIImageClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        label: str = "the image provider",
        timeout: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.label = label
        self.timeout = timeout
        self.transport = transport

    def _http(self, timeout: float) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={**headers, "Content-Type": "application/json"},
            timeout=timeout,
            transport=self.transport,
        )

    # ── the one error funnel ──────────────────────────────────────────────────

    def _failure(self, status: int, payload: Any, *, model: str = "") -> CloudImageError:
        text = _body_text(payload)
        lowered = text.lower()
        if status in (401, 403):
            return CloudImageError(f"The API key for {self.label} was rejected", "auth")
        if any(marker in lowered for marker in _MODERATION_MARKERS):
            return CloudImageError(
                f"{self.label} refused this prompt under its content policy. "
                "Rewording the scene, or a different provider, is the only way through.",
                "moderation",
            )
        if status == 404:
            named = f" the model {model!r}" if model else " that model"
            return CloudImageError(f"{self.label} does not have{named}", MODEL_NOT_FOUND)
        if status == 429:
            return CloudImageError(f"{self.label} is rate-limiting this key; try again in a moment", "rate_limit")
        if status >= 500:
            return CloudImageError(f"Could not communicate with {self.label}", "server")
        excerpt = _scrub(text, self.api_key)
        # Everything else is a request the provider understood and rejected. The
        # excerpt earns its place here and nowhere else.
        return CloudImageError(
            f"{self.label} rejected the request: {excerpt}" if excerpt else f"{self.label} rejected the request",
            "request",
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        body: Mapping[str, Any] | None = None,
        model: str = "",
    ) -> Any:
        """One request, decoded, with every failure routed through `_failure`."""
        try:
            async with self._http(timeout) as client:
                response = await client.request(method, path, json=dict(body) if body is not None else None)
                if response.status_code >= 400:
                    try:
                        payload: Any = response.json()
                    except (json.JSONDecodeError, ValueError):
                        payload = response.text
                    raise self._failure(response.status_code, payload, model=model)
                return response.json()
        except ImageGenerationError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise CloudImageError(f"Could not communicate with {self.label}", "transport") from exc

    # ── model discovery ───────────────────────────────────────────────────────

    async def list_models(self, path: str, response_shape: str) -> list[str]:
        """The provider's model ids.

        The **only** endpoint `validate_connection` touches: a Test connection
        button that bills the user is unacceptable, so nothing here may reach the
        generations path.
        """
        decoded = await self._send("GET", path, timeout=min(30.0, self.timeout))
        # xAI's image-model endpoint answers `{"models": [...]}`; OpenAI answers
        # `{"data": [...]}`. Not interchangeable, so the preset says which.
        key = "models" if response_shape == "models_list" else "data"
        entries = decoded.get(key) if isinstance(decoded, Mapping) else None
        if not isinstance(entries, list):
            raise CloudImageError(f"{self.label} returned a malformed model list", "malformed")
        names = []
        for entry in entries:
            ident = entry.get("id") if isinstance(entry, Mapping) else entry
            if isinstance(ident, str) and ident and len(ident) <= 512:
                names.append(ident)
        return sorted(dict.fromkeys(names))

    # ── generation ────────────────────────────────────────────────────────────

    async def create_image(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        provider_id: str,
        timeout: float,
    ) -> CloudImage:
        """One synchronous generation call. No polling loop -- these APIs answer on
        the same request, so the adapter emits a single progress event at submit."""
        payload = await self._send("POST", path, timeout=timeout, body=body, model=str(body.get("model") or ""))
        if not isinstance(payload, Mapping):
            raise CloudImageError(f"{self.label} returned a malformed response", "malformed")
        entries = payload.get("data")
        entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], Mapping) else None
        if entry is None:
            raise CloudImageError(f"{self.label} returned no image", "malformed")
        data = await self._image_bytes(entry, timeout=timeout)
        try:
            mime = image_mime(data)
        except ImageGenerationError as exc:
            # Named, like every other failure here: "the backend returned junk" is
            # not actionable without knowing which backend.
            raise CloudImageError(f"{self.label} returned data that is not a supported image", "malformed") from exc
        # Full-res source -> capped WebP off-thread (CPU-bound) before it is stored.
        shrunk, mime = await asyncio.to_thread(shrink_for_display, data, mime)
        return CloudImage(data=shrunk, mime=mime, cost=_cost(payload, provider_id))

    async def _image_bytes(self, entry: Mapping[str, Any], *, timeout: float) -> bytes:
        encoded = entry.get("b64_json")
        if isinstance(encoded, str) and encoded:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise CloudImageError(f"{self.label} returned an unreadable image", "malformed") from exc
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise CloudImageError(f"{self.label} returned an image that is empty or too large", "malformed")
            return data
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise CloudImageError(f"{self.label} returned no image", "malformed")
        return await self._fetch(url, timeout=timeout)

    async def _fetch(self, url: str, *, timeout: float) -> bytes:
        """Download a hosted result, bounded by a *running* byte count.

        `b64_json` is preferred wherever supported -- one fewer hop, and nothing
        fetches an attacker-influenceable URL. When this path is taken: https only,
        and the cap is enforced while streaming rather than by trusting
        `content-length`, which the server is free to lie about.
        """
        if not url.lower().startswith("https://"):
            raise CloudImageError(f"{self.label} returned an image over an insecure URL", "insecure_url")
        chunks: list[bytes] = []
        total = 0
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise CloudImageError(f"{self.label} returned an image that is too large", "too_large")
                        chunks.append(chunk)
        except ImageGenerationError:
            raise
        except httpx.HTTPError as exc:
            raise CloudImageError(f"Could not fetch the generated image from {self.label}", "transport") from exc
        data = b"".join(chunks)
        if not data:
            raise CloudImageError(f"{self.label} returned an empty image", "malformed")
        return data


def _cost(payload: Mapping[str, Any], provider_id: str) -> dict | None:
    """What the response reports about cost, **in the provider's own unit**.

    xAI answers `usage.cost_in_usd_ticks` and nowhere states what a tick is worth,
    so renaming it `cost_usd` would pick a divisor by omission and print a wrong
    billing figure. The unit travels with the value and the frontend renders only
    what it can name; a verified divisor later is a one-line change.
    """
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    for field, unit in (("cost_in_usd_ticks", "usd_ticks"), ("total_cost", "usd"), ("cost", "usd")):
        value = usage.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {"provider": provider_id, "unit": unit, "value": value}
    return None
