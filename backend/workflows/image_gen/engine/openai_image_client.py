"""HTTP client for the OpenAI-shaped `POST /v1/images/*` contract.

Mirrors `comfy_client.py`: one class, a `transport=` seam so every path is
testable under `httpx.MockTransport`, and **one error funnel** so no server
internal reaches the user.

**The funnel classifies only what changes Orb's behaviour, and never replaces what
the provider said.** There is exactly one branch a caller acts on --
`MODEL_NOT_FOUND`, which drives the re-render in the adapter. Every other branch
picks a sentence to put *in front of* the provider's own words; none of them may
swallow them. Classifying further does not scale: a marker list over provider prose
has to be right about vocabulary no two providers share, and when it is wrong it
replaces an actionable message with a confident wrong one. Guessing less and
quoting more is the trade this module makes on purpose.
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .contracts import ImageGenerationError
from .display_encode import shrink_for_display
from .image_bytes import MAX_IMAGE_BYTES, image_mime

# Raised as an ordinary ImageGenerationError, but flagged so the adapter can tell
# "this model is gone" apart from every other failure and degrade with disclosure.
MODEL_NOT_FOUND = "model_not_found"

# What a provider says when the model id is not one it has. Matched in *addition* to
# a 404, because the status code is not agreed on: xAI answers 404, NanoGPT answers
# 400 with `{"code": "invalid_model"}`. Without this the degrade path -- re-render on
# the configured model and say so -- never fires on half the providers, and a
# rehydrate of an image whose model was retired dies as a generic rejection.
_UNKNOWN_MODEL_MARKERS = (
    "invalid_model",
    "model_not_found",
    "unknown model",
    "invalid image model",
    "no such model",
)

_URL_RE = re.compile(r"https?://\S+")
_PATH_RE = re.compile(r"(?<![\w.])/[\w.\-/]+")
_WHITESPACE_RE = re.compile(r"\s+")
_EXCERPT_LIMIT = 200

# There is deliberately no content-refusal branch. Markers like "blocked" and "not
# allowed" are ordinary API-error English -- *"Model gpt-image-1 is not allowed for
# your account tier"* was reported as a content refusal -- and a provider refusing
# on content says so in its own words anyway. See
# `test_ordinary_api_english_is_not_read_as_a_content_refusal`.


class CloudImageError(ImageGenerationError):
    """A provider failure, already sanitized, tagged with what kind it was.

    `kind` is a hint, never a substitute for the message: only `MODEL_NOT_FOUND` is
    branched on, and the rest exist so a later caller can act without the funnel
    having to hide anything to make room.
    """

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


def _say(named: str, status: int, excerpt: str) -> str:
    """What Orb can name, the status, and whatever the provider said about it."""
    return f"{named} (HTTP {status}): {excerpt}" if excerpt else f"{named} (HTTP {status})"


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


def _string_leaves(payload: Any, *, limit: int = 6, budget: int = 200) -> str:
    """Every human-looking string in a body, outermost first.

    The fallback for a shape nobody enumerated, and the reason the well-known keys
    below can stay a short list instead of growing a row per provider. Breadth
    first, because the outer strings are the summary and the inner ones the
    particulars: OpenRouter buries its reason in `error.metadata.raw` and anything
    FastAPI-shaped puts it in a `detail` *list*, both of which used to reach the
    user as a bare "rejected the request" with nothing else in it.
    """
    found: list[str] = []
    queue: list[Any] = [payload]
    visited = 0
    while queue and len(found) < limit and visited < budget:
        node = queue.pop(0)
        visited += 1
        if isinstance(node, str):
            text = node.strip()
            # Long enough to be a payload rather than a sentence: an error body is
            # not supposed to carry one, but `_scrub`'s cap is not a reason to let
            # a base64 blob crowd out the message.
            if text and len(text) <= 300:
                found.append(text)
        elif isinstance(node, Mapping):
            queue.extend(node.values())
        elif isinstance(node, (list, tuple)):
            queue.extend(node)
    return "; ".join(dict.fromkeys(found))


def _body_text(payload: Any) -> str:
    """The human half of an error body, whatever shape the provider chose.

    The well-known keys first, because they yield the clean single sentence; the
    generic walk only when they find nothing, because a walk over a body that *has*
    an `error.message` drags its sibling codes along with it.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            # `type` is read here but never at the top level, where it names the
            # *category* that failed ("invalid_request_error") rather than the fault.
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
    return _string_leaves(payload)


