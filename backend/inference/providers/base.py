"""The dialect seam: what a provider speaking OpenAI's protocol does differently.

A :class:`Dialect` is identified by the shape of the route being called, never by a model id, so a proxy mirroring a provider's
paths gets that provider's dialect. Every hook defaults to plain OpenAI behaviour, which is what an unrecognized route gets.
Recovery that works for any provider (refused body fields, replayed reasoning, cache markers, the reasoning-effort ladder) is
shared in :mod:`..endpoint_profiles`; a dialect only adds what is true of its own provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..ledger import PairFacts


def is_forced_tool_choice(tc: object) -> bool:
    """Return ``True`` if *tc* forces a specific tool call (a dict or ``"required"``).

    Single source of truth for "forced" -- used by profile coercion and the client's self-heal path.
    """
    return isinstance(tc, dict) or tc == "required"


def path_segments(url: str) -> list[str]:
    """The lowercased, non-empty path segments of *url*; a URL without a scheme reads as all path."""
    return [segment for segment in urlsplit(url.strip()).path.lower().split("/") if segment]


@dataclass(frozen=True)
class ModelProfile:
    """Typed request policy for one model on a dialect."""

    # If False, coerce forced-function tool_choice dicts and "required" to "auto". True means the caller's value passes through
    # unchanged.
    allow_forced_tool_choice: bool = True

    # If True, the chat transport rewrites forced-function tool calls as strict ``response_format`` structured-output requests
    # (the chat analogue of text mode's forced grammar), guaranteeing byte-exact argument keys. Setting it also withholds
    # ``tools`` and ``tool_choice`` from every chat request to the endpoint -- a model that can still see ``tools`` may answer
    # with a native tool call that bypasses the schema. Opt-in per provider: only set after verifying the endpoint honors
    # ``response_format: {"type": "json_schema", "strict": true}``.
    structured_tool_calls: bool = False

    def apply(self, body: dict) -> list[str]:
        """Apply this profile to *body* in place. Returns log lines for each mutation."""
        actions: list[str] = []
        if not self.allow_forced_tool_choice:
            tc = body.get("tool_choice")
            if is_forced_tool_choice(tc):
                body["tool_choice"] = "auto"
                actions.append(f"tool_choice {tc!r} -> 'auto'")
        return actions


class DeltaReader:
    """Splits one streamed chat delta into ``(reasoning, content)``. One reader serves one stream, so it may keep state."""

    def read(self, delta: Mapping[str, Any]) -> tuple[str, Any]:
        return "", delta.get("content")


class Dialect:
    """Plain OpenAI chat completions. Subclasses override only what their provider does differently."""

    name = "openai"
    # The base path under which this provider serves OpenAI chat on its own host. A configured URL carrying it is an explicit
    # resource, and probing an ambiguous host tries it at the root.
    openai_base = ""

    def matches(self, url: str) -> bool:
        """Whether a route URL has this dialect's shape."""
        return False

    def serves_openai_base(self, url: str) -> bool:
        """Whether *url* sits on this dialect's :attr:`openai_base`."""
        return False

    def profile(self, model: str) -> ModelProfile | None:
        """Typed request policy for *model*, or ``None`` to pass requests through."""
        return None

    def prepare(self, body: dict) -> list[str]:
        """Rewrite *body* in place for this provider, after the profile; return a log line per change."""
        return []

    def suspects_effort(self, status: int, text: str) -> bool:
        """Whether a rejection that names no field may still be a refusal of ``reasoning_effort: none``."""
        return False

    def recover(self, facts: PairFacts, body: dict, status: int, text: str, model: str) -> str | None:
        """Handle a rejection only this provider gives, after the shared recoveries. Record what it teaches in *facts*, mutate
        *body* for the retry, and return a log line; ``None`` lets the error stand."""
        return None

    def delta_reader(self) -> DeltaReader:
        return DeltaReader()

    def auth_headers(self, url: str, api_key: str) -> dict[str, str]:
        """The headers that carry *api_key* to *url*."""
        return {"Authorization": f"Bearer {api_key}"}

    def normalize_model_id(self, model_id: str) -> str:
        """A catalogue id as generation expects it."""
        return model_id

    def decisions_path(self, netloc: str, segments: Sequence[str]) -> list[str] | None:
        """This provider's decisions route for a chat-style configured URL, or ``None`` to derive the generic one."""
        return None

    # The request field that carries the situation to a decisions route.
    decision_state_field = "state"
