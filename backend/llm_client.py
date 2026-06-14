from __future__ import annotations
import asyncio
import httpx
import json
import logging
import re
from typing import Any, AsyncIterator, Mapping, Sequence

from .endpoint_profiles import ModelProfile

logger = logging.getLogger(__name__)

# (base_url, model) pairs seen to reject the tool_choice param this session.
# In-memory only (cleared on restart); lets later calls drop it up front.
_TOOL_CHOICE_UNSUPPORTED: set[tuple[str, str]] = set()


def _is_tool_choice_unsupported(status: int, text: str) -> bool:
    """True for OpenRouter's tool_choice rejection: a 404 reading
    "No endpoints found that support the provided 'tool_choice' value."
    The provider routed for this model honors no tool_choice value at all
    (not just forced ones), so the recovery is to drop the param entirely.
    Narrow on purpose so genuine 404s (bad model id, etc.) don't match.
    """
    if status != 404:
        return False
    low = text.lower()
    return "tool_choice" in low and "no endpoints found" in low


class AbortToken:
    """A single stop signal shared by every LLMClient in one turn.

    A turn may spin up several physical clients (writer, separate agent, and
    their macro-resolving wrappers). They all hold a reference to the same
    token, so ``abort()`` is signalled once and ``is_aborted`` reads the same
    state everywhere — no per-client fan-out, no list bookkeeping.
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
    """Complete reasoning params for a client.complete() call, spread with **.

    Covers all API standards in one place:
      - reasoning.effort / reasoning.enabled  — OpenAI-style servers
      - chat_template_kwargs.enable_thinking  — llama.cpp servers
    """
    return (
        {
            "reasoning": {"effort": "low", "enabled": True},
            "chat_template_kwargs": {"enable_thinking": True},
            "thinking": {"type": "enabled"},
        }
        if on
        else {
            "reasoning": {"effort": "none", "enabled": False},
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking": {"type": "disabled"},
        }
    )


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 120.0,
        profile: ModelProfile | None = None,
        abort_token: AbortToken | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.profile = profile
        # Shared across the turn's clients when passed in; otherwise a private
        # token so a standalone client (e.g. a workflow hook) is still abortable.
        self.abort_token = abort_token or AbortToken()

    def abort(self) -> None:
        """Signal all ongoing complete() calls to stop and close their connections."""
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

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        """Streaming completion. Yields reasoning deltas then the assembled message.

        Yields:
            {"type": "reasoning", "delta": str}  — zero or more reasoning chunks
            {"type": "content",   "delta": str}  — zero or more content chunks
            {"type": "done", "message": dict, "usage": dict | None}
                — assembled message with content/tool_calls, plus the provider's
                  usage object if returned (None when the server doesn't emit it).
        """
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

        if self.profile is not None:
            for action in self.profile.apply(body):
                logger.info("LLM profile: %s", action)

        # Skip the round-trip if this pair already rejected tool_choice this session.
        is_openrouter = "openrouter.ai" in self.base_url.lower()
        if is_openrouter and "tool_choice" in body and (self.base_url, model) in _TOOL_CHOICE_UNSUPPORTED:
            logger.info(
                "LLM tool_choice: dropping unsupported tool_choice for known model %s",
                model,
            )
            body.pop("tool_choice")

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

        # At most one retry, solely to self-heal an OpenRouter model that rejects
        # the tool_choice param. The 404 lands before any SSE event, so the retry
        # is clean.
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

                        # Concern 2: self-heal the one quirk we recognise. Tightly
                        # gated so any other 404 still raises below.
                        if (
                            attempt == 0
                            and is_openrouter
                            and "tool_choice" in body
                            and _is_tool_choice_unsupported(resp.status_code, err_text)
                        ):
                            _TOOL_CHOICE_UNSUPPORTED.add((self.base_url, model))
                            logger.warning(
                                "Model %s rejected tool_choice=%r; retrying without it. "
                                "Add it to endpoint_profiles.PROFILES['openrouter.ai'] "
                                "for a zero-retry fix.",
                                model,
                                body.get("tool_choice"),
                            )
                            body.pop("tool_choice")
                            continue  # leave async-with cleanly, then retry
                        resp.raise_for_status()
                    # Race each line read against the abort signal so that client.abort()
                    # breaks out of this loop immediately, letting the async-with block
                    # exit *normally* and cleanly close the TCP connection to the LLM
                    # server. (Using asyncio task cancellation instead would leave the
                    # connection open under Python 3.11+ strict cancellation semantics.)
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
                                break  # exit loop → async-with closes connection cleanly

                            try:
                                line = line_task.result()
                            except StopAsyncIteration:
                                break

                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if payload == "[DONE]":
                                break
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
                    finally:
                        abort_wait.cancel()
                        try:
                            await abort_wait
                        except asyncio.CancelledError:
                            pass
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


def _sanitize_args(obj):
    """Recursively strip tokenizer-artifact quote tokens (e.g. <|"|) from string values."""
    if isinstance(obj, str):
        return obj.replace('<|"|', "").replace('<|"|', "")
    if isinstance(obj, list):
        return [_sanitize_args(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_args(v) for k, v in obj.items()}
    return obj


def _make_tool_call(name: str, arguments) -> dict:
    """Build a normalised tool-call dict, parsing arguments if they arrive as a JSON string."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    return {"name": name, "arguments": _sanitize_args(arguments)}


def parse_tool_calls(message: dict) -> list[dict]:
    """Extract tool calls from a completion message.

    Handles both the standard `tool_calls` array and a fallback where the
    model outputs JSON in the content body (common with some local servers).
    Also handles Gemma-style <tool_call>...</tool_call> tags.
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

    # Gemma-style <tool_call>...</tool_call> tags
    for match in re.finditer(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL):
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict) and "name" in parsed:
                tool_calls.append(_make_tool_call(parsed["name"], parsed.get("arguments", {})))
        except json.JSONDecodeError:
            pass
    if tool_calls:
        return tool_calls

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
