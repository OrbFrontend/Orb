"""Google AI Studio: the ``/v1beta/openai`` compatibility layer and the ``/v1beta/decisions`` Judge.

Identified by resource shape (a ``v1beta`` or ``v1alpha`` segment followed by ``openai``, or ending a route with ``decisions``),
so any proxy exposing the same paths gets the same requests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .base import DeltaReader, Dialect, ModelProfile, path_segments

VERSIONS = ("v1beta", "v1alpha")
OPENAI_BASE = "/v1beta/openai"

# The one name-based default: a configured URL that is the bare host has no shape to read, and Google serves its decision
# models under the first version.
_HOST = "generativelanguage.googleapis.com"

# Chat-style suffixes trimmed from a configured URL before deriving its decisions route.
_CHAT_SUFFIXES = ("chat", "completions", "v1", "responses", "alpha", "openai", "models")

# Orb's other reasoning dialects. The compatibility layer refuses each by name ('Unknown name "thinking": Cannot find field.'),
# so sending them costs one rejection per field per model before the refusal is learned.
_UNREAD_REASONING_FIELDS = ("reasoning", "chat_template_kwargs", "thinking")

# It takes ordinary OpenAI request fields, refuses unknown ones by name, and honors strict json_schema output.
_PROFILE = ModelProfile(structured_tool_calls=True)


def _follows_version(segments: Sequence[str], name: str) -> bool:
    return any(version in VERSIONS and after == name for version, after in zip(segments, segments[1:], strict=False))


def _is_decisions_route(segments: Sequence[str]) -> bool:
    return len(segments) >= 2 and segments[-1] == "decisions" and segments[-2] in VERSIONS


class _ThoughtReader(DeltaReader):
    """Moves a Gemma model's thought out of content.

    Google streams it as content flagged ``extra_content.google.thought`` and wrapped in ``<thought>`` tags, the closing tag
    leading the first unflagged delta. It is reasoning, so it must not reach the reply, or a forced call's JSON.
    """

    def __init__(self) -> None:
        self.in_thought = False

    def read(self, delta: Mapping[str, Any]) -> tuple[str, Any]:
        content = delta.get("content")
        if content and _is_thought(delta):
            thought = (content if self.in_thought else content.removeprefix("<thought>")).removesuffix("</thought>")
            self.in_thought = True
            return thought, None
        if content and self.in_thought:
            self.in_thought = False
            return "", content.removeprefix("</thought>")
        return "", content


def _is_thought(delta: Mapping[str, Any]) -> bool:
    extra = delta.get("extra_content")
    google = extra.get("google") if isinstance(extra, Mapping) else None
    return isinstance(google, Mapping) and google.get("thought") is True


class Google(Dialect):
    name = "google"
    openai_base = OPENAI_BASE

    def matches(self, url: str) -> bool:
        segments = path_segments(url)
        return _follows_version(segments, "openai") or _is_decisions_route(segments)

    def serves_openai_base(self, url: str) -> bool:
        return _follows_version(path_segments(url), "openai")

    def profile(self, model: str) -> ModelProfile:
        return _PROFILE

    def prepare(self, body: dict) -> list[str]:
        """Carry Orb's reasoning switch in ``reasoning_effort``, the one control this surface reads.

        Reasoning off asks for ``none`` unless an explicit effort is set; a level the model refuses is stepped along the shared
        effort ladder once learned. The other dialects are dropped up front.
        """
        reasoning = body.get("reasoning")
        thinking = body.get("thinking")
        disabled = (isinstance(reasoning, Mapping) and reasoning.get("enabled") is False) or (
            isinstance(thinking, Mapping) and thinking.get("type") == "disabled"
        )
        dropped = [key for key in _UNREAD_REASONING_FIELDS if key in body]
        for key in dropped:
            body.pop(key)
        actions: list[str] = []
        if disabled and "reasoning_effort" not in body:
            body["reasoning_effort"] = "none"
            actions.append("reasoning_effort='none' (reasoning off)")
        if dropped:
            actions.append(f"{dropped} dropped (not read by this surface)")
        return ["; ".join(actions)] if actions else []

    def suspects_effort(self, status: int, text: str) -> bool:
        # Google's catch-all 400, which is also all it answers ``none`` with on a model that cannot stop thinking.
        return status == 400 and "request contains an invalid argument" in text.lower()

    def delta_reader(self) -> DeltaReader:
        return _ThoughtReader()

    def auth_headers(self, url: str, api_key: str) -> dict[str, str]:
        # The compatibility layer takes a Bearer key; the decisions route reads a Bearer token as OAuth and refuses an API key.
        if _is_decisions_route(path_segments(url)):
            return {"x-goog-api-key": api_key}
        return super().auth_headers(url, api_key)

    def normalize_model_id(self, model_id: str) -> str:
        # The catalogue lists ids with a ``models/`` prefix that generation does not need.
        return model_id.removeprefix("models/")

    def decisions_path(self, netloc: str, segments: Sequence[str]) -> list[str] | None:
        """``/v1beta/decisions`` for a chat-style Google URL (``/v1beta/openai``, ``/v1beta``, or the bare host)."""
        trimmed = list(segments)
        while trimmed and trimmed[-1].lower() in _CHAT_SUFFIXES:
            trimmed.pop()
        if trimmed and trimmed[-1].lower() in VERSIONS:
            return [*trimmed, "decisions"]
        if not trimmed and netloc.lower() == _HOST:
            return [VERSIONS[0], "decisions"]
        return None

    # Decision questions and answers are Jev's shape, but ``state`` is refused as an unknown parameter.
    decision_state_field = "input"
    # Its noul criteria object takes no outcome fields ("Unknown parameter 'true'"); its choice questions take any keys.
    decision_noul_criteria = False
