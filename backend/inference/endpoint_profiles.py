"""Per-(endpoint, model) request translation profiles.

Some OpenAI-compatible backends reject unknown body fields or require
specific value shapes. This module defines policies that mutate the request
body before it leaves ``LLMClient.complete()``.

Two-level lookup (PROFILES dict):
- Known endpoint + known model → model-specific profile (replaces default).
- Known endpoint + unknown/blank model → endpoint default (``None`` key).
- Unknown endpoint → ``None`` = pass-through (local llama.cpp, vLLM, etc.).

To add a new quirk:
  1. Flip a typed knob (``allow_extra``, ``allow_forced_tool_choice``).
  2. Attach a ``custom=`` callable for one-off logic.
  3. Promote a recurring ``custom=`` pattern to a named dataclass field.
  4. Subclass ``ModelProfile`` and override ``apply()`` for radically different APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Body keys always sent; never subject to allowlist filtering.
ALWAYS_ALLOWED: frozenset[str] = frozenset({"model", "messages", "stream", "tools", "tool_choice"})

# Mutates body in place. Returns a log line to surface the action, or None.
Transform = Callable[[dict], Optional[str]]


def is_forced_tool_choice(tc: object) -> bool:
    """Return ``True`` if *tc* forces a specific tool call (a dict or ``"required"``).

    Single source of truth for "forced" — used by profile coercion and the
    client's self-heal path.
    """
    return isinstance(tc, dict) or tc == "required"


@dataclass(frozen=True)
class ModelProfile:
    """Per-(endpoint, model) request translation policy.

    Typed knobs cover the common cases; ``custom`` is the escape hatch for
    one-off transforms that don't yet warrant a named field.
    """

    # Extra body keys allowed past ALWAYS_ALLOWED. Anything else is dropped.
    # None disables the drop step entirely (no allowlist filtering) -- use for
    # lenient backends (e.g. OpenRouter) where enumerating params risks
    # dropping ones the model actually wants.
    allow_extra: frozenset[str] | None

    # If False, coerce forced-function tool_choice dicts and "required" to
    # "auto". True means the caller's value passes through unchanged.
    allow_forced_tool_choice: bool = True

    # If True, the chat transport rewrites forced-function tool calls as
    # strict ``response_format`` structured-output requests (the chat analogue
    # of text mode's forced grammar), guaranteeing byte-exact argument keys.
    # Opt-in per provider: only set after verifying the endpoint honors
    # ``response_format: {"type": "json_schema", "strict": true}``.
    structured_tool_calls: bool = False

    # Bespoke transforms applied after typed knobs, in order. Each callable
    # mutates body in place and may return a log line (or None for silent).
    custom: tuple[Transform, ...] = field(default_factory=tuple)

    def apply(self, body: dict) -> list[str]:
        """Apply this profile to *body* in place. Returns log lines for each mutation."""
        actions: list[str] = []

        if self.allow_extra is not None:
            allowed = ALWAYS_ALLOWED | self.allow_extra
            dropped = [k for k in body if k not in allowed]
            for k in dropped:
                body.pop(k)
            if dropped:
                actions.append(f"dropped={dropped}")

        if not self.allow_forced_tool_choice:
            tc = body.get("tool_choice")
            if is_forced_tool_choice(tc):
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
    """Coerce forced ``tool_choice`` to ``"auto"`` when thinking is enabled.

    DeepSeek routes any thinking-on request through reasoner semantics, which
    reject forced-function ``tool_choice`` (and ``"required"``) even when
    ``model=deepseek-chat``. Coercing to ``"auto"`` lets the director/editor
    graceful-skip paths handle any unselected tool calls.
    """
    thinking = body.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return None
    tc = body.get("tool_choice")
    if is_forced_tool_choice(tc):
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
    # No None-key default: unlisted OpenRouter models stay pass-through (most
    # honor forcing). List only models known to reject a forced-function
    # tool_choice; add a one-liner per newly-found one. llm_client self-heals
    # the first hit of an unlisted model and logs a reminder to add it here.
    "openrouter.ai": {
        "minimax/minimax-m3": ModelProfile(
            allow_extra=None,  # OpenRouter is lenient; drop nothing
            allow_forced_tool_choice=False,  # forced -> "auto"
        ),
    },
    # NanoGPT proxies to per-model providers whose tool-argument decoding is
    # unconstrained (observed: GLM-5.2 TEE mangles hyphenated argument keys
    # under a forced call), but its documented response_format json_schema
    # strict mode is honored -- so forced calls go out as structured output.
    "nano-gpt.com": {
        None: ModelProfile(
            allow_extra=None,  # lenient passthrough; drop nothing
            structured_tool_calls=True,
        ),
    },
}


def supports_structured_tool_calls(endpoint_url: str, model: str = "") -> bool:
    """True when the (endpoint, model) profile opts into structured forced calls."""
    profile = profile_for(endpoint_url, model)
    return profile is not None and profile.structured_tool_calls


def profile_for(endpoint_url: str, model: str = "") -> Optional[ModelProfile]:
    """Resolve (endpoint_url, model) to a ``ModelProfile``, or ``None`` for pass-through.

    A blank *model* falls through to the endpoint default. An unmatched URL
    returns ``None`` — the body is sent unchanged (local / unknown backends).
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