def _upstream(payload: Any) -> str:
    """Which upstream a *broker* is relaying a refusal from, when it names one.

    OpenRouter routes one model id to several providers and puts whichever answered
    in `error.metadata.provider_name`. Worth carrying in front of the message: *"User
    location is not supported for the API use"* reads as Orb or OpenRouter refusing
    the user until you know Google AI Studio said it -- and that the same catalogue
    holds models that route elsewhere and work from here. Without it the one
    actionable fact in the response is the one fact dropped.
    """
    error = payload.get("error") if isinstance(payload, Mapping) else None
    metadata = error.get("metadata") if isinstance(error, Mapping) else None
    name = metadata.get("provider_name") if isinstance(metadata, Mapping) else None
    return name if isinstance(name, str) and name else ""


def _body_codes(payload: Any) -> str:
    """Every machine-readable code in an error body, lowercased and joined.

    Separate from `_body_text` because the two answer different questions: the text
    is what the user is shown, the codes are what the funnel branches on. NanoGPT
    puts `invalid_model` in both `error.code` and a sibling top-level `code`, and
    reading only the human message would leave the branch resting on prose.
    """
    if not isinstance(payload, Mapping):
        return ""
    return " ".join(
        value.lower()
        for source in (payload, payload.get("error"))
        if isinstance(source, Mapping)
        for key in ("code", "type")
        if isinstance(value := source.get(key), str) and value
    )


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

    def _bad(self, said: str, kind: str = "malformed") -> CloudImageError:
        """Every failure that is not a provider rejection, named with the provider.

        "the backend returned junk" is not actionable without knowing which backend,
        and there are a dozen of these -- one constructor keeps them from drifting
        into a dozen spellings of the same sentence.
        """
        return CloudImageError(f"{self.label} {said}", kind)

    def _failure(self, status: int, payload: Any, *, model: str = "") -> CloudImageError:
        """One provider rejection, named as far as Orb can name it and quoted the
        rest of the way.

        The status code rides every message. It costs nothing, it means the same
        thing on every provider, and it is what lets a user tell "out of credits"
        (402) from "model gone" (404) without Orb having to recognise either.
        """
        text = _body_text(payload)
        lowered = text.lower()
        codes = _body_codes(payload)
        upstream = _upstream(payload)
        if upstream and upstream.lower() not in lowered:
            # In front of the provider's words, never instead of them, and only when
            # the words do not already name it -- so a broker that says "Google AI
            # Studio returned an error" is not made to say it twice.
            text = f"{upstream}: {text}"
        excerpt = _scrub(text, self.api_key)
        if status in (401, 403):
            # 403 is the loose one -- some providers spend it on content refusals and
            # plan restrictions rather than the key. The excerpt is what keeps that
            # from being hidden behind a guess about which.
            return CloudImageError(_say(f"The API key for {self.label} was rejected", status, excerpt), "auth")
        # The one branch a caller acts on, and not gated on 404: the providers
        # disagree about the status, so the code and the message decide. Carries the
        # excerpt like the rest, because a wrong base URL 404s every path and the
        # provider's own words are what distinguish that from a retired model.
        if status == 404 or any(marker in codes or marker in lowered for marker in _UNKNOWN_MODEL_MARKERS):
            named = f" the model {model!r}" if model else " that model"
            return CloudImageError(_say(f"{self.label} does not have{named}", status, excerpt), MODEL_NOT_FOUND)
        if status >= 500:
            # Not "could not communicate": the exchange succeeded and the provider
            # answered with its own failure, which it often explains.
            return CloudImageError(_say(f"{self.label} failed to render this request", status, excerpt), "server")
        # Everything else the provider understood and rejected. 429 keeps a `kind` of
        # its own but no bespoke sentence: it is not always a per-minute rate limit,
        # and "try again in a moment" was the wrong advice for a key out of quota.
        return CloudImageError(
            _say(f"{self.label} rejected the request", status, excerpt),
            "rate_limit" if status == 429 else "request",
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
                    except ValueError:
                        payload = response.text
                    raise self._failure(response.status_code, payload, model=model)
                try:
                    return response.json()
                except ValueError as exc:
                    # A 200 that is not JSON is a proxy or captive portal answering
                    # in the provider's place. Reporting it as "could not
                    # communicate" sent the user to look at their network for a
                    # request that completed.
                    raise self._bad(f"returned a malformed response (HTTP {response.status_code})") from exc
        except ImageGenerationError:
            raise
        except httpx.TimeoutException as exc:
            # Named apart from every other transport failure: on a render this is the
            # *expected* one, and "could not communicate" reads as a broken network
            # rather than a provider that is simply slower than the timeout.
            raise self._bad(f"did not respond within {timeout:.0f}s", "timeout") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudImageError(f"Could not communicate with {self.label}", "transport") from exc

    # ── model discovery ───────────────────────────────────────────────────────

    async def list_models(self, path: str, response_shape: str, model_filter: str = "") -> list[str]:
        """The provider's model ids, narrowed to the ones that make images.

        The **only** endpoint `validate_connection` touches, and nothing here may
        reach the generations path -- see `validate_connection` on ImageAdapter.
        """
        decoded = await self._send("GET", path, timeout=min(30.0, self.timeout))
        # Which shape each provider speaks is declared on its preset -- see
        # `models_response` in providers.py. Reading the wrong one yields an empty
        # list, which reads to the user as "this key has no models" rather than as
        # a parser pointed at the wrong key, so a mismatch is malformed, not empty.
        if response_shape == "bare_list":
            entries: Any = decoded
        elif response_shape == "nanogpt_image_map":
            catalogue = decoded.get("models") if isinstance(decoded, Mapping) else None
            images = catalogue.get("image") if isinstance(catalogue, Mapping) else None
            # The ids are the *keys*; the values are per-model capability records Orb
            # does not read. A mapping, so `_model_ids` gets the list it expects.
            entries = list(images) if isinstance(images, Mapping) else None
        else:
            key = "models" if response_shape == "models_list" else "data"
            entries = decoded.get(key) if isinstance(decoded, Mapping) else None
        if not isinstance(entries, list):
            raise self._bad("returned a malformed model list")
        names = _model_ids(entries, model_filter)
        if not names and model_filter:
            # The filter is an optimisation, never a gate: a provider that stops
            # tagging its entries should cost the user a longer list, not an empty
            # picker that reads as "this key has no models".
            names = _model_ids(entries, "")
        return names

    async def verify_key(self, path: str) -> None:
        """Prove the key is accepted, on a free endpoint, or raise.

        Only called where the preset declares one, because for most providers the
        model list already answers it. NanoGPT serves its catalogue to anonymous
        callers, so without this a Test connection reports "Connected" for a key
        that will 401 on the first render the user pays to discover.
        """
        await self._send("GET", path, timeout=min(30.0, self.timeout))

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
            raise self._bad("returned a malformed response")
        entries = payload.get("data")
        entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], Mapping) else None
        if entry is None:
            raise self._bad("returned no image")
        data = await self._image_bytes(entry, timeout=timeout)
        try:
            mime = image_mime(data)
        except ImageGenerationError as exc:
            raise self._bad("returned data that is not a supported image") from exc
        # Full-res source -> capped WebP off-thread (CPU-bound) before it is stored.
        shrunk, mime = await asyncio.to_thread(shrink_for_display, data, mime)
        return CloudImage(data=shrunk, mime=mime, cost=_cost(payload, provider_id))

    async def _image_bytes(self, entry: Mapping[str, Any], *, timeout: float) -> bytes:
        encoded = entry.get("b64_json")
        if isinstance(encoded, str) and encoded:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise self._bad("returned an unreadable image") from exc
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise self._bad("returned an image that is empty or too large")
            return data
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise self._bad("returned no image")
        return await self._fetch(url, timeout=timeout)

    async def _fetch(self, url: str, *, timeout: float) -> bytes:
        """Download a hosted result, bounded by a *running* byte count.

        `b64_json` is preferred wherever supported -- one fewer hop, and nothing
        fetches an attacker-influenceable URL. When this path is taken: https only,
        and the cap is enforced while streaming rather than by trusting
        `content-length`, which the server is free to lie about.
        """
        if not url.lower().startswith("https://"):
            raise self._bad("returned an image over an insecure URL", "insecure_url")
        chunks: list[bytes] = []
        total = 0
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise self._bad("returned an image that is too large", "too_large")
                        chunks.append(chunk)
        except ImageGenerationError:
            raise
        except httpx.TimeoutException as exc:
            raise self._bad(f"did not send the generated image within {timeout:.0f}s", "timeout") from exc
        except httpx.HTTPError as exc:
            raise CloudImageError(f"Could not fetch the generated image from {self.label}", "transport") from exc
        data = b"".join(chunks)
        if not data:
            raise self._bad("returned an empty image")
        return data


