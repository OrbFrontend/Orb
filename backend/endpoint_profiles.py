"""Endpoint/model-matched request translation profiles.

Some OpenAI-compatible backends reject unknown body fields or demand
specific value shapes that don't match Orb's defaults. This module defines
per-(endpoint_url, model) policies that mutate the request body before it
leaves LLMClient.complete().

Two-level lookup:
- Known endpoint + known model -> model-specific profile (replaces default).
- Known endpoint + unknown/blank model -> endpoint default (None key).
- Unknown endpoint -> None = pass-through (for local llama.cpp, vLLM, etc.).

Adding a new quirk (extensibility gradient):
  1. Flip an existing typed knob (allow_extra, allow_forced_tool_choice).
  2. Attach a `custom=` callable to a profile for one-off logic.
  3. Promote a recurring `custom=` pattern to a named dataclass field.
  4. Subclass ModelProfile and override apply() for radically different APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Body keys always sent; never subject to allowlist filtering.
ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {"model", "messages", "stream", "tools", "tool_choice"}
)

# Mutates body in place. Returns a log line to surface the action, or None.
Transform = Callable[[dict], Optional[str]]


@dataclass(frozen=True)
class ModelProfile:
    """Per-(endpoint, model) request translation policy.

    Typed knobs cover the common cases; `custom` is the escape hatch for
    bespoke transforms that don't yet warrant a named field.
    """

    # Extra body keys allowed past ALWAYS_ALLOWED. Anything else is dropped.
    allow_extra: frozenset[str]

    # If False, coerce forced-function tool_choice dicts and "required" to
    # "auto". True means the caller's value passes through unchanged.
    allow_forced_tool_choice: bool = True

    # Bespoke transforms applied after typed knobs, in order. Each callable
    # mutates body in place and may return a log line (or None for silent).
    custom: tuple[Transform, ...] = field(default_factory=tuple)

    def apply(self, body: dict) -> list[str]:
        """Mutate body in place. Return human-readable actions for logging."""
        actions: list[str] = []

        allowed = ALWAYS_ALLOWED | self.allow_extra
        dropped = [k for k in body if k not in allowed]
        for k in dropped:
            body.pop(k)
        if dropped:
            actions.append(f"dropped={dropped}")

        if not self.allow_forced_tool_choice:
            tc = body.get("tool_choice")
            if isinstance(tc, dict) or tc == "required":
                body["tool_choice"] = "auto"
                actions.append(f"tool_choice {tc!r} -> 'auto'")

        for fn in self.custom:
            log = fn(body)
            if log:
                actions.append(log)

        return actions


# https://api-docs.deepseek.com/api/create-chat-completion
_DEEPSEEK_DEFAULT_EXTRA: frozenset[str] = frozenset(
    {
        "temperature",
        "top_p",
        "max_tokens",
        "presence_penalty",
        "frequency_penalty",
        "stop",
        "response_format",
        "logprobs",
        "top_logprobs",
        "stream_options",
        "thinking",
    }
)

# deepseek-reasoner rejects logprobs/top_logprobs with HTTP 400. Other
# "unsupported" params (temperature/top_p/presence_penalty/frequency_penalty)
# are silently ignored per DeepSeek docs, so keeping them in is harmless.
_DEEPSEEK_REASONER_EXTRA: frozenset[str] = _DEEPSEEK_DEFAULT_EXTRA - {
    "logprobs",
    "top_logprobs",
}


def _deepseek_coerce_tool_choice_when_thinking(body: dict) -> Optional[str]:
    """Any DeepSeek request with thinking enabled is routed through reasoner
    semantics, which reject forced-function tool_choice and "required" -- the
    API echoes back "deepseek-reasoner does not support this tool_choice"
    even when model=deepseek-chat. Coerce to "auto" so the graceful-skip
    paths in Director/Editor handle any unselected tool calls.
    """
    thinking = body.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return None
    tc = body.get("tool_choice")
    if isinstance(tc, dict) or tc == "required":
        body["tool_choice"] = "auto"
        return f"tool_choice {tc!r} -> 'auto' (thinking enabled)"
    return None


# Outer key: URL-substring (case-insensitive match; first insertion wins, so
# order matters if adding more specific URL prefixes like "api.deepseek.com/beta"
# -- the more specific one must come first).
# Inner None key: endpoint default profile. Inner str keys: exact-match
# per-model overrides (replace, not merge).
PROFILES: dict[str, dict[Optional[str], ModelProfile]] = {
    "api.deepseek.com": {
        # deepseek-chat supports forced-function tool_choice in chat mode but
        # rejects it whenever the request also carries thinking=enabled (the
        # API silently routes thinking-on requests through reasoner semantics).
        # The custom transform handles that conditional case.
        None: ModelProfile(
            allow_extra=_DEEPSEEK_DEFAULT_EXTRA,
            allow_forced_tool_choice=True,
            custom=(_deepseek_coerce_tool_choice_when_thinking,),
        ),
        # deepseek-reasoner is unconditionally thinking-on, so coerce statically.
        # Equivalent to the conditional above for this model; kept as a static
        # knob for clarity. Graceful-skip paths in Director/Editor handle any
        # unselected tool calls.
        "deepseek-reasoner": ModelProfile(
            allow_extra=_DEEPSEEK_REASONER_EXTRA,
            allow_forced_tool_choice=False,
        ),
    },
}


def profile_for(endpoint_url: str, model: str = "") -> Optional[ModelProfile]:
    """Resolve (endpoint_url, model) to a ModelProfile, or None for pass-through.

    A blank `model` falls through to the endpoint default. An unmatched URL
    returns None -- LLMClient then sends the body unchanged (current behavior
    for local / unknown backends).
    """
    if not endpoint_url:
        return None
    haystack = endpoint_url.lower()
    for needle, models in PROFILES.items():
        if needle in haystack:
            if model and model in models:
                return models[model]
            return models.get(None)
    return None
