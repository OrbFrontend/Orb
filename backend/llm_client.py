from __future__ import annotations
import httpx
import json
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

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
    ) -> dict:
        """Non-streaming completion. Used for the agent pass."""
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "reasoning": {"effort": "low", "enabled": True},
            **params,
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self._url(), json=body, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

        # Log raw structure before accessing keys that may not exist
        logger.info("LLM complete: status=%d, keys=%s", resp.status_code, list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        if "choices" not in data:
            logger.error("LLM complete: response missing 'choices' key. Full keys: %s. Body (first 1000): %s",
                         list(data.keys()) if isinstance(data, dict) else "N/A", str(data)[:1000])
            raise KeyError(f"LLM response missing 'choices'. Keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        if not data["choices"]:
            logger.error("LLM complete: 'choices' is empty. Body (first 1000): %s", str(data)[:1000])
            raise ValueError("LLM response has empty 'choices' array")

        choice = data["choices"][0]
        logger.info("LLM complete: choice keys=%s, finish_reason=%s", list(choice.keys()), choice.get("finish_reason"))

        if "message" not in choice:
            logger.error("LLM complete: choice missing 'message' key. Choice keys: %s. Choice (first 1000): %s",
                         list(choice.keys()), str(choice)[:1000])
            raise KeyError(f"LLM choice missing 'message'. Keys: {list(choice.keys())}")

        message = choice["message"]
        logger.info("LLM complete: message keys=%s, has_tool_calls=%s, content_len=%s",
                     list(message.keys()), "tool_calls" in message,
                     len(message.get("content", "") or "") if message.get("content") else "null")
        return message

    async def stream(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        **params,
    ) -> AsyncIterator[str]:
        """Streaming completion. Yields content deltas."""
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "reasoning": {"enabled": False},
            **params,
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", self._url(), json=body, headers=self._headers()) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue


def _sanitize_args(obj):
    """Recursively strip tokenizer-artifact quote tokens (e.g. <|"|) from string values."""
    if isinstance(obj, str):
        return obj.replace("<|\"|", "").replace('<|"|', "")
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
                tool_calls.append({
                    "name": parsed["name"],
                    "arguments": _sanitize_args(parsed.get("arguments", {})),
                })
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
                        tool_calls.append({
                            "name": parsed["name"],
                            "arguments": _sanitize_args(parsed.get("arguments", {})),
                        })
                    # If it's an array of tool calls
                    elif isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict) and "name" in item:
                                tool_calls.append({
                                    "name": item["name"],
                                    "arguments": _sanitize_args(item.get("arguments", {})),
                                })
                    if tool_calls:
                        return tool_calls
                except json.JSONDecodeError:
                    pass
                break

    return tool_calls