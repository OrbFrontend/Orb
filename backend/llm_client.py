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

        logger.info("LLM complete: model=%s, tools=%s, tool_choice=%s",
                     model,
                     json.dumps([t["function"]["name"] for t in tools]) if tools else "None",
                     tool_choice)

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
        logit_bias: dict | None = None,
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
        if logit_bias:
            body["logit_bias"] = logit_bias

        logger.info("LLM stream: model=%s, tools=%s, tool_choice=%s",
                     model,
                     json.dumps([t["function"]["name"] for t in tools]) if tools else "None",
                     tool_choice)

        logger.info(messages)

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


    async def _tokenize_string(self, model: str, text: str) -> int | None:
        """Call the /tokenize endpoint to resolve a token string to its integer ID.

        Tries both /{api_prefix}/tokenize and the server-root /tokenize (used by
        llama.cpp and similar servers). Sends both 'content' and 'prompt' keys to
        handle differing server conventions.

        Returns the single token ID if the text maps to exactly one token, or
        None if the endpoint is unavailable or the text spans multiple tokens.
        """
        import urllib.parse
        parsed = urllib.parse.urlparse(self.base_url)
        server_root = f"{parsed.scheme}://{parsed.netloc}"

        candidates = []
        if server_root != self.base_url.rstrip("/"):
            candidates.append(f"{server_root}/tokenize")
        candidates.append(f"{self.base_url}/tokenize")

        for url in candidates:
            body = {"model": model, "content": text, "prompt": text, "add_special_tokens": False}
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(url, json=body, headers=self._headers())
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    tokens = data.get("tokens") or data.get("token_ids") or []
                    if len(tokens) == 1:
                        logger.debug("_tokenize_string: '%s' -> %d via %s", text, tokens[0], url)
                        return int(tokens[0])
                    if len(tokens) > 1:
                        logger.debug("_tokenize_string: '%s' spans %d tokens at %s", text, len(tokens), url)
                        return None
            except Exception as e:
                logger.debug("_tokenize_string: %s failed: %s", url, e)
        return None

    async def discover_tool_start_token(self, model: str) -> int | None:
        """Probe the API to discover the integer token ID that starts a tool call.

        Strategy:
        1. Force a tool call with a dummy tool, generating enough tokens to get
           past any thinking preamble and into the actual tool call.
        2. Scan all generated logprob tokens for a known tool-call start pattern
           OR for a special token immediately followed by "call" / "{".
        3. Resolve to an integer ID from the logprobs "id" field or via /tokenize.

        Returns the token ID, or None if discovery fails or the model doesn't use
        a dedicated control token (e.g. it emits raw JSON without a special marker).
        """
        # Known tool-call start token strings across common model families.
        # Checked by exact match against the logprobs token strings.
        KNOWN_TOOL_STARTS = {
            "<|tool_call>",       # Gemma 4 (stc_token)
            "<|python_tag|>",     # Llama / Code-Llama family
            "[TOOL_CALL]",        # Mistral
            "<tool_call>",        # Various open models
            "<function_calls>",   # Some fine-tunes
            "<|tool_calls|>",
            "<|function_calls|>",
        }

        dummy_tool = {
            "type": "function",
            "function": {
                "name": "dummy",
                "description": "Dummy tool for probing.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        # Use enough tokens to get through a thinking preamble before the tool call.
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Call the dummy tool now."}],
            "tools": [dummy_tool],
            "tool_choice": "required",
            "max_tokens": 150,
            "stream": False,
            "logprobs": True,
            "top_logprobs": 1,
        }

        logger.info("discover_tool_start_token: probing model=%s", model)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(self._url(), json=body, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning("discover_tool_start_token: probe returned HTTP %d", resp.status_code)
                    return None
                data = resp.json()
        except Exception as e:
            logger.warning("discover_tool_start_token: probe request failed: %s", e)
            return None

        entries: list[dict] = []
        try:
            lp = data["choices"][0].get("logprobs") or {}
            entries = lp.get("content") or []
        except (KeyError, IndexError, TypeError):
            pass

        if not entries:
            logger.info("discover_tool_start_token: no logprobs content in response")
            return None

        def _entry_id(entry: dict) -> int | None:
            # llama.cpp uses "id"; others use "token_id" or "token_ids"
            raw = entry.get("id") or entry.get("token_id") or (entry.get("token_ids") or [None])[0]
            return int(raw) if raw is not None else None

        # Pass 1: exact match against known tool-call start tokens
        for entry in entries:
            if entry.get("token") in KNOWN_TOOL_STARTS:
                tid = _entry_id(entry)
                if tid is not None:
                    logger.info(
                        "discover_tool_start_token: matched known pattern '%s' -> %d",
                        entry["token"], tid,
                    )
                    return tid
                # ID missing from logprobs; try /tokenize
                tid = await self._tokenize_string(model, entry["token"])
                if tid is not None:
                    logger.info(
                        "discover_tool_start_token: matched known pattern '%s' -> %d (via /tokenize)",
                        entry["token"], tid,
                    )
                    return tid

        # Pass 2: structural heuristic — special token immediately followed by
        # "call" or "{" (covers formats we don't have in the known list yet)
        import string as _string
        for i, entry in enumerate(entries[:-1]):
            token_str = entry.get("token", "")
            next_str = entries[i + 1].get("token", "")
            is_special = token_str and not (len(token_str) == 1 and (token_str.isalnum() or token_str in _string.punctuation))
            follows_call = next_str.lstrip().startswith(("call", "{"))
            if is_special and follows_call:
                tid = _entry_id(entry)
                if tid is None:
                    tid = await self._tokenize_string(model, token_str)
                if tid is not None:
                    logger.info(
                        "discover_tool_start_token: heuristic match '%s' -> %d",
                        token_str, tid,
                    )
                    return tid

        # Pass 3: fallback — the very first non-generic special token, for
        # models that don't think before calling tools
        for entry in entries:
            token_str = entry.get("token", "")
            if not token_str:
                continue
            if len(token_str) == 1 and (token_str.isalnum() or token_str in _string.punctuation):
                continue
            tid = _entry_id(entry) or await self._tokenize_string(model, token_str)
            if tid is not None:
                logger.info(
                    "discover_tool_start_token: fallback first special token '%s' -> %d",
                    token_str, tid,
                )
                return tid

        logger.info("discover_tool_start_token: no usable tool-call token found in %d logprob entries", len(entries))
        return None


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