# ---------------------------------------------------------------------------
# Request preparation + error recovery (the provider seam LLMClient calls)
#
# These two module-level functions are the *entire* provider-specific surface
# LLMClient depends on. The client stays transport-only: it builds the body,
# sends it, and on a >=400 asks here whether the failure is a recognised quirk
# worth one retry. Everything that knows about a provider -- URL matching,
# error-text sniffing, the session memory of what a model rejects -- lives
# here, not in llm_client.
# ---------------------------------------------------------------------------

# (endpoint_url, model) pairs seen to reject the tool_choice param this
# session. In-memory only (cleared on restart); lets later calls drop it up
# front instead of paying the round-trip + retry again.
_TOOL_CHOICE_UNSUPPORTED: set[tuple[str, str]] = set()


def _is_openrouter(endpoint_url: str) -> bool:
    return "openrouter.ai" in endpoint_url.lower()


def _is_tool_choice_unsupported(status: int, text: str) -> bool:
    """Return ``True`` for OpenRouter's ``tool_choice``-unsupported 404.

    Matches "No endpoints found that support the provided 'tool_choice'
    value." — meaning the routed provider rejects all ``tool_choice`` values.
    Kept narrow so genuine 404s (bad model id, etc.) don't match.
    """
    if status != 404:
        return False
    low = text.lower()
    return "tool_choice" in low and "no endpoints found" in low


def prepare_request_body(endpoint_url: str, model: str, body: dict) -> list[str]:
    """Apply the matching profile and any session-learned workarounds to *body* in place.

    Returns log lines for each mutation (empty list if the body is unchanged).
    """
    actions: list[str] = []

    profile = profile_for(endpoint_url, model)
    if profile is not None:
        actions.extend(profile.apply(body))

    # A model we already learned rejects tool_choice this session: drop it up
    # front so we skip the failing round-trip entirely.
    if "tool_choice" in body and (endpoint_url, model) in _TOOL_CHOICE_UNSUPPORTED:
        tc = body.pop("tool_choice")
        actions.append(f"tool_choice {tc!r} dropped (session-learned unsupported)")

    return actions


def recover_from_error(endpoint_url: str, model: str, body: dict, status: int, text: str) -> Optional[str]:
    """Handle a >=400 response. If a known provider quirk explains it, mutate
    *body* in place, record the quirk for the session, and return a log line
    (triggering one retry). Returns ``None`` to propagate the error.

    Currently handles one quirk: an OpenRouter model whose routed provider
    rejects ``tool_choice`` entirely. Recovery is to drop the param and retry;
    the 404 lands before any SSE event so the retry is clean. Add such models
    to ``PROFILES['openrouter.ai']`` for a zero-retry fix.
    """
    if not _is_openrouter(endpoint_url):
        return None
    if "tool_choice" in body and _is_tool_choice_unsupported(status, text):
        _TOOL_CHOICE_UNSUPPORTED.add((endpoint_url, model))
        tc = body.pop("tool_choice")
        return (
            f"Model {model} rejected tool_choice={tc!r}; retrying without it. "
            f"Add it to endpoint_profiles.PROFILES['openrouter.ai'] for a zero-retry fix."
        )
    return None
