from __future__ import annotations
import httpx
import json
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)


def reasoning_cfg(on: bool) -> dict:
    """Complete reasoning params for a client.complete() call, spread with **.

    Covers all API standards in one place:
      - reasoning.effort / reasoning.enabled  — OpenAI-style servers
      - chat_template_kwargs.enable_thinking  — llama.cpp servers
    """
    return {
        "reasoning": {"effort": "low",  "enabled": True},
        "chat_template_kwargs": {"enable_thinking": True},
    } if on else {
        "reasoning": {"effort": "none", "enabled": False},
        "chat_template_kwargs": {"enable_thinking": False},
    }


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
        logit_bias: dict | None = None,
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
        if logit_bias:
            body["logit_bias"] = logit_bias

        logger.info("LLM complete: model=%s, tools=%s, tool_choice=%s",
                     model,
                     json.dumps([t["function"]["name"] for t in tools]) if tools else "None",
                     tool_choice)
        logger.info(messages)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason: str | None = None

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
                        choice = chunk["choices"][0]
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
                        for tc_delta in (delta.get("tool_calls") or []):
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "", "type": "function",
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
                    "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]},
                }
                for v in (tool_calls_acc[k] for k in sorted(tool_calls_acc))
            ]
        if finish_reason:
            message["finish_reason"] = finish_reason

        logger.info("LLM complete: assembled keys=%s, has_tool_calls=%s, content_len=%s",
                     list(message.keys()), "tool_calls" in message,
                     len(message.get("content", "") or "") if message.get("content") else "null")
        yield {"type": "done", "message": message}


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

        Two-stage strategy:
        1. Non-streaming probe (reasoning disabled) — scan all logprob tokens via
           exact match against KNOWN_TOOL_STARTS and a structural heuristic.
           If logprobs are empty but a tool call still happened, brute-force
           the known list via /tokenize.
        2. Streaming probe fallback — some backends surface raw control tokens as
           content deltas in streaming mode even when non-streaming collapses them
           into the tool_calls object.

        Returns the validated token ID, or None if discovery fails or the model
        doesn't use a dedicated control token.
        """
        import re

        # Known tool-call start token strings across common model families.
        KNOWN_TOOL_STARTS = {
            "<|tool_call>",       # Gemma 4  (stc_token)
            "<|python_tag|>",     # Llama / Code-Llama
            "[TOOL_CALL]",        # Mistral
            "<tool_call>",        # Various open models
            "<function_calls>",   # Some fine-tunes
            "<|tool_calls|>",
            "<|function_calls|>",
        }

        # Matches well-formed control tokens only:
        #   <|...|>  <|...>  [...] (all-caps/underscore word inside brackets)
        # Rejects generic subwords, whitespace sequences, code fences, etc.
        _CONTROL_RE = re.compile(r'^(<\|.+\|?>|\[[A-Z_]+\])$')

        dummy_tool = {
            "type": "function",
            "function": {
                "name": "dummy",
                "description": "Dummy tool for probing.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        probe_messages = [{"role": "user", "content": "Call the dummy tool now."}]
        probe_base = {
            "model": model,
            "messages": probe_messages,
            "tools": [dummy_tool],
            "tool_choice": "required",
            "max_tokens": 150,
            "logprobs": True,
            "top_logprobs": 1,
            # Disable reasoning so thinking tokens don't consume the budget before the model reaches the tool-call control token.
            **reasoning_cfg(False),
        }

        def _entry_id(entry: dict) -> int | None:
            """Extract integer token ID from a logprob entry (field name varies by server)."""
            raw = entry.get("id") or entry.get("token_id") or (entry.get("token_ids") or [None])[0]
            return int(raw) if raw is not None else None

        async def _resolve(token_str: str, lp_id: int | None) -> int | None:
            """Validate a token string/ID pair; return a confirmed integer ID or None.

            Prefers /tokenize as the authoritative source (item 7). Warns on
            mismatches and on suspiciously low IDs (item 8) but does not discard —
            Gemma 4's <|tool_call> is ID 48, well below common thresholds.
            """
            tok_id = await self._tokenize_string(model, token_str)
            if tok_id is not None:
                if lp_id is not None and tok_id != lp_id:
                    logger.warning(
                        "discover_tool_start_token: ID mismatch for '%s': logprobs=%d /tokenize=%d — using /tokenize",
                        token_str, lp_id, tok_id,
                    )
                if tok_id < 100:
                    logger.warning(
                        "discover_tool_start_token: '%s' has low ID %d — confirm it's not a structural token",
                        token_str, tok_id,
                    )
                return tok_id
            # /tokenize unavailable; trust the logprobs ID
            if lp_id is not None:
                if lp_id < 100:
                    logger.warning(
                        "discover_tool_start_token: '%s' has low ID %d — confirm it's not a structural token",
                        token_str, lp_id,
                    )
                return lp_id
            return None

        def _scan_entries(entries: list[dict]) -> tuple[str, int | None] | None:
            """Run Pass 1 (exact match + top_logprobs) and Pass 2 (structural heuristic)
            over a list of logprob entries. Returns (token_str, lp_id) on first hit."""
            # Pass 1: exact match in chosen token AND top_logprobs alternatives (item 5)
            for entry in entries:
                for candidate in [entry] + (entry.get("top_logprobs") or []):
                    ts = candidate.get("token")
                    if ts in KNOWN_TOOL_STARTS:
                        return ts, _entry_id(candidate)

            # Pass 2: structural heuristic — control-token regex immediately followed by "call" or "{" in the next chosen token (item 4: tightened is_special)
            for i, entry in enumerate(entries[:-1]):
                ts = entry.get("token", "")
                next_ts = entries[i + 1].get("token", "")
                if _CONTROL_RE.match(ts) and next_ts.lstrip().startswith(("call", "{")):
                    return ts, _entry_id(entry)

            return None

        # ── Non-streaming probe ─────────────────────────────────────────────────
        logger.info("discover_tool_start_token: non-streaming probe model=%s", model)
        ns_data: dict | None = None
        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                r = await http.post(self._url(), json={**probe_base, "stream": False}, headers=self._headers())
                if r.status_code == 200:
                    ns_data = r.json()
                else:
                    logger.warning("discover_tool_start_token: non-streaming probe HTTP %d", r.status_code)
        except Exception as e:
            logger.warning("discover_tool_start_token: non-streaming probe error: %s", e)

        if ns_data:
            choice = (ns_data.get("choices") or [{}])[0]
            lp_entries: list[dict] = (choice.get("logprobs") or {}).get("content") or []
            has_tool_calls = bool((choice.get("message") or {}).get("tool_calls"))

            hit = _scan_entries(lp_entries)
            if hit:
                ts, lp_id = hit
                result = await _resolve(ts, lp_id)
                if result is not None:
                    logger.info("discover_tool_start_token: non-streaming scan '%s' -> %d", ts, result)
                    return result

            # Item 6: logprobs missed the token but the response has a tool_calls object
            # → brute-force tokenize each known string to find which one this model uses.
            if has_tool_calls and not lp_entries:
                logger.info("discover_tool_start_token: tool_calls present but logprobs empty — brute-forcing known list")
                for known_str in KNOWN_TOOL_STARTS:
                    tid = await self._tokenize_string(model, known_str)
                    if tid is not None:
                        logger.info("discover_tool_start_token: brute-force '%s' -> %d", known_str, tid)
                        return tid

        # ── Streaming probe fallback (item 2) ───────────────────────────────────
        # Some backends emit raw control tokens as content deltas in streaming mode even when non-streaming collapses them into the tool_calls structure.
        logger.info("discover_tool_start_token: falling back to streaming probe model=%s", model)
        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                async with http.stream(
                    "POST", self._url(),
                    json={**probe_base, "stream": True},
                    headers=self._headers(),
                ) as r:
                    r.raise_for_status()
                    stream_entries: list[dict] = []
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                            delta = chunk["choices"][0].get("delta", {})
                            content = delta.get("content") or ""
                            chunk_entries: list[dict] = (chunk["choices"][0].get("logprobs") or {}).get("content") or []

                            # Quick check: raw content delta matches a known start token
                            if content in KNOWN_TOOL_STARTS:
                                lp_id = _entry_id(chunk_entries[0]) if chunk_entries else None
                                result = await _resolve(content, lp_id)
                                if result is not None:
                                    logger.info(
                                        "discover_tool_start_token: streaming delta '%s' -> %d",
                                        content, result,
                                    )
                                    return result

                            stream_entries.extend(chunk_entries)
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue

                    # After streaming completes, run the same two-pass scan over all accumulated logprob entries from the stream.
                    hit = _scan_entries(stream_entries)
                    if hit:
                        ts, lp_id = hit
                        result = await _resolve(ts, lp_id)
                        if result is not None:
                            logger.info(
                                "discover_tool_start_token: streaming scan '%s' -> %d",
                                ts, result,
                            )
                            return result
        except Exception as e:
            logger.warning("discover_tool_start_token: streaming probe error: %s", e)

        logger.info("discover_tool_start_token: discovery failed, returning None")
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