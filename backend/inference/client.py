from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import httpx

from . import anthropic, endpoint_profiles, text_completion
from .errors import LLMCallError, llm_call_error, llm_stream_error
from .gemma_tool_format import parse_gemma_tool_calls
from .retry import RetryPolicy
from .tool_registry import strictify_schema

logger = logging.getLogger(__name__)


class AbortToken:
    """Shared stop signal for all clients in one turn.

    All clients in a turn hold the same token, so calling ``abort()`` once
    stops every ongoing completion — no per-client fan-out needed.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def abort(self) -> None:
        self._event.set()

    @property
    def is_aborted(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


def reasoning_cfg(on: bool, prefill: str = "") -> dict:
    """Return per-call reasoning parameters for model."""
    return (
        {
            "reasoning": {"enabled": True},
            "chat_template_kwargs": {"enable_thinking": True, "thinking": True},
            "thinking": {"type": "enabled"},
            **({"reasoning_prefill": prefill} if prefill else {}),
        }
        if on
        else {
            "reasoning": {"effort": "none", "enabled": False},
            "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
            "thinking": {"type": "disabled"},
        }
    )


def apply_reasoning_effort(body: dict, effort: str, param: str = "", value: str = "") -> None:
    """Inject the per-model reasoning-effort setting into an outbound chat body.

    Applies only when the call itself has reasoning enabled (the
    ``reasoning_cfg(True)`` shape); reasoning-off calls and callers that sent no
    reasoning params are left untouched. A standard level lands as the OpenAI
    ``reasoning_effort`` param plus the OpenRouter-style ``reasoning.effort``
    mirror; the ``custom`` sentinel sends exactly ``{param: value}`` instead,
    with *value* JSON-decoded when it parses (numbers, objects) and sent as a
    raw string otherwise. Runs before the endpoint profile, so providers that
    reject these params get them stripped there (e.g. DeepSeek's allowlist).
    """
    if not effort:
        return
    reasoning = body.get("reasoning")
    if not (isinstance(reasoning, dict) and reasoning.get("enabled")):
        return
    if effort == "custom":
        if not param:
            return
        try:
            body[param] = json.loads(value)
        except json.JSONDecodeError:
            body[param] = value
        return
    body["reasoning_effort"] = effort
    body["reasoning"] = {**reasoning, "effort": effort}


# RFC 7230 token: the only characters a header name may contain. Must stay
# identical to the API-layer check in schemas.py, so a row saved through the API
# is never silently dropped here.
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")


def parse_extra_headers(text: str) -> dict[str, str]:
    """Parse ``Name: value`` lines into a header dict.

    Blank lines and ``#`` comments are skipped; a malformed line is dropped with
    a warning rather than raised on. The API layer rejects malformed input at
    save time, so this tolerance only ever covers a row that predates that
    validation or was edited in the DB by hand -- such a row degrades to "send
    fewer headers" instead of killing every turn.
    """
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not sep:
            logger.warning("Ignoring extra header line, no colon: %r", line)
            continue
        if not _HEADER_NAME_RE.fullmatch(name):
            logger.warning("Ignoring extra header line, name is not an HTTP token: %r", line)
            continue
        if not value.isascii() or any(ord(c) < 0x20 and c != "\t" for c in value):
            logger.warning("Ignoring extra header line, value is not printable ASCII: %r", line)
            continue
        out[name] = value
    return out


def parse_extra_body(text: str) -> dict:
    """Parse a JSON object of extra body fields; ``{}`` when absent or unusable.

    Permissive for the same reason as :func:`parse_extra_headers`.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        logger.warning("Ignoring extra body: not valid JSON")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("Ignoring extra body: expected a JSON object, got %s", type(parsed).__name__)
        return {}
    return parsed


def _parse_chat_logprobs(choice: Mapping[str, Any]) -> list[dict]:
    """Normalize an OpenAI-compat ``choice.logprobs`` block to Orb's prob shape.

    Thin wrapper over :func:`text_completion.normalize_prob_records`: the
    ``logprobs.content`` records carry the same fields as llama.cpp's
    OpenAI-style ``completion_probabilities`` variant, so one normalizer
    serves both transports and the route frames both the same way.
    """
    logprobs = choice.get("logprobs")
    if not isinstance(logprobs, dict):
        return []
    return text_completion.normalize_prob_records(logprobs.get("content"))


def _text_message(content_parts: list[str], reasoning_parts: list[str]) -> dict:
    """Assemble the free-decoded half of a ``done`` message from its deltas.

    Both transports build the same shape: content and reasoning are included
    only when non-empty, so a pass can test presence rather than emptiness.
    Tool calls and ``finish_reason`` are transport-specific and layered on by
    the caller.
    """
    message: dict = {}
    content = "".join(content_parts)
    if content:
        message["content"] = content
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def _done(label: str, message: dict, usage: dict | None) -> dict:
    """Log the assembled completion and return the terminal ``done`` event."""
    logger.info(
        "LLM complete%s: assembled keys=%s, has_tool_calls=%s, content_len=%s, usage=%s",
        label,
        list(message.keys()),
        "tool_calls" in message,
        len(message.get("content", "") or "") if message.get("content") else "null",
        usage,
    )
    return {"type": "done", "message": message, "usage": usage}


async def _read_error_body(resp: httpx.Response, url: str) -> str:
    """Read and log an HTTP error response's body for upstream detail.

    Streaming responses aren't eagerly read, so ``raise_for_status()`` alone
    would log only the status line. Never raises — an unreadable body degrades
    to a placeholder string.
    """
    try:
        err_text = (await resp.aread()).decode("utf-8", errors="replace")
    except Exception as read_err:
        err_text = f"<failed to read response body: {read_err!r}>"
    logger.error("LLM HTTP %d from %s: %s", resp.status_code, url, err_text)
    return err_text


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 120.0,
        abort_token: AbortToken | None = None,
        completion_mode: str = "chat",
        retry: RetryPolicy | None = None,
        proxy: str | None = None,
        reasoning_effort: str = "",
        reasoning_effort_param: str = "",
        reasoning_effort_value: str = "",
        extra_headers: str = "",
        extra_body: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # Per-model reasoning effort (see apply_reasoning_effort): '' = provider
        # default, a level name, or 'custom' + the param/value pair to send.
        self.reasoning_effort = reasoning_effort
        self.reasoning_effort_param = reasoning_effort_param
        self.reasoning_effort_value = reasoning_effort_value
        # "chat" = OpenAI-compatible /chat/completions; "text" = llama.cpp's
        # native /apply-template + /completion transport (byte-level prompt
        # control). See text_completion.py and _complete_text.
        self.completion_mode = completion_mode
        # Empty string (the settings default = "no proxy") normalizes to None so
        # httpx connects directly; httpx rejects "" as a proxy URL.
        self.proxy = proxy or None
        # Parsed once here rather than on every request.
        self.extra_headers = parse_extra_headers(extra_headers)
        self.extra_body = parse_extra_body(extra_body)
        # Shared across the turn's clients when passed in; otherwise a private
        # token so a standalone client (e.g. a workflow hook) is still abortable.
        self.abort_token = abort_token or AbortToken()
        # Transient-error retry, always on with sensible defaults. See retry.py.
        self.retry = retry or RetryPolicy()

    def abort(self) -> None:
        """Stop all ongoing completions and close their connections."""
        self.abort_token.abort()

    @property
    def is_aborted(self) -> bool:
        return self.abort_token.is_aborted

    def _headers(self) -> dict:
        base: dict = {}
        if self.api_key:
            base["Authorization"] = f"Bearer {self.api_key}"
        # HTTP header names are case-insensitive but dict keys are not, so drop a
        # base header the configured set respells: a lowercase 'authorization'
        # override -- the form most provider docs use -- would otherwise send the
        # Bearer key alongside it. These ride every transport, unlike extra_body.
        configured = {k.lower() for k in self.extra_headers}
        headers = {k: v for k, v in base.items() if k.lower() not in configured}
        headers.update(self.extra_headers)
        return headers

    def _headers_for(self, auth_family: endpoint_profiles.AuthFamily) -> dict:
        """Return transport auth defaults with case-insensitive user overrides."""
        base: dict[str, str] = {}
        if self.api_key:
            if auth_family == "anthropic":
                base["x-api-key"] = self.api_key
            else:
                base["Authorization"] = f"Bearer {self.api_key}"
        if auth_family == "anthropic":
            base["anthropic-version"] = "2023-06-01"
        configured = {key.lower() for key in self.extra_headers}
        headers = {key: value for key, value in base.items() if key.lower() not in configured}
        headers.update(self.extra_headers)
        return headers

    async def list_models(self) -> list[str]:
        """Return model ids advertised by a compatible ``GET /models``.

        Explicit resource shapes have one sibling catalogue. For an ambiguous
        base, discovery walks the same bounded route/auth candidates as
        generation and caches the first catalogue that satisfies the shared
        ``data[].id`` contract. It uses a short finite timeout so failure still
        falls back to Orb's editable model-name field promptly.
        """
        routes = endpoint_profiles.endpoint_candidates(self.base_url)
        seen: set[tuple[str, endpoint_profiles.AuthFamily]] = set()
        contract_error: ValueError | None = None
        last_response: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=20.0, proxy=self.proxy, follow_redirects=True) as client:
            for route in routes:
                candidate = (route.models_url, route.auth_family)
                if candidate in seen:
                    continue
                seen.add(candidate)
                response = await client.get(route.models_url, headers=self._headers_for(route.auth_family))
                if response.status_code >= 400:
                    last_response = response
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    contract_error = ValueError("Endpoint returned a non-JSON models response")
                    continue

                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list):
                    contract_error = ValueError("Endpoint models response does not contain a data list")
                    continue

                endpoint_profiles.note_successful_route(self.base_url, "", route)
                model_ids: set[str] = set()
                for item in data:
                    model_id = item.get("id") if isinstance(item, dict) else None
                    if isinstance(model_id, str) and model_id.strip():
                        normalized = model_id.strip()
                        # This compatibility resource lists ids with a
                        # ``models/`` prefix that generation does not need.
                        if normalized.startswith("models/") and endpoint_profiles.is_gemini_openai_surface(route.url):
                            normalized = normalized.removeprefix("models/")
                        model_ids.add(normalized)
                return sorted(model_ids, key=str.casefold)

        if contract_error is not None:
            raise contract_error
        if last_response is not None:
            last_response.raise_for_status()
        raise ValueError("Endpoint did not expose a models response")

    def _server_root(self) -> str:
        """Server root for llama.cpp native endpoints (/completion, /apply-template,
        /props), which sit beside the OpenAI-compat ``/v1`` surface. Strips a
        trailing ``/v1`` from ``base_url``."""
        b = self.base_url
        return b[:-3] if b.endswith("/v1") else b

    def _uses_text_transport(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        """Return the transport branch :meth:`complete` will initially use."""
        return self.completion_mode == "text" and not text_completion.has_image_parts(messages)

    def _chat_tool_policy(self, model: str, *, tools_in_prompt: bool) -> tuple[bool, bool]:
        """Return ``(structured, sends_schemas)`` for one chat call.

        Both request construction and the pipeline-facing predicate consume this
        answer, so a future endpoint policy cannot change one side without the
        other. ``structured`` stays separate because forced calls still need the
        supplied tuple as the source of their ``response_format`` schema even
        when ``sends_schemas`` is false.
        """
        structured = endpoint_profiles.supports_structured_tool_calls(self.base_url, model)
        return structured, tools_in_prompt and not structured

    def sends_tool_schemas(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        *,
        tools_in_prompt: bool = True,
    ) -> bool:
        """Whether this call shape sends a ``tools`` field on the wire.

        This deliberately describes Orb's request, not the provider's rendered
        prompt: chat templates may narrow or omit a supplied schema array based
        on ``tool_choice``. Text transport never sends schemas; an image-bearing
        text-mode call takes the chat branch and is answered accordingly.

        The caller still owns whether its tools tuple is empty. Keeping that
        separate lets :class:`~backend.pipeline.state.ModelLane` combine the
        frozen cache base with this transport policy without teaching the
        client about pipeline state.
        """
        if self._uses_text_transport(messages):
            return False
        _, sends_schemas = self._chat_tool_policy(model, tools_in_prompt=tools_in_prompt)
        return sends_schemas

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        """Stream one completion and yield deltas followed by the assembled message."""
        # Transport choice and chat-only param scrubbing happen once, outside the
        # retry loop; each attempt re-opens a fresh stream from the same inputs.
        if self._uses_text_transport(messages):
            transport = self._complete_text
        else:
            # Chat transport: prefill (no render step) and raw GBNF grammar are
            # text-mode concepts; drop them so such calls degrade cleanly here.
            # json_schema is NOT dropped: _complete_chat consumes it for
            # structured forced calls (and discards it otherwise).
            params.pop("prefill", None)
            params.pop("grammar", None)
            # Reasoning prefill needs byte control of the prompt; chat mode has no
            # such seam (the provider owns the reasoning channel).
            params.pop("reasoning_prefill", None)
            # n_probs is a llama.cpp /completion field; a text→chat fallback (e.g. a
            # call carrying image parts) must not leak it into the OpenAI-compat body.
            params.pop("n_probs", None)
            transport = self._complete_chat

        # Retry transient server failures via _with_retry, which re-opens a fresh
        # transport stream per attempt.
        async for event in self._with_retry(lambda: transport(messages, model, tools, tool_choice, **params)):
            yield event

    async def _with_retry(self, open_stream: Callable[[], AsyncIterator[dict]]) -> AsyncIterator[dict]:
        """Yield events from ``open_stream()``, re-opening it on a transient failure.

        ``open_stream`` is a zero-arg factory returning a fresh completion stream;
        it is called once per attempt. A retry fires only while no event has been
        yielded -- once the stream emits content, re-issuing would double it, and
        both transports raise before their first event (HTTP status check /
        connect), so "produced is still False" is exactly the clean-retry window.
        """
        attempt = 0
        while True:
            produced = False
            try:
                async for event in open_stream():
                    produced = True
                    yield event
                return
            except httpx.HTTPError as exc:
                stream_event = isinstance(exc, LLMCallError) and exc.stream_event
                if (
                    produced
                    or stream_event
                    or self.is_aborted
                    or attempt >= self.retry.count
                    or not self.retry.should_retry(exc)
                ):
                    raise
                attempt += 1
                detail = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                logger.warning(
                    "LLM retry %d/%d after %s; waiting %.1fs",
                    attempt,
                    self.retry.count,
                    detail,
                    self.retry.delay,
                )
                if not await self._sleep_or_abort(self.retry.delay):
                    raise  # aborted mid-wait: surface the real error, stop retrying

    async def _sleep_or_abort(self, delay: float) -> bool:
        """Wait up to *delay* seconds, returning early if the turn is aborted.

        Returns True if the full delay elapsed, False if aborted first, so the
        retry loop drops out immediately on Stop instead of sleeping out its
        remaining attempts. The abort token is a shared ``asyncio.Event``; waiting
        on it (rather than a bare ``asyncio.sleep``) is what makes the delay
        interruptible.
        """
        if delay <= 0:
            return not self.is_aborted
        try:
            await asyncio.wait_for(self.abort_token.wait(), timeout=delay)
            return False  # abort fired within the delay
        except TimeoutError:
            return True  # full delay elapsed, no abort

    def _audit_structured_reply(self, model: str, content: str, finish_reason: str | None) -> bool:
        """Record that a structured reply did not honor its requested schema."""
        if not content or finish_reason == "length" or self.abort_token.is_aborted:
            return False
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            decoded = None
        # Every schema this path can carry describes an object -- a tool's
        # `function.parameters`, or a caller override narrowing it -- so a
        # decoded scalar or array is as much proof as a decode failure.
        if not isinstance(decoded, dict):
            endpoint_profiles.note_structured_output_ignored(self.base_url, model)
            logger.warning(
                "LLM structured output: %s answered a strict json_schema with a non-object; "
                "demoting to tools+tool_choice for the rest of the session: %s",
                model,
                _preview(content),
            )
            return True
        return False

    async def _complete_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        """The OpenAI-compatible ``/chat/completions`` transport (default)."""
        # Structured forced calls: a forced-function tool_choice is rewritten as
        # a strict ``response_format`` structured-output request -- the chat
        # analogue of text mode's forced grammar (see _complete_text). The
        # provider then grammar-constrains the content to the tool's argument
        # schema, which guarantees byte-exact argument keys where free-decoded
        # tool calls do not (e.g. GLM-5.2 snake-cases hyphenated keys). Two
        # triggers:
        #   * profile opt-in -- the endpoint honors strict json_schema for the
        #     models it fronts (``supports_structured_tool_calls``).
        #   * ``tools_in_prompt=False`` -- the caller's conversation has no
        #     tools in its cached prefix (doc-mode auditor), so the schema must
        #     not touch the prompt at all.
        # The caller-supplied ``json_schema`` (per-fragment director steps)
        # narrows the schema exactly as it narrows the text-mode grammar.
        tools_in_prompt = params.pop("tools_in_prompt", True)
        schema_override = params.pop("json_schema", None)

        def _plan() -> tuple[dict, str | None, bool]:
            """Resolve the current tool policy into ``(body, forced_name, structured)``.

            A closure rather than straight-line code because the policy it reads
            can change *between* the two issues below: a reply that disproves
            structured output demotes the pair mid-call, and the retry has to be
            shaped by the new answer, not the one that already failed.
            """
            structured, sends_schemas = self._chat_tool_policy(model, tools_in_prompt=tools_in_prompt)
            call_tools, call_choice = tools, tool_choice
            forced_name: str | None = None
            extra = dict(params)
            if isinstance(call_choice, dict) and (not tools_in_prompt or structured):
                name = (call_choice.get("function") or {}).get("name")
                schema = schema_override or text_completion.forced_schema(call_tools, call_choice)
                if name and schema:
                    forced_name = name
                    extra["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": name, "strict": True, "schema": strictify_schema(schema)},
                    }
                    call_choice = None
            # Both triggers withhold the tool blob -- and ``tool_choice`` with it --
            # from the body; on a structured-output endpoint that holds for EVERY
            # pass, not just the forced ones. Two reasons:
            #
            # Correctness -- a model that can still see ``tools`` may answer with a
            # native tool call instead, and that path bypasses the schema entirely.
            # DeepSeek rewrites the argument keys when it does (0/39 came back
            # intact under ``tools`` + strict schema, 22/22 without ``tools``), so
            # the caller's lookup by the name it sent silently finds nothing.
            #
            # Caching -- the server renders ``tools`` into the prompt, so dropping
            # it only on forced passes would leave the writer with a different
            # prefix from the director and editor and thrash the shared KV base
            # they sit on (Invariant 3, docs/architecture/kv-cache.md). Dropping it
            # for every pass keeps one stable prefix, and a smaller one. For
            # ``tools_in_prompt=False`` callers the same drop is simply the flag's
            # contract: their prefix never had schemas to begin with.
            #
            # ``tools`` still arrives here: it is the source of the response_format
            # schema built above. If that derivation fails the call goes out with
            # neither tools nor tool_choice and degrades to the parse_tool_calls
            # recovery chain, which is the same posture as any unforced pass.
            if not sends_schemas:
                call_tools = None
                call_choice = None

            body = {
                "model": model,
                "messages": messages,
                "stream": True,
                **extra,
            }
            if call_tools:
                body["tools"] = call_tools
            if call_choice:
                body["tool_choice"] = call_choice
            # Requests usage in the terminal SSE chunk; servers that don't support it silently ignore this field.
            body.setdefault("stream_options", {"include_usage": True})

            apply_reasoning_effort(body, self.reasoning_effort, self.reasoning_effort_param, self.reasoning_effort_value)

            logger.info(
                "LLM complete: model=%s, tools=%s, tool_choice=%s",
                model,
                json.dumps([t["function"]["name"] for t in call_tools]) if call_tools else "None",
                call_choice,
            )
            logger.debug(messages)
            return body, forced_name, structured

        # The body is re-derived on every recovery, auth retry and route probe, so
        # its INFO lines would otherwise repeat once per attempt. Keyed on the
        # rendered line, not on "first attempt only": a recovery that learns a new
        # quirk adds an action, and that line is exactly the one worth surfacing.
        logged_lines: set[str] = set()

        def _log_once(line: str) -> None:
            if line not in logged_lines:
                logged_lines.add(line)
                logger.info("%s", line)

        def _outbound_body(body: dict, route: endpoint_profiles.EndpointRoute) -> dict:
            """Copy the canonical OpenAI body into one route's wire dialect."""
            outbound = dict(body)
            if route.protocol == "openai" and self.extra_body:
                outbound.update(self.extra_body)
                _log_once(f"LLM extra body fields: {sorted(self.extra_body)}")
            for action in endpoint_profiles.prepare_request_body(self.base_url, model, outbound):
                _log_once(f"LLM profile: {action}")
            if route.protocol == "anthropic":
                return anthropic.build_request_body(outbound, self.base_url, model, self.extra_body)
            return outbound

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: dict | None = None

        async def _issue(body: dict, forced_name: str | None) -> AsyncIterator[dict]:
            """Stream one request into the accumulators, replacing anything already there.

            Yielding is the reason this is a generator and not a coroutine: the
            content/reasoning deltas belong to the caller as they arrive. A
            second issue re-yields its own reasoning, so a retried call shows
            two thinking runs in the pass's box -- the honest picture of what
            was spent, and bounded to once per model per process.
            """
            nonlocal finish_reason, usage
            content_parts.clear()
            reasoning_parts.clear()
            tool_calls_acc.clear()
            finish_reason = None
            usage = None

            def tool_entry(index: int) -> dict:
                if index not in tool_calls_acc:
                    tool_calls_acc[index] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                return tool_calls_acc[index]

            async def consume_openai(resp) -> AsyncIterator[dict]:
                nonlocal finish_reason, usage
                # Slot last handed to an index-less delta; -1 before the first.
                unindexed = -1

                def slot_for(tc_delta: Mapping[str, Any]) -> int:
                    """Resolve one tool-call delta to an accumulator slot.

                    Google's OpenAI-compatible surface omits ``index`` from
                    ``delta.tool_calls`` entirely, so keying on ``index`` with a
                    default of 0 merged every parallel call into one entry --
                    names concatenated, all but the first argument payload lost.
                    Without an index, a delta that STARTS a call (it carries an
                    ``id`` or a function ``name``, which the OpenAI contract
                    sends only on a call's first chunk) opens the next free
                    slot; a bare argument continuation appends to the newest.
                    """
                    nonlocal unindexed
                    index = tc_delta.get("index")
                    if isinstance(index, int) and not isinstance(index, bool):
                        return index
                    function = tc_delta.get("function")
                    starts = bool(tc_delta.get("id")) or bool(isinstance(function, Mapping) and function.get("name"))
                    if starts or unindexed < 0:
                        unindexed = max([*tool_calls_acc, unindexed], default=-1) + 1
                    return unindexed

                async for payload in self._iter_sse_payloads(resp):
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    u = chunk.get("usage")
                    if isinstance(u, dict):
                        usage = u
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    try:
                        choice = choices[0]
                        delta = choice.get("delta", {})
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        if rc:
                            reasoning_parts.append(rc)
                            yield {"type": "reasoning", "delta": rc}
                        content = delta.get("content")
                        if content:
                            content_parts.append(content)
                            if forced_name is None:
                                yield {"type": "content", "delta": content}
                        for rec in _parse_chat_logprobs(choice):
                            yield {"type": "token_probs", **rec}
                        for tc_delta in delta.get("tool_calls") or []:
                            entry = tool_entry(slot_for(tc_delta))
                            if tc_delta.get("id"):
                                entry["id"] = tc_delta["id"]
                            fn = tc_delta.get("function", {})
                            if fn.get("name"):
                                entry["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                entry["function"]["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    except (KeyError, IndexError):
                        continue

            async def consume_anthropic(resp, url: str) -> AsyncIterator[dict]:
                nonlocal finish_reason, usage
                stopped = False
                async for payload in self._iter_sse_payloads(resp):
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    if event_type == "ping":
                        continue
                    if event_type == "error":
                        raise llm_stream_error(payload=event, url=url, model=model, api_key=self.api_key)
                    if event_type == "message_start":
                        initial = (event.get("message") or {}).get("usage")
                        if isinstance(initial, dict):
                            usage = dict(initial)
                    elif event_type == "content_block_start":
                        index = event.get("index", 0)
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            entry = tool_entry(index)
                            entry["id"] = block.get("id", "")
                            entry["function"]["name"] = block.get("name", "")
                            if block.get("input"):
                                entry["function"]["arguments"] = json.dumps(block["input"], separators=(",", ":"))
                        elif block.get("type") == "text" and block.get("text"):
                            content_parts.append(block["text"])
                            if forced_name is None:
                                yield {"type": "content", "delta": block["text"]}
                        elif block.get("type") == "thinking" and block.get("thinking"):
                            reasoning_parts.append(block["thinking"])
                            yield {"type": "reasoning", "delta": block["thinking"]}
                    elif event_type == "content_block_delta":
                        index = event.get("index", 0)
                        delta = event.get("delta") or {}
                        delta_type = delta.get("type")
                        if delta_type == "text_delta" and delta.get("text"):
                            content_parts.append(delta["text"])
                            # Same gate as consume_openai: a forced pass buffers its
                            # text as the tool-arguments payload instead of streaming
                            # it, so the caller never sees a half-built JSON body.
                            if forced_name is None:
                                yield {"type": "content", "delta": delta["text"]}
                        elif delta_type == "thinking_delta" and delta.get("thinking"):
                            reasoning_parts.append(delta["thinking"])
                            yield {"type": "reasoning", "delta": delta["thinking"]}
                        elif delta_type == "input_json_delta" and delta.get("partial_json"):
                            tool_entry(index)["function"]["arguments"] += delta["partial_json"]
                    elif event_type == "message_delta":
                        delta = event.get("delta") or {}
                        stop_reason = delta.get("stop_reason")
                        if stop_reason:
                            finish_reason = {
                                "end_turn": "stop",
                                "stop_sequence": "stop",
                                "tool_use": "tool_calls",
                                "max_tokens": "length",
                            }.get(stop_reason, stop_reason)
                        update = event.get("usage")
                        if isinstance(update, dict):
                            usage = {**(usage or {}), **update}
                    elif event_type == "message_stop":
                        stopped = True
                        break
                if not stopped and not self.is_aborted:
                    raise llm_stream_error(
                        payload={"error": {"message": "Anthropic stream ended before message_stop"}},
                        url=url,
                        model=model,
                        api_key=self.api_key,
                    )

            routes = endpoint_profiles.endpoint_candidates(self.base_url, model)
            route_index = 0
            while route_index < len(routes):
                route = routes[route_index]
                recovery_count = 0
                auth_family = route.auth_family
                auth_retried = False
                while True:
                    outbound = _outbound_body(body, route)
                    # No read timeout on streaming calls: a long prefill silence
                    # is normal; abort and disconnect close the stream instead.
                    async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, read=None), proxy=self.proxy) as client:
                        async with client.stream(
                            "POST", route.url, json=outbound, headers=self._headers_for(auth_family)
                        ) as resp:
                            if resp.status_code >= 400:
                                err_text = await _read_error_body(resp, route.url)
                                # One attempt per independent quirk class: the profile
                                # rules, a refused reasoning_effort level, Anthropic
                                # sampling controls, and the 4.6+ reasoning fields can
                                # each need their own rejection before a body this
                                # endpoint accepts is reached. No route reaches more
                                # than three of them -- reasoning_effort is an
                                # OpenAI-body field the Anthropic translation drops.
                                if recovery_count < 3:
                                    fix = endpoint_profiles.recover_from_error(
                                        self.base_url, model, outbound, resp.status_code, err_text
                                    )
                                    if fix is None and route.protocol == "anthropic":
                                        fix = anthropic.recover_sampling_error(
                                            self.base_url, model, outbound, resp.status_code, err_text
                                        ) or anthropic.recover_thinking_error(
                                            self.base_url, model, outbound, resp.status_code, err_text
                                        )
                                    if fix is not None:
                                        recovery_count += 1
                                        logger.warning("LLM recovery: %s", fix)
                                        continue
                                auths = endpoint_profiles.auth_families(route, resp.status_code, err_text)
                                if not auth_retried and len(auths) > 1:
                                    auth_family = auths[1]
                                    auth_retried = True
                                    logger.warning("LLM auth recovery: retrying %s with %s auth", route.url, auth_family)
                                    continue
                                if route_index + 1 < len(routes) and endpoint_profiles.should_probe_route(
                                    resp.status_code, err_text
                                ):
                                    logger.warning(
                                        "LLM endpoint probe: %s rejected the route; trying %s",
                                        route.url,
                                        routes[route_index + 1].url,
                                    )
                                    route_index += 1
                                    break
                                raise llm_call_error(
                                    response=resp,
                                    body=err_text,
                                    url=route.url,
                                    model=model,
                                    api_key=self.api_key,
                                )
                            if route.protocol == "anthropic":
                                async for event in consume_anthropic(resp, route.url):
                                    yield event
                            else:
                                async for event in consume_openai(resp):
                                    yield event
                    endpoint_profiles.note_successful_route(self.base_url, model, route)
                    return

        body, forced_name, structured = _plan()
        async for _ev in _issue(body, forced_name):
            yield _ev

        # A structured forced call that came back disproving its own schema:
        # demote the pair and re-issue once under the new policy. The wasted
        # attempt is otherwise a whole pass lost per process -- and this is the
        # one moment a retry is clean, because a forced call buffers its content
        # rather than streaming it, so nothing but reasoning has reached the
        # caller yet. Same shape as workflows/_forced_call.py's retry after
        # note_forced_tool_choice_ignored.
        #
        # ``tools_in_prompt`` is the gate: a caller whose prefix must stay
        # schema-free has no second shape to fall back to -- re-planning would
        # rebuild the identical request -- so it demotes for everyone else's
        # benefit and keeps its own degraded reply.
        # The tracker sees only the surviving attempt's usage, so a retried call
        # under-reports its true token cost by the discarded one.
        if forced_name is not None and structured and tools_in_prompt and not tool_calls_acc:
            if self._audit_structured_reply(model, "".join(content_parts), finish_reason):
                body, forced_name, structured = _plan()
                async for _ev in _issue(body, forced_name):
                    yield _ev

        # Assemble the final message dict (mirrors the non-streaming message format)
        if forced_name is not None and not tool_calls_acc:
            # Structured forced call: the constrained content IS the arguments
            # JSON; re-synthesize the tool-call shape the pipeline expects. A
            # provider that answered with real tool_calls anyway wins below.
            message = text_completion.forced_tool_message(forced_name, "".join(content_parts))
            message.update(_text_message([], reasoning_parts))
        else:
            message = _text_message(content_parts, reasoning_parts)
        if tool_calls_acc:
            message["tool_calls"] = [
                {
                    "id": v["id"],
                    "type": "function",
                    "function": {
                        "name": v["function"]["name"],
                        "arguments": v["function"]["arguments"],
                    },
                }
                for v in (tool_calls_acc[k] for k in sorted(tool_calls_acc))
            ]
        if finish_reason:
            message["finish_reason"] = finish_reason

        yield _done("", message, usage)

    async def _iter_sse_payloads(self, resp) -> AsyncIterator[str]:
        """Yield each SSE ``data:`` payload string, racing reads against abort.

        Each line read is raced against the abort signal so ``client.abort()``
        breaks out immediately, letting the caller's ``async with`` exit
        *normally* and cleanly close the TCP connection to the LLM server.
        (asyncio task cancellation instead would leave the connection open under
        Python 3.11+ strict cancellation semantics.) Stops at ``[DONE]``. Shared
        by the chat and text transports so the abort race lives in one place.
        """
        aiter = resp.aiter_lines().__aiter__()
        abort_wait = asyncio.create_task(self.abort_token.wait())
        try:
            while True:
                line_task = asyncio.ensure_future(aiter.__anext__())
                try:
                    done, _ = await asyncio.wait(
                        {line_task, abort_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except BaseException:
                    line_task.cancel()
                    raise

                if abort_wait in done:
                    line_task.cancel()
                    try:
                        await line_task
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                    return  # stop iterating → async-with closes connection cleanly

                try:
                    line = line_task.result()
                except StopAsyncIteration:
                    return

                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    return
                yield payload
        finally:
            abort_wait.cancel()
            try:
                await abort_wait
            except asyncio.CancelledError:
                pass

    async def _apply_template(
        self,
        server_root: str,
        messages: Sequence[Mapping[str, Any]],
        chat_template_kwargs: Mapping[str, Any] | None = None,
    ) -> str:
        """Render *messages* to a prompt string via llama.cpp ``POST /apply-template``.

        *chat_template_kwargs* (e.g. ``{"enable_thinking": False}``) is forwarded so
        the template renders its own reasoning on/off bytes — see ``_complete_text``.
        """
        body: dict[str, Any] = {"messages": list(messages)}
        if chat_template_kwargs is not None:
            body["chat_template_kwargs"] = dict(chat_template_kwargs)
        async with httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy) as client:
            resp = await client.post(
                f"{server_root}/apply-template",
                json=body,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()["prompt"]

    async def _fetch_chat_template(self, server_root: str) -> str:
        """Fetch the server's chat template, returning empty text on failure."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy) as client:
                resp = await client.get(f"{server_root}/props", headers=self._headers())
                resp.raise_for_status()
                return resp.json().get("chat_template", "") or ""
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("text mode: /props fetch failed (%r); reasoning toggle disabled this call", e)
            return ""

    async def _reasoning_format(self, server_root: str) -> text_completion.ReasoningFormat:
        """Sniff the loaded model's reasoning format for this call."""
        return await text_completion.get_reasoning_format(lambda: self._fetch_chat_template(server_root))

    async def _stream_completion(self, url: str, body: dict) -> AsyncIterator[dict]:
        """POST *body* to llama.cpp ``/completion`` and yield each parsed SSE chunk.

        Races reads against abort via the shared :meth:`_iter_sse_payloads`.
        The single HTTP seam of the text transport (patched wholesale in tests,
        which is why the signature stays exactly ``(url, body)``).

        A rejection is raised as :class:`LLMCallError` with no model attributed:
        llama.cpp's native ``/completion`` serves whichever model the server
        loaded and the body never names one, so there is nothing honest to put
        there.
        """
        # read=None for the same reason as the chat transport: llama.cpp is
        # silent for the whole prefill, which legitimately exceeds any flat
        # read timeout on long contexts.
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, read=None), proxy=self.proxy) as client:
            async with client.stream("POST", url, json=body, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    err_text = await _read_error_body(resp, url)
                    raise llm_call_error(response=resp, body=err_text, url=url, model="", api_key=self.api_key)
                async for payload in self._iter_sse_payloads(resp):
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue

    async def render_prompt(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        prefill: str | None = None,
        reasoning: bool = False,
        fmt: text_completion.ReasoningFormat | None = None,
    ) -> str:
        """Render *messages* to the exact prompt string ``_complete_text`` sends.

        Text-transport only. Replicates the transport's render step byte-for-byte
        (trailing open assistant turn for *prefill*; ``enable_thinking`` kwargs
        only when there is no prefill) so a caller can re-derive a past
        generation's prompt and byte-extend it via :meth:`complete_raw` — the
        doc-mode auditor's KV-parity hook. Template quirks (e.g. Qwen injecting
        ``<think></think>`` into the generation prompt) are reproduced for free
        because it is the same render, not a reconstruction.

        For routed-channel models, *fmt* also reproduces the non-reasoning
        header written by the transport. Only the unconstrained render is
        reproduced; a grammar-forced call takes one further tail in
        ``_complete_text``, and nothing re-derives such a prompt.
        """
        server_root = self._server_root()
        render_msgs: list[Mapping[str, Any]] = list(messages)
        if prefill:
            render_msgs = [*render_msgs, {"role": "assistant", "content": prefill}]
        ctk = None if prefill else {"enable_thinking": reasoning, "thinking": reasoning}
        prompt = await self._apply_template(server_root, render_msgs, ctk)
        if not reasoning and not prefill:
            fmt = fmt or await self._reasoning_format(server_root)
            if text_completion.opens_reply_channel(fmt, reasoning=reasoning, prefill=bool(prefill)):
                prompt += fmt.disable_bytes
        return prompt

    async def _complete_text(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        """llama.cpp native text-completion transport (``/apply-template`` + ``/completion``).

        Preserves the ``complete()`` event contract. Falls back to the chat
        transport on any ``/apply-template`` HTTP error (odd templates/shapes).
        See text_completion.py for the pure helpers.
        """
        prefill = params.pop("prefill", None)
        grammar = params.pop("grammar", None)
        # Popped before the /apply-template try below so the chat fallback never
        # leaks it into an OpenAI-compat body either.
        rprefill = params.pop("reasoning_prefill", "") or ""
        schema_override = params.pop("json_schema", None)
        # No-op here: tools are never rendered into a text-mode prompt. If
        # /apply-template fails, the chat fallback below explicitly preserves
        # that invariant by withholding them there too.
        params.pop("tools_in_prompt", None)
        server_root = self._server_root()
        reasoning_on = text_completion.reasoning_enabled(params)
        fmt = await self._reasoning_format(server_root)
        # Let the chat template control tag-pair reasoning; prefills own their
        # trailing assistant turn.
        try:
            prompt = await self.render_prompt(messages, prefill=prefill, reasoning=reasoning_on, fmt=fmt)
        except httpx.HTTPError as e:
            logger.warning("text mode: /apply-template failed (%r); falling back to chat transport", e)
            async for event in self._complete_chat(messages, model, tools, tool_choice, tools_in_prompt=False, **params):
                yield event
            return

        open_bytes = fmt.open_bytes
        # Prime the splitter from the rendered prompt, not the requested flag.
        pre_opened = bool(open_bytes) and prompt.rstrip().endswith(open_bytes.rstrip())
        content_open = text_completion.opens_reply_channel(fmt, reasoning=reasoning_on, prefill=bool(prefill))

        # Seed a reasoning prefill in the prompt tail; assistant prefills already
        # own that tail.
        if rprefill and reasoning_on and open_bytes and not prefill:
            if not pre_opened:
                prompt += open_bytes
                pre_opened = True
            prompt += rprefill
        else:
            rprefill = ""  # no-op: reasoning off, non-thinking model, or assistant-prefill call

        # Forced tool_choice → grammar-constrain the whole output to the tool's
        # JSON schema. tools is otherwise unused in text mode (never rendered).
        # A caller-supplied json_schema narrows the forced grammar per call
        # (e.g. one direct_scene field per step) — decoding-only, so the prompt
        # bytes and KV cache are untouched.
        schema = text_completion.forced_schema(tools, tool_choice)
        if schema is not None and schema_override is not None:
            schema = schema_override
        forced_name: str | None = None
        if schema is not None and isinstance(tool_choice, dict):
            forced_name = (tool_choice.get("function") or {}).get("name")

        # A constrained pass cannot route: the grammar owns every sampled token,
        # so the channel shape's routing header is not among the tokens the model
        # may emit and its JSON would open where the template expects that header.
        # Write the reply channel's header for it. Reasoning off already did so in
        # render_prompt, and an open thought span is free text where the JSON is
        # as fine as it is on a tag-pair model -- hence ``not pre_opened``.
        if (grammar is not None or schema is not None) and reasoning_on and not prefill and not pre_opened:
            if fmt.disable_bytes:
                prompt += fmt.disable_bytes
                content_open = True

        body = text_completion.build_completion_params(params)
        body["prompt"] = prompt
        body["stream"] = True
        if grammar is not None:
            # Caller-supplied raw GBNF wins over the schema-derived grammar: a
            # prefilled call continues mid-JSON, where json_schema (which
            # constrains a fresh, complete object) would reject the remainder.
            body["grammar"] = grammar
        elif schema is not None:
            body["json_schema"] = schema

        logger.info(
            "LLM complete (text): model=%s, forced=%s, reasoning=%s, prefill=%s, grammar=%s",
            model,
            forced_name,
            reasoning_on,
            bool(prefill),
            bool(grammar),
        )

        # trim_lead off on a prefilled call: the stream continues an open assistant
        # turn, so a leading space is the word separator, not template padding.
        start: text_completion.SplitterStart = "reasoning" if pre_opened else "content" if prefill or content_open else "auto"
        splitter = text_completion.make_splitter(fmt, start=start, trim_lead=not prefill)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        forced_buf: list[str] = []
        usage: dict | None = None
        finish_reason: str | None = None
        async for data in self._stream_completion(f"{server_root}/completion", body):
            if rprefill:
                # First chunk only: the POST is known good here, so _with_retry's
                # clean-retry window (no event yielded yet) is preserved.
                reasoning_parts.append(rprefill)
                yield {"type": "reasoning", "delta": rprefill}
                rprefill = ""
            stop = bool(data.get("stop"))
            if stop:
                usage, finish_reason = text_completion.terminal_state(data)
            delta = data.get("content") or ""
            if delta:
                if forced_name is not None:
                    # Forced call: buffer as arguments, emit no content deltas
                    # (mirrors chat mode, where args stream as tool_calls deltas
                    # the pipeline doesn't surface).
                    forced_buf.append(delta)
                else:
                    for kind, text in splitter.feed(delta):
                        (reasoning_parts if kind == "reasoning" else content_parts).append(text)
                        yield {"type": kind, "delta": text}
            # Per-token alternatives ride a separate channel (Document mode's
            # token-swap steering); never for a forced tool call, whose output is
            # buffered as arguments rather than surfaced as content.
            if forced_name is None:
                for rec in text_completion.parse_token_probs(data):
                    yield {"type": "token_probs", **rec}
            if stop:
                break

        if forced_name is not None:
            # The arguments are the whole assistant turn: the prompt-side
            # prefill bytes plus the generated continuation.
            message = text_completion.forced_tool_message(forced_name, (prefill or "") + "".join(forced_buf))
        else:
            for kind, text in splitter.flush():
                (reasoning_parts if kind == "reasoning" else content_parts).append(text)
                yield {"type": kind, "delta": text}
            message = _text_message(content_parts, reasoning_parts)
        if finish_reason:
            message["finish_reason"] = finish_reason

        yield _done(" (text)", message, usage)

    async def complete_raw(self, prompt: str, model: str, **params) -> AsyncIterator[dict]:
        """Stream a raw completion for prompt."""
        async for event in self._with_retry(lambda: self._complete_raw(prompt, **params)):
            yield event

    async def _complete_raw(self, prompt: str, **params) -> AsyncIterator[dict]:
        """Raw ``/completion`` stream backing :meth:`complete_raw` (one attempt)."""
        grammar = params.pop("grammar", None)
        schema = params.pop("json_schema", None)
        body = text_completion.build_completion_params(params)
        body["prompt"] = prompt
        body["stream"] = True
        if grammar is not None:
            body["grammar"] = grammar
        elif schema is not None:
            body["json_schema"] = schema

        logger.info(
            "LLM complete_raw (text): prompt_len=%d, n_predict=%s, constrained=%s",
            len(prompt),
            body.get("n_predict"),
            bool(grammar or schema),
        )

        content_parts: list[str] = []
        usage: dict | None = None
        finish_reason: str | None = None
        async for data in self._stream_completion(f"{self._server_root()}/completion", body):
            stop = bool(data.get("stop"))
            if stop:
                usage, finish_reason = text_completion.terminal_state(data)
            delta = data.get("content") or ""
            if delta:
                content_parts.append(delta)
                yield {"type": "content", "delta": delta}
            # Per-token alternatives on a separate channel (Document mode). Absent
            # unless the caller passed n_probs, so this is a no-op by default.
            for rec in text_completion.parse_token_probs(data):
                yield {"type": "token_probs", **rec}
            if stop:
                break

        message: dict = {"content": "".join(content_parts)}
        if finish_reason:
            message["finish_reason"] = finish_reason
        yield {"type": "done", "message": message, "usage": usage}


def client_from_settings(settings: Mapping[str, Any], *, abort_token: AbortToken | None = None) -> LLMClient:
    """Build the writer :class:`LLMClient` from a settings row.

    The single construction seam for writer clients: ``LLMClient`` is resolved
    from this module's globals at call time, so tests substitute the client
    everywhere by patching ``backend.inference.client.LLMClient`` alone.
    """
    return LLMClient(
        settings["endpoint_url"],
        api_key=settings.get("api_key", ""),
        abort_token=abort_token,
        completion_mode=settings.get("completion_mode", "chat"),
        proxy=settings.get("proxy"),
        reasoning_effort=settings.get("reasoning_effort", ""),
        reasoning_effort_param=settings.get("reasoning_effort_param", ""),
        reasoning_effort_value=settings.get("reasoning_effort_value", ""),
        extra_headers=settings.get("extra_headers", ""),
        extra_body=settings.get("extra_body", ""),
    )


def agent_client_from_settings(settings: Mapping[str, Any], *, abort_token: AbortToken | None = None) -> LLMClient:
    """Build the dual-model agent :class:`LLMClient` from a settings row.

    Agent endpoint/key fall back to the writer's when the agent columns are
    unset. Same patch seam as :func:`client_from_settings`.
    """
    return LLMClient(
        settings.get("agent_endpoint_url", settings["endpoint_url"]),
        api_key=settings.get("agent_api_key", settings.get("api_key", "")),
        abort_token=abort_token,
        completion_mode=settings.get("agent_completion_mode", "chat"),
        proxy=settings.get("agent_proxy", settings.get("proxy")),
        reasoning_effort=settings.get("agent_reasoning_effort", settings.get("reasoning_effort", "")),
        reasoning_effort_param=settings.get("agent_reasoning_effort_param", settings.get("reasoning_effort_param", "")),
        reasoning_effort_value=settings.get("agent_reasoning_effort_value", settings.get("reasoning_effort_value", "")),
        extra_headers=settings.get("agent_extra_headers", settings.get("extra_headers", "")),
        extra_body=settings.get("agent_extra_body", settings.get("extra_body", "")),
    )


def separate_agent_lane_configured(settings: Mapping[str, Any]) -> bool:
    """Whether settings resolve to a usable, physically separate Agent lane."""
    return (
        not bool(settings.get("agent_same_as_writer", True))
        and bool(settings.get("agent_endpoint_id"))
        and bool(settings.get("agent_endpoint_url"))
        and bool(settings.get("agent_model_name"))
    )


def agent_lane_from_settings(
    settings: Mapping[str, Any],
    *,
    writer_client: LLMClient,
    abort_token: AbortToken | None = None,
) -> tuple[LLMClient, str]:
    """Resolve the client/model pair used by agentic off-turn work.

    The pipeline represents single-model mode by reusing the writer lane and
    dual-model mode by constructing a separate agent lane. Workflow HTTP routes
    need the same resolution without importing ``pipeline`` upward into ``api``:
    reuse the already-built ``writer_client`` unless a concrete separate agent
    endpoint is selected, otherwise construct the configured agent client.
    ``separate_agent_lane_configured`` already guarantees ``agent_model_name``.
    """
    if separate_agent_lane_configured(settings):
        return (
            agent_client_from_settings(settings, abort_token=abort_token),
            settings["agent_model_name"],
        )
    return writer_client, settings["model_name"]


def _preview(text: str, limit: int = 200) -> str:
    """A single-line, length-capped excerpt of *text* for a log line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _balanced_span(text: str, open_ch: str, close_ch: str) -> str | None:
    """Return the first brace-balanced ``open_ch``…``close_ch`` slice of *text*.

    String-aware: braces inside a JSON string literal (and escaped quotes
    inside one) do not move the depth counter, so a payload whose values are
    prose full of punctuation still closes at the right place. Returns ``None``
    when *text* has no opener or never returns to depth zero.
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _first_json(text: str, open_ch: str, close_ch: str) -> Any | None:
    """Decode the first balanced ``open_ch``…``close_ch`` value in *text*, or ``None``."""
    span = _balanced_span(text, open_ch, close_ch)
    if span is None:
        return None
    try:
        return json.loads(span)
    except json.JSONDecodeError:
        return None


def _sanitize_args(obj):
    """Recursively strip tokenizer-artifact quote tokens (``<|"|>``) from string values."""
    if isinstance(obj, str):
        return obj.replace('<|"|>', "")
    if isinstance(obj, list):
        return [_sanitize_args(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_args(v) for k, v in obj.items()}
    return obj


def _make_tool_call(name: str, arguments) -> dict:
    """Build a normalized tool-call dictionary."""
    raw = arguments if isinstance(arguments, str) else None
    if raw is not None:
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = _first_json(raw, "{", "}")
            if isinstance(arguments, dict):
                logger.warning("Tool %s: arguments salvaged from a non-JSON wrapper: %s", name, _preview(raw))
    if not isinstance(arguments, dict):
        logger.warning(
            "Tool %s: arguments are not a JSON object; the call degrades to no arguments: %s",
            name,
            _preview(raw if raw is not None else repr(arguments)),
        )
        arguments = {}
    return {"name": name, "arguments": _sanitize_args(arguments)}


def parse_tool_calls(message: dict) -> list[dict]:
    """Extract tool calls from a completion message.

    Tries, in order: the standard ``tool_calls`` array, Hermes-style
    ``<tool_call>...</tool_call>`` tags, Gemma 4 native
    ``<|tool_call>call:NAME{...}<tool_call|>`` tokens, then JSON embedded in
    the content body (common with some local servers).
    """
    tool_calls = []

    # Standard OpenAI tool_calls format
    if "tool_calls" in message and message["tool_calls"]:
        for tc in message["tool_calls"]:
            fn = tc.get("function", {})
            tool_calls.append(_make_tool_call(fn.get("name", ""), fn.get("arguments", "{}")))
        return tool_calls

    # Fallback: try to parse JSON from content
    content = message.get("content", "")
    if not content:
        return []

    # Hermes-style <tool_call>...</tool_call> tags
    for match in re.finditer(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL):
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict) and "name" in parsed:
                tool_calls.append(_make_tool_call(parsed["name"], parsed.get("arguments", {})))
        except json.JSONDecodeError:
            pass
    if tool_calls:
        return tool_calls

    # Gemma 4 native <|tool_call>call:NAME{...}<tool_call|> tokens
    gemma_calls = parse_gemma_tool_calls(content)
    if gemma_calls:
        return [_make_tool_call(c["name"], c["arguments"]) for c in gemma_calls]

    # Try to find JSON objects or arrays in the content
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        parsed = _first_json(content, start_char, end_char)
        if isinstance(parsed, dict) and "name" in parsed:
            tool_calls.append(_make_tool_call(parsed["name"], parsed.get("arguments", {})))
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and "name" in item:
                    tool_calls.append(_make_tool_call(item["name"], item.get("arguments", {})))
        if tool_calls:
            return tool_calls

    return tool_calls
