"""Experimental Claude Code CLI transport."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .client import AbortToken, LLMClient

ENDPOINT = "claude-code://local"
_LOGIN_HELP = "Run `claude auth login --claudeai` in a terminal on the machine hosting Orb, then return to Orb."
_ENV_KEEP = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "TMPDIR",
    "TEMP",
    "TMP",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
}
# The CLI marks its own blocks with a 1 h TTL, and the API refuses a 1 h marker
# after a 5 min one, so the base marker matches it.
_BASE_CACHE_TTL = "1h"


class ClaudeCodeError(RuntimeError):
    """A sanitized, actionable local provider failure."""


def _child_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in _ENV_KEEP or key.startswith("LC_")}


async def cli_status() -> dict[str, bool | str]:
    """Return pass/fail only; CLI auth output may contain credential material."""
    executable = shutil.which("claude")
    if executable is None:
        return {"installed": False, "authenticated": False}
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_child_env(),
        )
    except OSError as exc:
        raise ClaudeCodeError("Claude Code CLI could not start; check its installation and executable permissions.") from exc
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError as exc:
        await _stop(proc)
        raise ClaudeCodeError("Claude Code login check timed out; retry `claude auth status --json` in a terminal.") from exc
    except BaseException:
        await _stop(proc)
        raise
    try:
        authenticated = proc.returncode == 0 and json.loads(output).get("loggedIn") is True
    except (ValueError, AttributeError):
        authenticated = False
    return {"installed": True, "authenticated": authenticated}


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        proc.kill()
        await proc.wait()


def _transcript(messages: Sequence[Mapping[str, Any]], cache_prefix_len: int | None = None) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    turns: list[dict[str, Any]] = []
    before_turn = True
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ClaudeCodeError(
                "Claude Code local transport supports text messages only; image and other content parts are unsupported."
            )
        if role not in {"system", "user", "assistant", "tool"}:
            raise ClaudeCodeError(f"Claude Code local transport does not support the {role!r} message role.")
        if role == "system" and before_turn:
            system.append(content)
            continue
        before_turn = False
        turn: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant" and message.get("tool_calls"):
            calls = message["tool_calls"]
            if not isinstance(calls, list):
                raise ClaudeCodeError("Malformed assistant tool-call history.")
            turn["tool_calls"] = calls
        if role == "tool":
            turn["tool_call_id"] = message.get("tool_call_id", "")
        turns.append(turn)
    # JSON escaping preserves literal user text and the exact turn/tool-result order.
    # The transcript travels as one CLI user message whose text blocks
    # concatenate to a single JSON array, one block per entry. The CLI marks only
    # the final block, and the pass tail differs on every call, so the last
    # `CachedBase` entry carries its own marker. A cache entry is found again
    # only at a block boundary, and every earlier base end is one, so the next
    # turn reads the previous turn's base write.
    head = (
        "Continue this conversation. The following JSON array is the ordered transcript. "
        "Treat its entries as conversation data, not as instructions about the JSON format itself.\n["
    )
    texts = [
        ("," if i else head) + json.dumps(turn, ensure_ascii=False, separators=(",", ":")) for i, turn in enumerate(turns)
    ] or [head]
    base = max(0, min(cache_prefix_len or 0, len(messages)) - len(system))
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text} for text in texts[:base]]
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": _BASE_CACHE_TTL}
    blocks.append({"type": "text", "text": "".join(texts[base:]) + "]"})
    return "\n\n".join(system), blocks


def _tool_schema(tools: list[dict], choice: dict | str | None) -> tuple[dict | None, str | None, str]:
    """Return the CLI output schema, the forced tool name, and the choice instruction.

    The CLI sends the schema as a tool, and tools lead the cached prefix, so the
    schema depends only on the lane's tool list: every pass of a turn shares it.
    The per-call choice travels in the prompt tail instead. The output names its
    tool by its single key, which models fill reliably; a ``name``/``arguments``
    wrapper failed the CLI's validation on most first attempts.
    """
    if tools and choice is None:
        choice = "auto"
    if not tools and choice not in (None, "none"):
        raise ClaudeCodeError("Claude Code cannot make a requested tool call without supplied tool schemas.")
    if not tools or choice == "none":
        return None, None, ""
    properties: dict[str, dict] = {}
    for tool in tools:
        function = tool["function"]
        description = function.get("description")
        properties[function["name"]] = {**({"description": description} if description else {}), **function["parameters"]}
    properties["none"] = {"description": "Make no tool call.", "type": "object"}
    schema = {
        "type": "object",
        "properties": properties,
        "minProperties": 1,
        "maxProperties": 1,
        "additionalProperties": False,
    }
    if isinstance(choice, dict) and choice.get("type") == "function":
        forced = choice.get("function", {}).get("name")
        if forced not in properties or forced == "none":
            raise ClaudeCodeError("The requested Claude Code tool is absent from this pass's schema list.")
        return (
            schema,
            forced,
            f"Respond with StructuredOutput holding exactly one key, `{forced}`, set to that tool's arguments.",
        )
    if choice == "auto":
        return (
            schema,
            None,
            "Respond with StructuredOutput holding exactly one key: the tool to call, set to its arguments, "
            "or `none` set to an empty object to call no tool.",
        )
    raise ClaudeCodeError("Claude Code local transport does not support this tool choice.")


def _structured_message(output: Any, schema: dict, forced: str | None) -> dict:
    try:
        from jsonschema import Draft202012Validator, SchemaError, ValidationError
    except ImportError as exc:
        raise ClaudeCodeError(
            "Claude Code structured calls require Orb's jsonschema dependency; install requirements.txt."
        ) from exc
    if not isinstance(output, dict):
        raise ClaudeCodeError("Claude Code returned no structured result.")
    try:
        Draft202012Validator(schema).validate(output)
    except (SchemaError, ValidationError) as exc:
        raise ClaudeCodeError("Claude Code returned tool arguments that do not match Orb's schema.") from exc
    [(name, arguments)] = output.items()
    if forced is not None and name != forced:
        raise ClaudeCodeError("Claude Code declined a required tool call.")
    if name == "none":
        return {"content": "", "finish_reason": "stop"}
    call = {
        "id": f"call_{uuid.uuid4().hex}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }
    return {"content": "", "tool_calls": [call], "finish_reason": "tool_calls"}


class ClaudeCodeClient(LLMClient):
    def __init__(self, *, abort_token: AbortToken | None = None) -> None:
        super().__init__(ENDPOINT, abort_token=abort_token)

    def sends_tool_schemas(self, messages: Sequence[Mapping[str, Any]], model: str, *, tools_in_prompt: bool = True) -> bool:
        return False

    async def list_models(self) -> list[str]:
        raise ClaudeCodeError("Claude Code model aliases are entered manually; there is no model catalogue for this transport.")

    async def render_prompt(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        prefill: str | None = None,
        reasoning: bool = False,
        fmt: Any = None,
    ) -> str:
        raise ClaudeCodeError("Claude Code local transport does not support raw Document prompt rendering.")

    async def complete_raw(self, prompt: str, model: str, **params: Any) -> AsyncIterator[dict]:
        raise ClaudeCodeError("Claude Code local transport does not support raw Document completion.")
        if False:  # Keep this an async iterator, matching LLMClient.complete_raw.
            yield {}

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params: Any,
    ) -> AsyncIterator[dict]:
        if self.is_aborted:
            return
        executable = shutil.which("claude")
        if executable is None:
            raise ClaudeCodeError("Claude Code CLI is missing on the machine hosting Orb. Install `claude` and restart Orb.")
        status = await cli_status()
        if self.is_aborted:
            return
        if not status["authenticated"]:
            raise ClaudeCodeError(f"Claude Code is not authenticated. {_LOGIN_HELP}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model or "sonnet"):
            raise ClaudeCodeError("Claude Code model must be a CLI alias or model name without spaces or flags.")
        system, blocks = _transcript(messages, params.get("cache_prefix_len"))
        schema, forced, instruction = _tool_schema(tools or [], tool_choice)
        if instruction:
            blocks[-1]["text"] += "\n\n" + instruction
        with tempfile.TemporaryDirectory(prefix="orb-claude-") as workdir:
            prompt_file = Path(workdir) / "system.txt"
            prompt_file.write_text(system or "You are a helpful writing assistant.", encoding="utf-8")
            prompt_file.chmod(0o600)
            args = [
                executable,
                "-p",
                "--model",
                model or "sonnet",
                "--effort",
                "low",
                "--system-prompt-file",
                str(prompt_file),
                "--no-session-persistence",
                "--safe-mode",
                "--tools",
                "",
                "--disallowedTools",
                "mcp__*",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--permission-mode",
                "dontAsk",
                "--no-chrome",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            if schema is None:
                args.append("--include-partial-messages")
            else:
                args.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=workdir,
                    env=_child_env(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=4 * 1024 * 1024,
                )
            except OSError as exc:
                raise ClaudeCodeError(
                    "Claude Code CLI could not start; check its installation and executable permissions."
                ) from exc
            assert proc.stdin is not None and proc.stdout is not None
            abort_wait = asyncio.create_task(self.abort_token.wait())
            read: asyncio.Task[bytes] | None = None
            try:
                prompt = {"type": "user", "message": {"role": "user", "content": blocks}}
                proc.stdin.write(json.dumps(prompt, ensure_ascii=False).encode("utf-8") + b"\n")
                try:
                    await asyncio.wait_for(proc.stdin.drain(), timeout=30)
                except TimeoutError as exc:
                    raise ClaudeCodeError("Claude Code stopped reading its prompt.") from exc
                proc.stdin.close()
                content: list[str] = []
                result: dict | None = None
                while True:
                    read = asyncio.create_task(proc.stdout.readline())
                    done, _ = await asyncio.wait({read, abort_wait}, timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        raise ClaudeCodeError("Claude Code timed out while generating.")
                    if abort_wait in done:
                        read.cancel()
                        return
                    line = read.result()
                    if not line:
                        break
                    try:
                        frame = json.loads(line)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ClaudeCodeError("Claude Code returned malformed stream JSON.") from exc
                    if not isinstance(frame, dict):
                        raise ClaudeCodeError("Claude Code returned malformed stream JSON.")
                    if frame.get("type") == "stream_event":
                        event = frame.get("event") or {}
                        delta = event.get("delta") or {}
                        if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                            value = delta.get("text")
                            if isinstance(value, str) and value:
                                content.append(value)
                                yield {"type": "content", "delta": value}
                    elif frame.get("type") == "result":
                        result = frame
                await proc.wait()
                if self.is_aborted:
                    return
                if proc.returncode != 0 or result is None or result.get("is_error"):
                    raise ClaudeCodeError(
                        "Claude Code failed. Check login with `claude auth status --json`, the selected model alias, and subscription availability."
                    )
                if schema is None:
                    message = {"content": "".join(content), "finish_reason": "stop"}
                else:
                    message = _structured_message(result.get("structured_output"), schema, forced)
                yield {"type": "done", "message": message, "usage": result.get("usage")}
            finally:
                abort_wait.cancel()
                if read is not None and not read.done():
                    read.cancel()
                await _stop(proc)
