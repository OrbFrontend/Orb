"""Translate requests for endpoint and model quirks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

# Body keys always sent; never subject to allowlist filtering.
ALWAYS_ALLOWED: frozenset[str] = frozenset({"model", "messages", "stream", "tools", "tool_choice"})

# Mutates body in place. Returns a log line to surface the action, or None.
Transform = Callable[[dict], str | None]


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
    # Setting it also withholds ``tools`` and ``tool_choice`` from every chat
    # request to the endpoint -- a model that can still see ``tools`` may
    # answer with a native tool call that bypasses the schema.
    # Opt-in per provider: only set after verifying the endpoint honors
    # ``response_format: {"type": "json_schema", "strict": true}``.
    structured_tool_calls: bool = False

    # If True, every value except ``"auto"`` is rewritten to ``"auto"``.
    # Some routed providers reject not only forced choices but also ``"none"``;
    # this is deliberately separate from allow_forced_tool_choice.
    auto_tool_choice_only: bool = False

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

        if self.auto_tool_choice_only:
            tc = body.get("tool_choice")
            if tc is not None and tc != "auto":
                body["tool_choice"] = "auto"
                actions.append(f"tool_choice {tc!r} -> 'auto' (auto-only endpoint)")

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


def _deepseek_coerce_tool_choice_when_thinking(body: dict) -> str | None:
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
PROFILES: dict[str, dict[str | None, ModelProfile]] = {
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
    # NanoGPT is a *proxy*: each model id it fronts sits behind a different
    # upstream engine with its own config, so no endpoint-wide statement about
    # decoding is true of every model. Its own tool-argument decoding is
    # unconstrained (observed: GLM-5.2 TEE mangles hyphenated argument keys
    # under a forced call) while its documented response_format json_schema
    # strict mode is honored by the routes that implement it -- so the opt-in
    # here is the *optimistic default*, not a claim about the whole catalogue.
    # An upstream that quietly ignores the schema is demoted per model on the
    # first reply that proves it (``note_structured_output_ignored``), which is
    # why this stays one endpoint-wide knob instead of a hand-kept model list.
    "nano-gpt.com": {
        None: ModelProfile(
            allow_extra=None,  # lenient passthrough; drop nothing
            structured_tool_calls=True,
        ),
    },
    # Google's OpenAI compatibility API accepts ordinary OpenAI request fields
    # (unknown additions are ignored) and honors strict json_schema output.
    "generativelanguage.googleapis.com": {
        None: ModelProfile(
            allow_extra=None,
            structured_tool_calls=True,
        ),
    },
}


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


# A successful route is remembered per configured URL and model because client
# objects are per-turn. Stored URLs are never rewritten.
_RESOLVED_ROUTES: dict[tuple[str, str], EndpointRoute] = {}


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
    if path.endswith(resource):
        models_path = f"{path[: -len(resource)]}/models"
    else:
        models_path = f"{path}/models"
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
    if parsed is None:
        url = f"{clean}{suffix}"
    else:
        url = _replace_path(parsed, f"{parsed.path.rstrip('/')}{suffix}")
    return _resource_route(protocol, url, authoritative=authoritative)


def _deterministic_route(endpoint_url: str) -> EndpointRoute:
    """Resolve explicit resources and strong provider hints without probing."""
    clean = _clean_url(endpoint_url)
    parsed = _parsed_http_url(clean)
    low = clean.lower()
    path = parsed.path.rstrip("/").lower() if parsed is not None else low

    # Full resource URLs are user intent and win over host heuristics.
    if path.endswith("/chat/completions"):
        return _resource_route("openai", clean, authoritative=True)
    if path.endswith("/messages"):
        return _resource_route("anthropic", clean, authoritative=True)

    host = (parsed.hostname or "").lower() if parsed is not None else ""
    if host == "generativelanguage.googleapis.com" or host.endswith(".generativelanguage.googleapis.com"):
        base = _replace_path(parsed, "/v1beta/openai") if parsed is not None else clean
        return _base_route("openai", base, authoritative=True)

    segments = [segment for segment in path.split("/") if segment]
    if host == "api.anthropic.com" or host.endswith(".api.anthropic.com"):
        if not segments:
            base = _replace_path(parsed, "/v1") if parsed is not None else f"{clean}/v1"
        else:
            base = clean
        return _base_route("anthropic", base, authoritative=True)

    # Proxy prefixes such as /anthropic or /providers/anthropic/v1 are strong
    # enough to select native Messages without replaying a prompt elsewhere.
    if "anthropic" in segments:
        base = f"{clean}/v1" if segments[-1] == "anthropic" else clean
        return _base_route("anthropic", base, authoritative=True)

    return _base_route("openai", clean)


def resolve_endpoint(endpoint_url: str, model: str = "") -> EndpointRoute:
    """Return the cached or deterministic route for one configured endpoint."""
    return _RESOLVED_ROUTES.get((endpoint_url, model)) or _deterministic_route(endpoint_url)


def endpoint_candidates(endpoint_url: str, model: str = "") -> list[EndpointRoute]:
    """Return bounded same-host routes in request order.

    Explicit resources and provider hints are authoritative. Ambiguous URLs keep
    Orb's historical ``{configured}/chat/completions`` request first, followed
    by the conventional host-root OpenAI and Anthropic v1 resources. Candidates
    are only attempted when :func:`should_probe_route` recognizes the response
    body as a route mismatch.
    """
    primary = resolve_endpoint(endpoint_url, model)
    if primary.authoritative or (endpoint_url, model) in _RESOLVED_ROUTES:
        return [primary]
    parsed = _parsed_http_url(endpoint_url)
    if parsed is None:
        return [primary]
    root = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    candidates = [
        primary,
        _base_route("openai", f"{root}/v1"),
        _base_route("anthropic", f"{root}/v1"),
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
    """Cache a route after its stream completes successfully."""
    _RESOLVED_ROUTES[(endpoint_url, model)] = route


def should_probe_route(status: int, text: str) -> bool:
    """Whether an error body specifically identifies an HTTP route mismatch."""
    del status  # Deliberately a body fact; status-only routing is unsafe.
    low = text.lower()
    markers = (
        "cannot post /",
        "route not found",
        "unknown endpoint",
        "unrecognized request url",
        "unsupported endpoint",
        "invalid url (post",
    )
    return any(marker in low for marker in markers)


def auth_families(route: EndpointRoute, endpoint_url: str, model: str, status: int | None = None) -> tuple[AuthFamily, ...]:
    """Return primary auth and, on supported 401/403 evidence, its peer."""
    primary = route.auth_family
    if status not in {401, 403}:
        return (primary,)
    evidence = "claude" in model.lower() or "anthropic" in endpoint_url.lower() or route.protocol == "anthropic"
    if not evidence:
        return (primary,)
    other: AuthFamily = "bearer" if primary == "anthropic" else "anthropic"
    return (primary, other)


# (endpoint_url, model) pairs observed to answer a forced tool_choice with a
# different tool this session — either a profile coerced the choice to "auto"
# or the provider ignored it silently (OpenRouter + a thinking-on model,
# llama.cpp's chat endpoint, …). In-memory only, like _TOOL_CHOICE_UNSUPPORTED.
_FORCED_CHOICE_IGNORED: set[tuple[str, str]] = set()


def note_forced_tool_choice_ignored(endpoint_url: str, model: str) -> None:
    """Record that *model* answered a forced tool_choice with some other tool."""
    _FORCED_CHOICE_IGNORED.add((endpoint_url, model))


# (endpoint_url, model) pairs whose reply proved the endpoint did not actually
# constrain decoding to the strict ``response_format`` schema it was sent. A
# provider that *rejects* the field answers 4xx and is handled by
# recover_from_error; one that accepts and ignores it can only be caught by
# reading the reply. In-memory only, like ``_FORCED_CHOICE_IGNORED`` above.
_STRUCTURED_OUTPUT_IGNORED: set[tuple[str, str]] = set()


def note_structured_output_ignored(endpoint_url: str, model: str) -> None:
    """Record that *model* ignored a strict ``response_format`` schema.

    Callers must only report a reply that *proves* the constraint was absent --
    a completed, non-empty answer that is not the JSON the schema demanded.
    A truncated or empty reply proves nothing (same standard as
    :func:`note_forced_tool_choice_ignored`), and demoting on one would cost
    the endpoint its best-caching call shape over a flaky turn.
    """
    _STRUCTURED_OUTPUT_IGNORED.add((endpoint_url, model))


def honors_forced_tool_choice(endpoint_url: str, model: str = "", params: Mapping[str, Any] | None = None) -> bool:
    """Return whether this endpoint should honor forced tool choice."""
    if (endpoint_url, model) in _FORCED_CHOICE_IGNORED:
        return False
    body: dict = {**(dict(params) if params else {}), "tool_choice": {"type": "function", "function": {"name": "_probe"}}}
    prepare_request_body(endpoint_url, model, body)
    return is_forced_tool_choice(body.get("tool_choice"))


def supports_structured_tool_calls(endpoint_url: str, model: str = "") -> bool:
    """True when the (endpoint, model) profile opts into structured forced calls.

    The profile knob is an *opt-in that evidence can revoke*. A proxy endpoint
    fronts many upstream engines, so honoring strict ``response_format`` is a
    per-model fact the URL cannot settle; a model that answers a schema-forced
    call with something the schema forbids has proven its route decodes
    unconstrained, and :func:`note_structured_output_ignored` demotes just that
    pair for the rest of the session.
    """
    if (endpoint_url, model) in _STRUCTURED_OUTPUT_IGNORED:
        return False
    profile = profile_for(endpoint_url, model)
    return profile is not None and profile.structured_tool_calls


def profile_for(endpoint_url: str, model: str = "") -> ModelProfile | None:
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


# Request preparation + error recovery (the provider seam LLMClient calls)
#
# These two module-level functions are the *entire* provider-specific surface
# LLMClient depends on. The client stays transport-only: it builds the body,
# sends it, and on a >=400 asks here whether the failure is a recognised quirk
# worth one retry. Everything that knows about a provider -- URL matching,
# error-text sniffing, the session memory of what a model rejects -- lives
# here, not in llm_client.

# (endpoint_url, model) pairs seen to reject the tool_choice param this
# session. In-memory only (cleared on restart); lets later calls drop it up
# front instead of paying the round-trip + retry again.
_TOOL_CHOICE_UNSUPPORTED: set[tuple[str, str]] = set()

# Pairs observed to accept only the literal ``"auto"`` value. Unlike
# _TOOL_CHOICE_UNSUPPORTED, these endpoints still need the field so forced and
# writer ``"none"`` requests are coerced rather than dropped.
_TOOL_CHOICE_AUTO_ONLY: set[tuple[str, str]] = set()


def _is_openrouter(endpoint_url: str) -> bool:
    return "openrouter.ai" in endpoint_url.lower()


def _is_tool_choice_unsupported(status: int, text: str) -> bool:
    """Return ``True`` when the body says no ``tool_choice`` value is routed.

    Matches "No endpoints found that support the provided 'tool_choice'
    value." — meaning the routed provider rejects all ``tool_choice`` values.
    Kept narrow so genuine 404s (bad model id, etc.) don't match.
    """
    low = text.lower()
    return status in {400, 404} and "tool_choice" in low and "no endpoints found" in low


def _is_tool_choice_auto_only(status: int, text: str) -> bool:
    """Return True when the body states that only ``auto`` is accepted."""
    low = text.lower()
    return status in {400, 404} and "tool_choice" in low and "only" in low and "auto" in low and "support" in low


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

    if "tool_choice" in body and (endpoint_url, model) in _TOOL_CHOICE_AUTO_ONLY:
        tc = body["tool_choice"]
        if tc != "auto" and tc != {"type": "auto"}:
            body["tool_choice"] = "auto"
            actions.append(f"tool_choice {tc!r} -> 'auto' (session-learned auto-only)")

    return actions


def recover_from_error(endpoint_url: str, model: str, body: dict, status: int, text: str) -> str | None:
    """Handle a >=400 response. If a known provider quirk explains it, mutate
    *body* in place, record the quirk for the session, and return a log line
    (triggering one retry). Returns ``None`` to propagate the error.

    Currently handles one quirk: an OpenRouter model whose routed provider
    rejects ``tool_choice`` entirely. Recovery is to drop the param and retry;
    the 404 lands before any SSE event so the retry is clean. Model catalog ids
    are deliberately never recorded here; learned capability facts expire with
    the backend process.
    """
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
        _TOOL_CHOICE_AUTO_ONLY.add((endpoint_url, model))
        note_forced_tool_choice_ignored(endpoint_url, model)
        body["tool_choice"] = {"type": "auto"}
        return f"Model {model} rejected forced Anthropic tool choice; retrying with auto."
    if "tool_choice" in body and _is_tool_choice_auto_only(status, text):
        _TOOL_CHOICE_AUTO_ONLY.add((endpoint_url, model))
        tc = body["tool_choice"]
        body["tool_choice"] = {"type": "auto"} if isinstance(tc, dict) and "function" not in tc else "auto"
        return f"Model {model} accepts only tool_choice='auto'; retrying with auto."
    if not _is_openrouter(endpoint_url):
        return None
    if "tool_choice" in body and _is_tool_choice_unsupported(status, text):
        _TOOL_CHOICE_UNSUPPORTED.add((endpoint_url, model))
        tc = body.pop("tool_choice")
        return f"Model {model} rejected tool_choice={tc!r}; retrying without it."
    return None
