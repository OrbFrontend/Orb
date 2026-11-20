"""Resolve endpoint routes and recover from the rejections any OpenAI-protocol provider may give.

What a particular provider does differently lives in its dialect (:mod:`.providers`), resolved from the route being called.
What each endpoint and model was seen to accept lives in the :data:`~.ledger.LEDGER`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from . import providers
from .ledger import LEDGER
from .providers import Dialect, is_forced_tool_choice

# Assistant-message fields that carry a model's reasoning back to it. The chat transport stores reasoning under the names the
# provider streamed, and a replay echoes them unchanged, because servers disagree about what they read: llama.cpp reads only
# ``reasoning_content``, vLLM only ``reasoning``, and OpenRouter also needs ``reasoning_details`` to keep signed reasoning
# valid.
REASONING_REPLAY_FIELDS: tuple[str, ...] = ("reasoning_content", "reasoning", "reasoning_details")


Protocol = Literal["openai", "anthropic"]
AuthFamily = Literal["bearer", "anthropic"]


@dataclass(frozen=True)
class EndpointRoute:
    """One concrete transport resource resolved from a configured endpoint."""

    protocol: Protocol
    url: str
    models_url: str
    auth_family: AuthFamily
    authoritative: bool = False


def _clean_url(url: str) -> str:
    return url.strip().rstrip("/")


def _parsed_http_url(url: str):
    parsed = urlsplit(_clean_url(url))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed


def _replace_path(parsed, path: str) -> str:
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _resource_route(protocol: Protocol, url: str, *, authoritative: bool) -> EndpointRoute:
    parsed = _parsed_http_url(url)
    clean = _clean_url(url)
    path = parsed.path.rstrip("/") if parsed is not None else clean
    resource = "/messages" if protocol == "anthropic" else "/chat/completions"
    models_path = f"{path[: -len(resource)]}/models" if path.endswith(resource) else f"{path}/models"
    models_url = _replace_path(parsed, models_path) if parsed is not None else f"{clean}/models"
    return EndpointRoute(
        protocol=protocol,
        url=clean,
        models_url=models_url,
        auth_family="anthropic" if protocol == "anthropic" else "bearer",
        authoritative=authoritative,
    )


def _base_route(protocol: Protocol, base_url: str, *, authoritative: bool = False) -> EndpointRoute:
    clean = _clean_url(base_url)
    suffix = "/messages" if protocol == "anthropic" else "/chat/completions"
    parsed = _parsed_http_url(clean)
    url = f"{clean}{suffix}" if parsed is None else _replace_path(parsed, f"{parsed.path.rstrip('/')}{suffix}")
    return _resource_route(protocol, url, authoritative=authoritative)


def _deterministic_route(endpoint_url: str) -> EndpointRoute:
    """Resolve explicit resource shapes without provider/model-name guesses."""
    clean = _clean_url(endpoint_url)
    parsed = _parsed_http_url(clean)
    low = clean.lower()
    path = parsed.path.rstrip("/").lower() if parsed is not None else low

    # Full resource URLs are user intent.
    if path.endswith("/chat/completions"):
        return _resource_route("openai", clean, authoritative=True)
    if path.endswith("/messages"):
        return _resource_route("anthropic", clean, authoritative=True)

    # A provider's own OpenAI base path is as explicit as a full resource URL.
    if providers.openai_base_dialect(clean) is not None:
        return _base_route("openai", clean, authoritative=True)

    return _base_route("openai", clean)


def _learned_route(endpoint_url: str, model: str) -> EndpointRoute | None:
    # A successful route is remembered per configured URL and model because client objects are per-turn; a model listing
    # remembers one for the whole endpoint. Stored URLs are never rewritten.
    return LEDGER.peek(endpoint_url, model).route or LEDGER.peek(endpoint_url).route


def route_for(endpoint_url: str, model: str = "") -> EndpointRoute:
    """The route a call to this endpoint and model goes to first: the one that last succeeded, or the deterministic one."""
    return _learned_route(endpoint_url, model) or _deterministic_route(endpoint_url)


def endpoint_candidates(endpoint_url: str, model: str = "") -> list[EndpointRoute]:
    """Return bounded same-host routes in request order.

    Explicit resource URLs win; ambiguous URLs try the configured chat route, Messages sibling and conventional host-root
    routes, then each provider's own OpenAI base at the root. Probe alternatives only for recognized route-mismatch responses.
    """
    primary = route_for(endpoint_url, model)
    if primary.authoritative or _learned_route(endpoint_url, model) is not None:
        return [primary]
    parsed = _parsed_http_url(endpoint_url)
    if parsed is None:
        return [primary]
    root = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    candidates = [
        primary,
        _base_route("anthropic", endpoint_url),
        _base_route("openai", f"{root}/v1"),
        _base_route("anthropic", f"{root}/v1"),
        *(_base_route("openai", f"{root}{dialect.openai_base}") for dialect in providers.DIALECTS if dialect.openai_base),
    ]
    out: list[EndpointRoute] = []
    seen: set[tuple[str, str]] = set()
    for route in candidates:
        key = (route.protocol, route.url)
        if key not in seen:
            seen.add(key)
            out.append(route)
    return out


def note_successful_route(endpoint_url: str, model: str, route: EndpointRoute) -> None:
    """Cache a route after its stream completes successfully, confirming a suspected ``reasoning_effort`` refusal it stepped past."""
    facts = LEDGER.facts(endpoint_url, model)
    facts.route = route
    facts.effort_acquitted = False
    if facts.effort_suspected is not None:
        facts.effort_refused.add(facts.effort_suspected)
        facts.effort_suspected = None


def auth_families(route: EndpointRoute, status: int | None = None, text: str = "") -> tuple[AuthFamily, ...]:
    """Return primary auth and a bounded, evidence-based alternative."""
    primary = route.auth_family
    if status not in {401, 403}:
        return (primary,)
    # A Messages route defines its auth default. Ambiguous routes may need the other family before the server will reveal that
    # the resource is wrong; an explicit OpenAI route only retries when the response names the native API-key header. None of
    # these signals depends on provider or model names.
    if route.protocol != "anthropic" and route.authoritative and "x-api-key" not in text.lower():
        return (primary,)
    other: AuthFamily = "bearer" if primary == "anthropic" else "anthropic"
    return (primary, other)


def note_forced_tool_choice_ignored(endpoint_url: str, model: str) -> None:
    """Record that *model* answered a forced tool_choice with some other tool.

    Either a profile coerced the choice to "auto" or the provider ignored it silently (OpenRouter with a thinking-on model,
    llama.cpp's chat endpoint, ...).
    """
    LEDGER.facts(endpoint_url, model).forced_choice_ignored = True


def note_structured_output_ignored(endpoint_url: str, model: str) -> None:
    """Record that *model* ignored a strict ``response_format`` schema.

    A provider that *rejects* the field answers 4xx and is handled by recover_from_error; one that accepts and ignores it can
    only be caught by reading the reply.

    Callers must only report a reply that *proves* the constraint was absent -- a completed, non-empty answer that is not the
    JSON the schema demanded, or one cut off after padding its tail with whitespace, which a model does when the strict
    grammar demands a key it will not write. Any other truncated reply, or an empty one, proves nothing (same standard as
    :func:`note_forced_tool_choice_ignored`), and demoting on one would cost the endpoint its best-caching call shape over a
    flaky turn.
    """
    LEDGER.facts(endpoint_url, model).structured_output_ignored = True


def honors_forced_tool_choice(endpoint_url: str, model: str = "", params: Mapping[str, Any] | None = None) -> bool:
    """Return whether this endpoint should honor forced tool choice."""
    if LEDGER.peek(endpoint_url, model).forced_choice_ignored:
        return False
    body: dict = {**(dict(params) if params else {}), "tool_choice": {"type": "function", "function": {"name": "_probe"}}}
    prepare_request_body(endpoint_url, model, body, route_for(endpoint_url, model))
    return is_forced_tool_choice(body.get("tool_choice"))


def supports_structured_tool_calls(endpoint_url: str, model: str = "") -> bool:
    """True when the (endpoint, model) profile opts into structured forced calls.

    The profile knob is an *opt-in that evidence can revoke*. A proxy endpoint fronts many upstream engines, so honoring strict
    ``response_format`` is a per-model fact the URL cannot settle; a model that answers a schema-forced call with something the
    schema forbids has proven its route decodes unconstrained, and :func:`note_structured_output_ignored` demotes just that pair
    for the rest of the session.
    """
    if LEDGER.peek(endpoint_url, model).structured_output_ignored:
        return False
    profile = providers.resolve(route_for(endpoint_url, model).url).profile(model)
    return profile is not None and profile.structured_tool_calls


# Request preparation + error recovery (the seam LLMClient calls)
#
# ``prepare_request_body``, ``recover_from_error`` and ``recover_refused_field`` are the request side of the provider surface
# LLMClient depends on; the stream, auth and catalogue sides are the route's dialect hooks. The client stays transport-only: it
# builds the body, sends it, and on a >=400 asks here whether the failure is a recognised quirk worth a retry. Both functions
# take the route being called, so a probed route gets its own dialect, while what they learn is keyed by the configured URL.

# What a pair was seen to refuse is kept in the ledger (see ``PairFacts``): a refused tool_choice value or the whole field, each
# refused ``reasoning_effort`` level, a disabling ``reasoning`` object on an endpoint that always reasons, replayed reasoning
# fields, prompt-cache markers, and top-level body keys. Orb offers a superset of levels and fields and providers validate
# against their own, which move under us, so refusals are learned rather than listed. Learning is per level and per field: a
# refused ``xhigh`` must not cost a reasoning-off call the ``none`` the same model takes. Some providers stream reasoning and then
# reject it on the way back (Groq refuses both ``reasoning_content`` and ``reasoning``); dropping it on every later call keeps the
# rendered prefix the same from call to call. Prompt-cache markers turn a message's string content into a one-part text list,
# which a strict schema can refuse without ever naming ``cache_control`` (DeepSeek documents system and assistant content as
# string-only), so that refusal is recognized by the unmarked retry succeeding, not by the error text.

# Levels from least to most reasoning. A refused level moves to the nearest level the pair has not refused, the cheaper one on a
# tie, and never to ``none``, so an off call stays as near off as the model allows and an on call stays on. Measured on Google's
# surface 2026-11-20: Gemma 4 takes only minimal and high, Gemini 3.5 Flash-Lite refuses none, and no model takes xhigh. A level
# outside this list, or with nowhere left to go, is dropped and the provider default governs.
_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh")

# A refusal that names nothing (Google's bare "Request contains an invalid argument.") only suspects the level: it is stepped past
# like a refusal but learned only once the stepped call succeeds; the same 400 again means the level was not the cause, and it is
# restored and not suspected again until the pair next succeeds. OpenRouter answers "Reasoning is mandatory for this endpoint and
# cannot be disabled" to a disabling ``reasoning`` object while taking the other dialects' off switches, so a reasoning-off call
# sheds that object and runs, reasoning. A reasoning-on call keeps it, and with it ``reasoning.effort``.

_FIELD_REFUSAL_MARKERS = (
    "unsupported",
    "not supported",
    "not allowed",
    "not permitted",
    "extra inputs",
    "additional properties",
    "unexpected",
    "unknown",
    "unrecognized",
)

# Orb forwards top-level keys only some servers read -- the min_p/top_k/repetition samplers, every reasoning dialect, the user's
# extra body -- and a server that validates its schema answers 400/422 naming the one it does not take. Learning each refusal
# stands in for a hand-kept list of what every provider accepts.

# Keys a request cannot shed and stay the request it was, so a refusal naming one is the error itself. ``max_tokens`` is the
# visible budget setting: a refusal of it surfaces instead of the call quietly running unbounded. ``tool_choice`` has its own
# recoveries.
_ESSENTIAL_BODY_FIELDS = frozenset({"model", "messages", "stream", "tools", "tool_choice", "max_tokens"})

# How close a key must sit to the refusal wording to be the key it refuses. A schema error that goes on to list every accepted
# field (serde's "unknown field `min_p`, expected one of ...") names several body keys; the refused one is next to the marker.
_REFUSAL_WINDOW = 48

_REFUSAL_RE = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(marker) for marker in _FIELD_REFUSAL_MARKERS) + r")(?!\w)")


def _is_tool_choice_auto_only(status: int, text: str) -> bool:
    """Return True when the body states that only ``auto`` is accepted."""
    low = text.lower()
    return status in {400, 404} and "tool_choice" in low and "only" in low and "auto" in low and "support" in low


_THINKING_REFUSAL_RE = re.compile(r"thinking (?:budget|level)\b.*\bnot supported")


def _is_reasoning_effort_rejected(status: int, text: str) -> bool:
    """Return True when a 400 refuses the body's ``reasoning_effort`` level.

    Matches a body naming the field as invalid, and Google's wording for the thinking setting the level maps to ("Thinking budget
    is not supported for this model.", "Thinking level XHIGH is not supported"). A generic 400 (bad model, oversized prompt)
    never costs a reasoning setting the endpoint accepts.
    """
    low = text.lower()
    if status != 400:
        return False
    if "reasoning_effort" in low:
        return any(marker in low for marker in ("invalid", "unsupported", "not supported", "not allowed", "valid values"))
    return _THINKING_REFUSAL_RE.search(low) is not None


def _settle_effort(body: dict, refused: set[str]) -> str | None:
    """Move the body's ``reasoning_effort`` past every level in *refused*; return a log line when it changed."""
    original = body.get("reasoning_effort")
    if not isinstance(original, str) or original not in refused:
        return None
    effort: str | None = None
    if original in _EFFORT_LEVELS:
        rank = _EFFORT_LEVELS.index(original)
        # Nearest first, the cheaper level on a tie; ``none`` (index 0) is never a step.
        steps = sorted(range(1, len(_EFFORT_LEVELS)), key=lambda index: (abs(index - rank), index))
        effort = next((_EFFORT_LEVELS[index] for index in steps if _EFFORT_LEVELS[index] not in refused), None)
    if effort is None:
        body.pop("reasoning_effort")
        return f"reasoning_effort {original!r} dropped"
    body["reasoning_effort"] = effort
    return f"reasoning_effort {original!r} -> {effort!r}"


def _disables_reasoning(body: Mapping[str, Any]) -> bool:
    """Whether the body's ``reasoning`` object turns reasoning off."""
    reasoning = body.get("reasoning")
    return isinstance(reasoning, Mapping) and (reasoning.get("enabled") is False or reasoning.get("effort") == "none")


def _is_reasoning_mandatory(status: int, text: str) -> bool:
    """Return True when the body says reasoning cannot be turned off here."""
    low = text.lower()
    return status == 400 and "reasoning" in low and ("cannot be disabled" in low or "mandatory" in low)


def _drop_replay_fields(body: dict, fields: set[str]) -> list[str]:
    """Remove *fields* from the body's messages and return the ones that were present.

    Changed messages are copied, because the outbound body shares its message dicts with the caller's transcript.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    removed: set[str] = set()
    kept: list[Any] = []
    for message in messages:
        present = fields.intersection(message) if isinstance(message, Mapping) else set()
        if present:
            removed |= present
            message = {key: value for key, value in message.items() if key not in present}
        kept.append(message)
    if removed:
        body["messages"] = kept
    return sorted(removed)


def _refused_replay_fields(body: dict, status: int, text: str) -> set[str]:
    """Return the replayed reasoning fields that a rejection names.

    Only fields the body actually carries can match, and the body must point at ``messages``. That way a refused top-level
    ``reasoning`` param, or a generic 400, never costs a replay the endpoint accepts.
    """
    low = text.lower()
    if status not in {400, 422} or "messages" not in low:
        return set()
    if not any(marker in low for marker in _FIELD_REFUSAL_MARKERS):
        return set()
    messages = body.get("messages")
    carried = {
        field
        for message in (messages if isinstance(messages, list) else ())
        if isinstance(message, Mapping)
        for field in REASONING_REPLAY_FIELDS
        if field in message
    }
    return {field for field in carried if re.search(rf"(?<!\w){field}(?!\w)", low)}


def _refused_body_field(body: Mapping[str, Any], status: int, text: str) -> str | None:
    """Return the one top-level body key a rejection refuses, or ``None``.

    Only a 400/422 with refusal wording counts, and only a key the body carries and can shed. The key named nearest that wording
    wins, within :data:`_REFUSAL_WINDOW` characters. A key spelled as a path segment (``messages.1.reasoning``) belongs to a
    message, not the body.
    """
    if status not in {400, 422}:
        return None
    low = text.lower()
    markers = [match.span() for match in _REFUSAL_RE.finditer(low)]
    if not markers:
        return None
    best: tuple[int, str] | None = None
    for key in body:
        if key in _ESSENTIAL_BODY_FIELDS:
            continue
        for found in re.finditer(rf"(?<![\w.]){re.escape(key.lower())}(?!\w)", low):
            gap = min(max(0, start - found.end(), found.start() - end) for start, end in markers)
            if gap <= _REFUSAL_WINDOW and (best is None or gap < best[0]):
                best = (gap, key)
    return best[1] if best else None


def recover_refused_field(endpoint_url: str, model: str, body: Mapping[str, Any], status: int, text: str) -> str | None:
    """Learn a top-level body key the endpoint refused, so this and every later request omits it.

    Returns a log line (triggering a retry) or ``None``. Each recovery sheds a key the body still carried, so a run of them ends
    once the body holds only keys the endpoint takes.
    """
    key = _refused_body_field(body, status, text)
    if key is None:
        return None
    LEDGER.facts(endpoint_url, model).body_fields_refused.add(key)
    return f"Model {model} refused the {key!r} request field; retrying without it."


def sends_cache_markers(endpoint_url: str, model: str) -> bool:
    """Whether requests to this pair still carry prompt-cache breakpoints."""
    return not LEDGER.peek(endpoint_url, model).cache_markers_refused


def note_cache_markers_refused(endpoint_url: str, model: str) -> None:
    """Record that an unmarked retry succeeded where the marked request failed."""
    LEDGER.facts(endpoint_url, model).cache_markers_refused = True


def prepare_request_body(endpoint_url: str, model: str, body: dict, route: EndpointRoute) -> list[str]:
    """Apply the route's dialect and every session-learned workaround to *body* in place.

    Returns log lines for each mutation (empty list if the body is unchanged).
    """
    dialect = providers.resolve(route.url)
    facts = LEDGER.peek(endpoint_url, model)
    actions: list[str] = []

    profile = dialect.profile(model)
    if profile is not None:
        actions.extend(profile.apply(body))
    actions.extend(dialect.prepare(body))

    refused_fields = sorted(key for key in facts.body_fields_refused if key in body)
    for key in refused_fields:
        body.pop(key)
    if refused_fields:
        actions.append(f"{refused_fields} dropped (session-learned unsupported)")

    refused_efforts = facts.effort_refused
    if facts.effort_suspected is not None:
        refused_efforts = refused_efforts | {facts.effort_suspected}
    settled = _settle_effort(body, refused_efforts)
    if settled:
        actions.append(f"{settled} (session-learned unsupported)")

    if facts.reasoning_mandatory and _disables_reasoning(body):
        body.pop("reasoning")
        actions.append("reasoning-off object dropped (session-learned: this endpoint always reasons)")

    if facts.replay_refused:
        dropped = _drop_replay_fields(body, facts.replay_refused)
        if dropped:
            actions.append(f"replayed reasoning {dropped} dropped (session-learned unsupported)")

    # A model we already learned rejects tool_choice this session: drop it up front so we skip the failing round-trip entirely.
    if "tool_choice" in body and facts.tool_choice_unsupported:
        tc = body.pop("tool_choice")
        actions.append(f"tool_choice {tc!r} dropped (session-learned unsupported)")

    if "tool_choice" in body and facts.tool_choice_auto_only:
        tc = body["tool_choice"]
        if tc != "auto" and tc != {"type": "auto"}:
            body["tool_choice"] = "auto"
            actions.append(f"tool_choice {tc!r} -> 'auto' (session-learned auto-only)")

    return actions


def recover_from_error(endpoint_url: str, model: str, body: dict, status: int, text: str, route: EndpointRoute) -> str | None:
    """Handle a >=400 response. If a known quirk explains it, mutate *body* in place, record the quirk for the session, and
    return a log line (triggering one retry). Returns ``None`` to propagate the error.

    The shared recoveries run first; the route's dialect then handles what only its provider answers. Model catalog ids are
    deliberately never recorded here; learned capability facts expire with the backend process.
    """
    dialect: Dialect = providers.resolve(route.url)
    facts = LEDGER.facts(endpoint_url, model)
    tc = body.get("tool_choice")
    low = text.lower()
    native_forced_rejected = (
        status == 400
        and isinstance(tc, Mapping)
        and tc.get("type") in {"any", "tool"}
        and "tool_choice" in low
        and any(marker in low for marker in ("not supported", "unsupported", "not allowed"))
    )
    if native_forced_rejected:
        facts.tool_choice_auto_only = True
        facts.forced_choice_ignored = True
        body["tool_choice"] = {"type": "auto"}
        return f"Model {model} rejected forced Anthropic tool choice; retrying with auto."
    if "tool_choice" in body and _is_tool_choice_auto_only(status, text):
        facts.tool_choice_auto_only = True
        tc = body["tool_choice"]
        body["tool_choice"] = {"type": "auto"} if isinstance(tc, dict) and "function" not in tc else "auto"
        return f"Model {model} accepts only tool_choice='auto'; retrying with auto."
    if "reasoning_effort" in body and _is_reasoning_effort_rejected(status, text):
        effort = body["reasoning_effort"]
        if not isinstance(effort, str):
            body.pop("reasoning_effort")
            return f"Model {model} rejected reasoning_effort={effort!r}; retrying without it."
        facts.effort_refused.add(effort)
        return f"Model {model} rejected reasoning_effort={effort!r}; {_settle_effort(body, facts.effort_refused)}, retrying."
    if dialect.suspects_effort(status, text):
        if facts.effort_suspected is not None:
            # The stepped call failed the same way, so the level was not the cause; later attempts send it again.
            facts.effort_suspected = None
            facts.effort_acquitted = True
        elif body.get("reasoning_effort") == "none" and not facts.effort_acquitted:
            facts.effort_suspected = "none"
            settled = _settle_effort(body, facts.effort_refused | {"none"})
            return f"Model {model} answered reasoning_effort='none' with a bare 400; {settled}, retrying."
    if _disables_reasoning(body) and _is_reasoning_mandatory(status, text):
        facts.reasoning_mandatory = True
        body.pop("reasoning")
        return f"Model {model} always reasons and refused a reasoning-off request; retrying without that switch."
    refused = _refused_replay_fields(body, status, text)
    if refused:
        facts.replay_refused.update(refused)
        _drop_replay_fields(body, refused)
        return f"Model {model} rejected replayed reasoning {sorted(refused)}; retrying without it."
    return dialect.recover(facts, body, status, text, model)