def _declares_image_type(entry: Mapping[str, Any]) -> bool:
    """Together: every catalogue entry carries a `type`."""
    return entry.get("type") == "image"


def _outputs_an_image(entry: Mapping[str, Any]) -> bool:
    """OpenRouter: no `type` anywhere, but each entry declares its modalities.

    Read from `output_modalities`, never `modality` or `input_modalities` -- an
    image model's inputs say what it can be *shown*, and every text model that can
    read a picture matches on those.
    """
    architecture = entry.get("architecture")
    modalities = architecture.get("output_modalities") if isinstance(architecture, Mapping) else None
    return isinstance(modalities, (list, tuple)) and "image" in modalities


# Which rule a provider uses is declared on its preset -- see `models_filter` in
# providers.py. An unknown name filters nothing, so a preset typo costs a longer
# picker rather than an empty one, matching the fallback in `list_models`.
_MODEL_FILTERS: dict[str, Callable[[Mapping[str, Any]], bool]] = {
    "type_image": _declares_image_type,
    "output_image": _outputs_an_image,
}


def _model_ids(entries: list[Any], model_filter: str) -> list[str]:
    """The `id` of every entry, de-duplicated and sorted, optionally image-only."""
    keep = _MODEL_FILTERS.get(model_filter)
    names = []
    for entry in entries:
        if keep is not None and not (isinstance(entry, Mapping) and keep(entry)):
            continue
        ident = entry.get("id") if isinstance(entry, Mapping) else entry
        if isinstance(ident, str) and ident and len(ident) <= 512:
            names.append(ident)
    return sorted(dict.fromkeys(names))


def _cost(payload: Mapping[str, Any], provider_id: str) -> dict | None:
    """What the response reports about cost, **in the provider's own unit**.

    xAI answers `usage.cost_in_usd_ticks` and nowhere states what a tick is worth,
    so renaming it `cost_usd` would pick a divisor by omission and print a wrong
    billing figure. The unit travels with the value and the frontend renders only
    what it can name; a verified divisor later is a one-line change.

    `usage` is searched first and the top level second, because a provider that
    reports both means the nested one -- NanoGPT reports only a top-level `cost`, in
    plain USD, and reading `usage` alone showed no cost row on a render that had one.
    """
    reported = payload.get("usage")
    usage: Mapping[str, Any] = reported if isinstance(reported, Mapping) else {}
    for source, field, unit in (
        (usage, "cost_in_usd_ticks", "usd_ticks"),
        (usage, "total_cost", "usd"),
        (usage, "cost", "usd"),
        (payload, "cost", "usd"),
    ):
        value = source.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {"provider": provider_id, "unit": unit, "value": value}
    return None
