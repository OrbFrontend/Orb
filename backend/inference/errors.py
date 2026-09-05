"""Preserve useful provider error messages for LLM calls."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

# Long enough for a full provider sentence, short enough that a stack trace or an
# HTML error page pasted into a body cannot become the headline.
SENTENCE_LIMIT = 300

# The whole body is kept for the Details pane, but a provider that answers a 400
# with a megabyte of HTML must not be allowed to bloat every SSE frame.
BODY_LIMIT = 20_000

# Below this a "secret" is more likely a placeholder than a credential, and
# blanking it would shred unrelated text (a 1-char key would redact every match).
_MIN_SECRET_LEN = 4

_WHITESPACE_RE = re.compile(r"\s+")

# How many nested gateway envelopes to unwrap. One gateway in front of one
# provider is the shape that exists; the cap is only so a body that quotes itself
# cannot recurse without end.
_MAX_UNWRAP = 3


class LLMCallError(httpx.HTTPStatusError):
    """A provider rejection with the provider's own words kept alongside the status.

    ``sentence`` is the one line worth putting in front of a user; ``body`` is the
    whole response, credential removed, for the case where the sentence is not
    enough. Both are already sanitized -- a consumer may render them without
    re-checking.
    """

    stream_event = False

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        sentence: str,
        body: str,
        host: str,
        model: str,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.sentence = sentence
        self.body = body
        self.host = host
        self.model = model


def redact(text: str, secret: str) -> str:
    """*text* with the API key removed, and nothing else removed.

    URLs and filesystem paths stay: Orb runs on the user's own machine, so there is
    no third party to leak them to, and the endpoint URL is usually what explains a
    404. The credential is the one token that is never diagnostic.
    """
    if not secret or len(secret) < _MIN_SECRET_LEN:
        return text
    return text.replace(secret, "[redacted]")


def _walk_strings(payload: Any, *, limit: int = 6, budget: int = 200) -> str:
    """Every human-looking string in a body, outermost first.

    The fallback for a shape nobody enumerated, and the reason the well-known keys
    in :func:`provider_sentence` can stay a short list instead of growing a row per
    provider. Breadth first, because the outer strings are the summary and the inner
    ones the particulars.
    """
    found: list[str] = []
    queue: list[Any] = [payload]
    visited = 0
    while queue and len(found) < limit and visited < budget:
        node = queue.pop(0)
        visited += 1
        if isinstance(node, str):
            text = node.strip()
            if text and len(text) <= SENTENCE_LIMIT:
                found.append(text)
        elif isinstance(node, Mapping):
            queue.extend(node.values())
        elif isinstance(node, (list, tuple)):
            queue.extend(node)
    return "; ".join(dict.fromkeys(found))


def _first_string(value: Any) -> str:
    """A string, or the first string inside a list of them (FastAPI ``detail``)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [p for p in (_first_string(v) for v in value) if p]
        if parts:
            return "; ".join(dict.fromkeys(parts))
    if isinstance(value, Mapping):
        for key in ("msg", "message", "detail"):
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got
    return ""


def provider_sentence(body: str, _depth: int = 0) -> str:
    """Extract one sanitized sentence from a provider error body."""
    text = body.strip()
    if not text:
        return ""
    try:
        payload: Any = json.loads(text)
    except (ValueError, TypeError):
        return _WHITESPACE_RE.sub(" ", text).strip()[:SENTENCE_LIMIT]

    found = ""
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            metadata = error.get("metadata")
            if isinstance(metadata, Mapping) and _depth < _MAX_UNWRAP:
                raw = _first_string(metadata.get("raw"))
                if raw:
                    found = provider_sentence(raw, _depth + 1)
            if not found:
                found = _first_string(error.get("message"))
            if not found:
                for key in ("detail", "code", "type"):
                    found = _first_string(error.get(key))
                    if found:
                        break
        elif isinstance(error, str):
            found = error
        if not found:
            for key in ("message", "detail"):
                found = _first_string(payload.get(key))
                if found:
                    break
    elif isinstance(payload, str):
        found = payload

    if not found:
        found = _walk_strings(payload)
    return _WHITESPACE_RE.sub(" ", found).strip()[:SENTENCE_LIMIT]


def llm_call_error(
    *,
    response: httpx.Response,
    body: str,
    url: str,
    model: str,
    api_key: str,
) -> LLMCallError:
    """Build the typed rejection from what the transport already holds.

    *body* is the text ``_read_error_body`` already read and logged; passing it in
    rather than re-reading matters because a streaming response can only be read
    once.

    ``Response.request`` raises ``RuntimeError`` when the response was constructed
    without one, so it is never read bare — a synthesized request from *url* keeps
    the exception well-formed for ``RetryPolicy`` either way.
    """
    clean = redact(body, api_key)[:BODY_LIMIT]
    sentence = redact(provider_sentence(body), api_key)[:SENTENCE_LIMIT]
    host = urlsplit(url).netloc
    detail = f": {sentence}" if sentence else ""
    try:
        request = response.request
    except (RuntimeError, AttributeError):
        request = httpx.Request("POST", url)
    return LLMCallError(
        f"HTTP {response.status_code} from {host}{detail}",
        request=request,
        response=response,
        sentence=sentence,
        body=clean,
        host=host,
        model=model,
    )


def llm_stream_error(*, payload: object, url: str, model: str, api_key: str) -> LLMCallError:
    """Build a normal sanitized provider error for an SSE error event.

    Anthropic can report a terminal provider error inside an HTTP-200 stream.
    A synthetic 502 response represents that upstream failure without teaching
    pipeline failure rendering a second exception shape.
    """
    body = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    request = httpx.Request("POST", url)
    response = httpx.Response(502, request=request)
    error = llm_call_error(response=response, body=body, url=url, model=model, api_key=api_key)
    error.stream_event = True
    return error
