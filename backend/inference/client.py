from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import Any, AsyncIterator, Mapping, Sequence

import httpx

from . import endpoint_profiles, text_completion
from .gemma_tool_format import parse_gemma_tool_calls

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


def reasoning_cfg(on: bool) -> dict:
    """Reasoning params dict to spread into a ``client.complete()`` call.

    Covers all known API styles in one place (OpenAI-style, llama.cpp,
    Anthropic thinking).

    ``chat_template_kwargs`` carries two aliases for the same toggle because
    templates disagree on the name: Qwen3/Gemma read ``enable_thinking``; Kimi K2
    reads a boolean ``thinking``. Each template reads only the name it knows and
    ignores the other, so sending both is safe and makes the toggle actually take
    on all of them (a template that silently ignores the flag would keep thinking).
    """
    return (
        {
            "reasoning": {"effort": "low", "enabled": True},
            "chat_template_kwargs": {"enable_thinking": True, "thinking": True},
            "thinking": {"type": "enabled"},
        }
        if on
        else {
            "reasoning": {"effort": "none", "enabled": False},
            "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
            "thinking": {"type": "disabled"},
        }
    )


def _parse_chat_logprobs(choice: Mapping[str, Any]) -> list[dict]:
    """Normalize an OpenAI-compat ``choice.logprobs`` block to Orb's prob shape.

    Reads ``logprobs.content`` — an array of
    ``{token, logprob, top_logprobs:[{token, logprob}]}`` — folding each into
    ``{"token", "prob", "top": [{"t","p"}]}`` with linear probabilities
    (``math.exp``). This is the chat-transport twin of
    ``text_completion.parse_token_probs``; the output shape is identical so the
    route frames both the same way. Returns ``[]`` when the provider omits
    logprobs (graceful degrade → no popup) and skips malformed records; never
    raises.
    """
    logprobs = choice.get("logprobs")
    if not isinstance(logprobs, dict):
        return []
    content = logprobs.get("content")
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for rec in content:
        if not isinstance(rec, dict) or not isinstance(rec.get("token"), str):
            continue
        try:
            prob = math.exp(float(rec["logprob"]))
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        top: list[dict] = []
        for alt in rec.get("top_logprobs") or []:
            if not isinstance(alt, dict) or not isinstance(alt.get("token"), str):
                continue
            try:
                top.append({"t": alt["token"], "p": math.exp(float(alt["logprob"]))})
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
        out.append({"token": rec["token"], "prob": prob, "top": top})
    return out


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 120.0,
        abort_token: AbortToken | None = None,
        completion_mode: str = "chat",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # "chat" = OpenAI-compatible /chat/completions; "text" = llama.cpp's
        # native /apply-template + /completion transport (byte-level prompt
        # control). See text_completion.py and _complete_text.
        self.completion_mode = completion_mode
        # Shared across the turn's clients when passed in; otherwise a private
        # token so a standalone client (e.g. a workflow hook) is still abortable.
        self.abort_token = abort_token or AbortToken()

    def abort(self) -> None:
        """Stop all ongoing completions and close their connections."""
        self.abort_token.abort()

    @property
    def is_aborted(self) -> bool:
        return self.abort_token.is_aborted

    def _headers(self) -> dict:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _server_root(self) -> str:
        """Server root for llama.cpp native endpoints (/completion, /apply-template,
        /props), which sit beside the OpenAI-compat ``/v1`` surface. Strips a
        trailing ``/v1`` from ``base_url``."""
        b = self.base_url
        return b[:-3] if b.endswith("/v1") else b

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        """Stream a completion. Yields deltas then a final assembled message.

        Transport is chosen by ``completion_mode``: text mode routes through
        :meth:`_complete_text` (llama.cpp native), except calls carrying image
        parts, which fall back to chat (no text-mode multimodal path yet).

        Yields:
            ``{"type": "reasoning", "delta": str}`` — zero or more reasoning chunks
            ``{"type": "content",   "delta": str}`` — zero or more content chunks
            ``{"type": "done", "message": dict, "usage": dict | None}``
                — assembled message (content and/or tool_calls) and the
                  provider usage object (``None`` when the server omits it).
        """
        if self.completion_mode == "text" and not text_completion.has_image_parts(messages):
            async for event in self._complete_text(messages, model, tools, tool_choice, **params):
                yield event
            return
        # Chat transport: prefill (no render step), raw GBNF grammar, and the
        # per-call json_schema narrowing are text-mode concepts; drop them so
        # such calls degrade cleanly here.
        params.pop("prefill", None)
        params.pop("grammar", None)
        params.pop("json_schema", None)
        # n_probs is a llama.cpp /completion field; a text→chat fallback (e.g. a
        # call carrying image parts) must not leak it into the OpenAI-compat body.
        params.pop("n_probs", None)
        async for event in self._complete_chat(messages, model, tools, tool_choice, **params):
            yield event

    async def _complete_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        """The OpenAI-compatible ``/chat/completions`` transport (default)."""
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            **params,
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        # Requests usage in the terminal SSE chunk; servers that don't support it silently ignore this field.
        body.setdefault("stream_options", {"include_usage": True})

        # Provider-specific body translation (profiles + session-learned
        # workarounds) lives entirely in endpoint_profiles; the client just
        # applies whatever it returns.
        for action in endpoint_profiles.prepare_request_body(self.base_url, model, body):
            logger.info("LLM profile: %s", action)

        logger.info(
            "LLM complete: model=%s, tools=%s, tool_choice=%s",
            model,
            json.dumps([t["function"]["name"] for t in tools]) if tools else "None",
            tool_choice,
        )
        logger.debug(messages)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: dict | None = None

        # At most one retry, solely to self-heal a provider quirk that
        # endpoint_profiles.recover_from_error() recognises (e.g. an OpenRouter
        # model rejecting tool_choice). The error lands before any SSE event,
        # so the retry is clean.
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", self._url(), json=body, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        # Concern 1: surface the error body. Streaming responses
                        # aren't eagerly read, so raise_for_status() alone would log
                        # only the status line -- read the body for upstream detail.
                        try:
                            err_bytes = await resp.aread()
                            err_text = err_bytes.decode("utf-8", errors="replace")
                        except Exception as read_err:
                            err_text = f"<failed to read response body: {read_err!r}>"
                        logger.error(
                            "LLM HTTP %d from %s: %s",
                            resp.status_code,
                            self._url(),
                            err_text,
                        )

                        # Concern 2: ask the provider layer whether this is a
                        # recognised quirk worth one retry. It mutates body in
                        # place and returns a log line, or None to propagate.
                        if attempt == 0:
                            fix = endpoint_profiles.recover_from_error(self.base_url, model, body, resp.status_code, err_text)
                            if fix is not None:
                                logger.warning("LLM recovery: %s", fix)
                                continue  # leave async-with cleanly, then retry
                        resp.raise_for_status()
                    async for payload in self._iter_sse_payloads(resp):
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        # Usage may appear in a terminal chunk (choices=[]) or on the final content chunk; last-write-wins since totals are monotonic.
                        u = chunk.get("usage")
                        if isinstance(u, dict):
                            usage = u

                        choices = chunk.get("choices") or []
                        if not choices:
                            # Pure usage/metadata chunk — nothing else to do.
                            continue

                        try:
                            choice = choices[0]
                            delta = choice.get("delta", {})

                            # Reasoning delta (field name varies by server)
                            rc = delta.get("reasoning_content") or delta.get("reasoning")
                            if rc:
                                reasoning_parts.append(rc)
                                yield {"type": "reasoning", "delta": rc}

                            # Content delta
                            c = delta.get("content")
                            if c:
                                content_parts.append(c)
                                yield {"type": "content", "delta": c}

                            # Per-token alternatives (Document mode steering) —
                            # present only when the caller passed logprobs and the
                            # provider honoured them; otherwise a no-op.
                            for rec in _parse_chat_logprobs(choice):
                                yield {"type": "token_probs", **rec}

                            # Tool call argument deltas — accumulate by index
                            for tc_delta in delta.get("tool_calls") or []:
                                idx = tc_delta.get("index", 0)
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                entry = tool_calls_acc[idx]
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
            # Streamed to completion (or aborted) without a retry-triggering
            # error -- done, no second attempt.
            break

        # Assemble the final message dict (mirrors the non-streaming message format)
        message: dict = {}
        content = "".join(content_parts)
        if content:
            message["content"] = content
        reasoning = "".join(reasoning_parts)
        if reasoning:
            message["reasoning_content"] = reasoning
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

        logger.info(
            "LLM complete: assembled keys=%s, has_tool_calls=%s, content_len=%s, usage=%s",
            list(message.keys()),
            "tool_calls" in message,
            len(message.get("content", "") or "") if message.get("content") else "null",
            usage,
        )
        yield {"type": "done", "message": message, "usage": usage}

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
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{server_root}/apply-template",
                json=body,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()["prompt"]

    async def _fetch_chat_template(self, server_root: str) -> str:
        """Fetch the server's ``chat_template`` text via ``GET /props`` (for tag sniff).

        Returns ``""`` on any failure so the caller falls back to a no-op reasoning
        toggle without caching the miss (see text_completion.get_think_tags).
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{server_root}/props", headers=self._headers())
                resp.raise_for_status()
                return resp.json().get("chat_template", "") or ""
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("text mode: /props fetch failed (%r); reasoning toggle disabled this call", e)
            return ""

    async def _stream_completion(self, url: str, body: dict) -> AsyncIterator[dict]:
        """POST *body* to llama.cpp ``/completion`` and yield each parsed SSE chunk.

        Races reads against abort via the shared :meth:`_iter_sse_payloads`.
        The single HTTP seam of the text transport (patched wholesale in tests).
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", url, json=body, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    try:
                        err_text = (await resp.aread()).decode("utf-8", errors="replace")
                    except Exception as read_err:
                        err_text = f"<failed to read response body: {read_err!r}>"
                    logger.error("LLM HTTP %d from %s: %s", resp.status_code, url, err_text)
                    resp.raise_for_status()
                async for payload in self._iter_sse_payloads(resp):
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue

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
        schema_override = params.pop("json_schema", None)
        render_msgs: list[Mapping[str, Any]] = list(messages)
        if prefill:
            # F9: a trailing assistant message renders as an open model turn ending
            # exactly at *prefill* (the follow-up editor feature's hook).
            render_msgs = [*render_msgs, {"role": "assistant", "content": prefill}]

        server_root = self._server_root()
        reasoning_on = text_completion.reasoning_enabled(params)
        # Let the chat template own reasoning on/off via ``enable_thinking`` rather
        # than hand-appending disable bytes: templates disagree on where the think
        # tag lives (Qwen3 pre-opens ``<think>`` in the generation prompt and closes
        # it for enable_thinking=false; Gemma 4 leaves the open tag to the model's
        # output). Hand-appending double-opened Qwen's tag and leaked its CoT as
        # content. Skip for prefill: the trailing assistant turn, not the generation
        # prompt, governs thinking there.
        ctk = None if prefill else {"enable_thinking": reasoning_on, "thinking": reasoning_on}
        try:
            prompt = await self._apply_template(server_root, render_msgs, ctk)
        except httpx.HTTPError as e:
            logger.warning("text mode: /apply-template failed (%r); falling back to chat transport", e)
            async for event in self._complete_chat(messages, model, tools, tool_choice, **params):
                yield event
            return

        tags = await text_completion.get_think_tags(server_root, lambda: self._fetch_chat_template(server_root))
        # Prime the splitter from what the template ACTUALLY rendered, not from the
        # requested reasoning flag.
        pre_opened = bool(tags[0]) and prompt.rstrip().endswith(tags[0].rstrip())

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

        splitter = text_completion.ThinkSplitter(tags, already_open=pre_opened)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        forced_buf: list[str] = []
        usage: dict | None = None
        async for data in self._stream_completion(f"{server_root}/completion", body):
            stop = bool(data.get("stop"))
            if stop:
                usage = text_completion.synthesize_usage(data)
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
            message = {}
            content = "".join(content_parts)
            if content:
                message["content"] = content
            reasoning = "".join(reasoning_parts)
            if reasoning:
                message["reasoning_content"] = reasoning

        logger.info(
            "LLM complete (text): assembled keys=%s, has_tool_calls=%s, content_len=%s, usage=%s",
            list(message.keys()),
            "tool_calls" in message,
            len(message.get("content", "") or "") if message.get("content") else "null",
            usage,
        )
        yield {"type": "done", "message": message, "usage": usage}

    async def complete_raw(self, prompt: str, model: str, **params) -> AsyncIterator[dict]:
        """Stream a raw text completion from a bare *prompt* string (no chat template).

        Text-transport only: POSTs *prompt* verbatim to llama.cpp's native
        ``/completion``. There is no ``/apply-template`` step and no
        ThinkSplitter — a raw continuation has no chat template, so no reasoning
        channel; provider bytes are streamed through as content. Preserves the
        ``complete()`` event contract (``content`` deltas then a ``done`` message
        with synthesized usage). ``cache_prompt: true`` gives KV reuse across
        successive continuations of the same document for free.

        *model* is accepted for signature symmetry with ``complete()`` (the
        native ``/completion`` endpoint serves whatever model the server loaded,
        so it is not sent in the body).
        """
        body = text_completion.build_completion_params(params)
        body["prompt"] = prompt
        body["stream"] = True

        logger.info("LLM complete_raw (text): prompt_len=%d, n_predict=%s", len(prompt), body.get("n_predict"))

        content_parts: list[str] = []
        usage: dict | None = None
        async for data in self._stream_completion(f"{self._server_root()}/completion", body):
            stop = bool(data.get("stop"))
            if stop:
                usage = text_completion.synthesize_usage(data)
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

        message = {"content": "".join(content_parts)}
        yield {"type": "done", "message": message, "usage": usage}


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
    """Build a normalised tool call dict, JSON-decoding ``arguments`` if it's a string."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
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
        start = content.find(start_char)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(content)):
            if content[i] == start_char:
                depth += 1
            elif content[i] == end_char:
                depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(content[start : i + 1])
                    if isinstance(parsed, dict) and "name" in parsed:
                        tool_calls.append(_make_tool_call(parsed["name"], parsed.get("arguments", {})))
                    elif isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict) and "name" in item:
                                tool_calls.append(_make_tool_call(item["name"], item.get("arguments", {})))
                    if tool_calls:
                        return tool_calls
                except json.JSONDecodeError:
                    pass
                break

    return tool_calls
