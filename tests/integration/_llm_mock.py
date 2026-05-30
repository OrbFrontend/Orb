"""LLM client substitute for integration tests of the streaming pipeline.

The real ``backend.llm_client.LLMClient`` reaches the OpenAI-compatible
endpoint over httpx. Concurrency tests do not need that round-trip; they
need a deterministic, in-process stand-in whose timing the test
controls. ``FakeLLMClient`` provides that: its ``complete()`` is an
async generator that yields canned reasoning / done events, optionally
waiting on a per-pass ``asyncio.Event`` so a test can hold a stream
mid-pipeline while another concurrent action arrives.

Pass dispatch is by ``tool_choice`` rather than a call counter, because
director may be skipped entirely (gated behind ``has_pre_writer_tools``
in the orchestrator) and editor iterates multiple times per turn -- a
positional scheme would mis-bind queued responses.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator


_EDITOR_FUNCTION_NAMES = {"editor_apply_patch", "editor_rewrite"}
_DIRECTOR_FUNCTION_NAMES = {"direct_scene", "rewrite_user_prompt"}


class PassGate:
    """Pair of events the test uses to pause a single ``complete()`` call.

    ``reached`` is set by the mock right before awaiting ``release``, so
    a test can ``await gate.reached`` to know the call has actually
    arrived at the gate. ``release`` is set by the test to let the call
    proceed.
    """

    __slots__ = ("reached", "release")

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()


def _pass_from_tool_choice(tool_choice: Any) -> str:
    # Writer omits tool_choice when no tools are enabled (passes None at the
    # kwarg default) and passes the literal "none" when tools are enabled
    # but the writer must not invoke any of them.
    if tool_choice is None or tool_choice == "none":
        return "writer"
    # Editor passes "auto" when audit is disabled and no length guard fired,
    # otherwise forces editor_apply_patch or editor_rewrite.
    if tool_choice == "auto":
        return "editor"
    if isinstance(tool_choice, dict):
        name = tool_choice.get("function", {}).get("name")
        if name in _EDITOR_FUNCTION_NAMES:
            return "editor"
        if name in _DIRECTOR_FUNCTION_NAMES:
            return "director"
        # Any other forced function name belongs to a workflow tool: the
        # toolkit's forced_tool_call helper passes the same dict shape via
        # TOOLS[<wid_registered_name>]["choice"], but the name is not one of
        # the four core pass tools.
        if name:
            return "workflow"
    return "director"


class FakeLLMClient:
    """A deterministic stand-in for ``LLMClient`` shared by every
    constructor call within one test.

    Tests interact via the public mutator methods (``enqueue_*``,
    ``gate``) before kicking off the action under test, then drive the
    rest of the test through the HTTP client; the mock yields the
    enqueued event for each matching pass invocation.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[dict]] = {"director": [], "writer": [], "editor": [], "workflow": []}
        self._gates: dict[str, list[PassGate]] = {"director": [], "writer": [], "editor": [], "workflow": []}
        self._abort = asyncio.Event()
        # Public assertion surface: tests inspect ``calls`` directly for
        # dispatch order and invocation counts, so its shape is part of
        # the mock's contract -- do not rename or restructure.
        self.calls: list[tuple[str, Any]] = []

    def enqueue_director(self, tool_calls: list[dict]) -> None:
        """Queue a director response.

        The director pass calls ``parse_tool_calls`` on the result, so
        *tool_calls* must follow the OpenAI ``message.tool_calls`` shape
        (``{"id", "type": "function", "function": {"name", "arguments"}}``);
        a mismatch yields silent empty parsing rather than a test failure.
        """
        self._queues["director"].append({"tool_calls": tool_calls})

    def enqueue_writer(self, text: str) -> None:
        self._queues["writer"].append({"content": text})

    def enqueue_editor(self, decision: dict | None = None) -> None:
        """Queue an editor response. ``decision`` is the ``message`` dict
        the editor pass receives. ``None`` (the default) yields an empty
        message with no tool calls, which causes the editor loop to stop.
        """
        self._queues["editor"].append({"message": decision or {"tool_calls": []}})

    def enqueue_workflow(self, message: dict) -> None:
        self._queues["workflow"].append({"message": message})

    def gate(self, pass_name: str) -> PassGate:
        """Return a ``PassGate`` controlling the next *pass_name* call.

        Gates are FIFO and one-shot: each ``gate(pass_name)`` applies
        to exactly one ``complete()`` call for that pass, in registration
        order, and once consumed subsequent calls run ungated.
        """
        gate = PassGate()
        self._gates[pass_name].append(gate)
        return gate

    def abort(self) -> None:
        """Mirror ``LLMClient.abort()``: makes in-flight ``complete()``
        calls exit at their next gate or yield boundary.
        """
        self._abort.set()

    @property
    def is_aborted(self) -> bool:
        return self._abort.is_set()

    async def complete(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ) -> AsyncIterator[dict]:
        pass_name = _pass_from_tool_choice(tool_choice)
        self.calls.append((pass_name, tool_choice))

        gates = self._gates[pass_name]
        if gates:
            gate = gates.pop(0)
            gate.reached.set()
            await gate.release.wait()

        if self._abort.is_set():
            return

        if pass_name == "writer":
            payload = self._queues["writer"].pop(0) if self._queues["writer"] else {"content": ""}
            text = payload.get("content", "")
            if text:
                yield {"type": "content", "delta": text}
            yield {"type": "done", "message": {"role": "assistant", "content": text}}
            return

        if pass_name == "editor":
            payload = self._queues["editor"].pop(0) if self._queues["editor"] else {"message": {"tool_calls": []}}
            yield {"type": "done", "message": payload.get("message", {})}
            return

        if pass_name == "workflow":
            payload = self._queues["workflow"].pop(0) if self._queues["workflow"] else {"message": {"tool_calls": []}}
            yield {"type": "done", "message": payload.get("message", {})}
            return

        payload = self._queues["director"].pop(0) if self._queues["director"] else {"tool_calls": []}
        yield {
            "type": "done",
            "message": {"role": "assistant", "content": "", "tool_calls": payload.get("tool_calls", [])},
        }


def llm_factory(fake: FakeLLMClient):
    """Wrap *fake* so calling ``LLMClient(url, api_key=..., profile=...)``
    inside production code yields the same shared instance the test holds.
    """

    def make(*_args, **_kwargs):
        return fake

    return make
