from __future__ import annotations
import asyncio
import httpx
import json
import logging
from typing import AsyncIterator

from .endpoint_profiles import ModelProfile

logger = logging.getLogger(__name__)


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
        }
        if on
        else {
            "reasoning": {"effort": "none", "enabled": False},
            "chat_template_kwargs": {"enable_thinking": False},
        }
    )


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 120.0,
        profile: ModelProfile | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.profile = profile
        self._abort: asyncio.Event = asyncio.Event()

    def abort(self) -> None:
        """Signal all ongoing complete() calls to stop and close their connections."""
        logger.info("Stop Generation button clicked — abort signal sent to LLM client")
        self._abort.set()

    def _headers(self) -> dict:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    async def complete(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        """Streaming completion. Yields reasoning deltas then the assembled message.

        Yields:
            {"type": "reasoning", "delta": str}  — zero or more reasoning chunks
            {"type": "done", "message": dict}    — assembled message with content/tool_calls
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

        if self.profile is not None:
            for action in self.profile.apply(body):
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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", self._url(), json=body, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    # Streaming response: body isn't eagerly read, so
                    # raise_for_status() would surface only the status line.
                    # Read the body so upstream error detail lands in logs.
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
                    resp.raise_for_status()
                # Race each line read against the abort signal so that client.abort()
                # breaks out of this loop immediately, letting the async-with block
                # exit *normally* and cleanly close the TCP connection to the LLM
                # server. (Using asyncio task cancellation instead would leave the
                # connection open under Python 3.11+ strict cancellation semantics.)
                aiter = resp.aiter_lines().__aiter__()
                abort_wait = asyncio.create_task(self._abort.wait())
                try:
                    while True:
                        line_task = asyncio.create_task(aiter.__anext__())
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
                            choice = chunk["choices"][0]
                            delta = choice.get("delta", {})

                            # Reasoning delta (field name varies by server)
                            rc = delta.get("reasoning_content") or delta.get(
                                "reasoning"
                            )
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

                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                finally:
                    abort_wait.cancel()
                    try:
                        await abort_wait
                    except asyncio.CancelledError:
                        pass

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
            "LLM complete: assembled keys=%s, has_tool_calls=%s, content_len=%s",
            list(message.keys()),
            "tool_calls" in message,
            len(message.get("content", "") or "") if message.get("content") else "null",
        )
        yield {"type": "done", "message": message}


def _sanitize_args(obj):
    """Recursively strip tokenizer-artifact quote tokens (e.g. <|"|) from string values."""
    if isinstance(obj, str):
        return obj.replace('<|"|', "").replace('<|"|', "")
    if isinstance(obj, list):
        return [_sanitize_args(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_args(v) for k, v in obj.items()}
    return obj


def parse_tool_calls(message: dict) -> list[dict]:
    """Extract tool calls from a completion message.

    Handles both the standard `tool_calls` array and a fallback where the
    model outputs JSON in the content body (common with some local servers).
    Also handles Gemma-style <tool_call>...</tool_call> tags.
    """
    import re

    tool_calls = []

    # Standard OpenAI tool_calls format
    if "tool_calls" in message and message["tool_calls"]:
        for tc in message["tool_calls"]:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"name": name, "arguments": _sanitize_args(args)})
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
                tool_calls.append(
                    {
                        "name": parsed["name"],
                        "arguments": _sanitize_args(parsed.get("arguments", {})),
                    }
                )
        except json.JSONDecodeError:
            pass
    if tool_calls:
        return tool_calls

    # Try to find JSON objects or arrays in the content
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = content.find(start_char)
        if start == -1:
            continue
        # Find matching close
        depth = 0
        for i in range(start, len(content)):
            if content[i] == start_char:
                depth += 1
            elif content[i] == end_char:
                depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(content[start : i + 1])
                    # If it's a single object with 'name' and 'arguments', treat as one tool call
                    if isinstance(parsed, dict) and "name" in parsed:
                        tool_calls.append(
                            {
                                "name": parsed["name"],
                                "arguments": _sanitize_args(
                                    parsed.get("arguments", {})
                                ),
                            }
                        )
                    # If it's an array of tool calls
                    elif isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict) and "name" in item:
                                tool_calls.append(
                                    {
                                        "name": item["name"],
                                        "arguments": _sanitize_args(
                                            item.get("arguments", {})
                                        ),
                                    }
                                )
                    if tool_calls:
                        return tool_calls
                except json.JSONDecodeError:
                    pass
                break

    return tool_calls
