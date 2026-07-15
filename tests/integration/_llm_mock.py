"""LLM client substitute for integration tests of the streaming pipeline.

The real ``backend.inference.client.LLMClient`` reaches the OpenAI-compatible
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
import copy
from typing import Any, AsyncIterator

from backend.inference import AbortToken

_EDITOR_FUNCTION_NAMES = {"editor_apply_patch", "editor_rewrite"}
_DIRECTOR_FUNCTION_NAMES = {"direct_scene"}
_FEEDBACK_FUNCTION_NAMES = {"give_feedback"}
_DIRECTION_NOTE_FUNCTION_NAMES = {"record_direction_note"}


def _validate_tool_calls(tool_calls: Any) -> None:
    """Assert *tool_calls* is the OpenAI ``message.tool_calls`` shape.

    ``parse_tool_calls`` (backend.inference.client) reads ``tc["function"]["name"]``
    and falls back to ``""`` when the function or name is missing, so a
    malformed enqueue would parse to a named-but-empty (or empty-list) tool
    call and the director turn would no-op silently. Raising here turns that
    into a loud failure at the call site that built the bad shape.
    """
    if not isinstance(tool_calls, list):
        raise TypeError(f"tool_calls must be a list, got {type(tool_calls).__name__}")
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            raise TypeError(f"tool_calls[{i}] must be a dict, got {type(tc).__name__}")
        fn = tc.get("function")
        if not isinstance(fn, dict):
            raise ValueError(f"tool_calls[{i}] missing a 'function' dict: {tc!r}")
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"tool_calls[{i}].function.name must be a non-empty string: {tc!r}")
        if "arguments" not in fn:
            raise ValueError(f"tool_calls[{i}].function missing 'arguments': {tc!r}")


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
        if name in _FEEDBACK_FUNCTION_NAMES:
            return "feedback"
        if name in _DIRECTION_NOTE_FUNCTION_NAMES:
            return "direction_note"
        # Any other forced function name belongs to a workflow tool: the
        # toolkit's forced_tool_call helper passes the same dict shape via
        # TOOLS[<wid_registered_name>]["choice"], but the name is not one of
        # the four core pass tools.
        if name:
            return "workflow"
    # No production pass emits any other shape (writer -> None/"none", editor ->
    # "auto"/forced dict, director/workflow -> forced dict with a name). An
    # earlier version returned "director" here as a catch-all, which silently
    # mis-routed an unrecognized tool_choice to the director queue -- a wrong
    # tool_choice convention would then bind responses to the wrong pass and the
    # test would pass for the wrong reason. Fail loudly instead so such a change
    # surfaces as a dispatch error, not a confusing assertion downstream.
    raise ValueError(
        f"Unroutable tool_choice {tool_choice!r}: no pass owns this shape. "
        "If production added a new tool_choice convention, extend "
        "_pass_from_tool_choice to map it explicitly."
    )


class FakeLLMClient:
    """A deterministic stand-in for ``LLMClient`` shared by every
    constructor call within one test.

    Tests interact via the public mutator methods (``enqueue_*``,
    ``gate``) before kicking off the action under test, then drive the
    rest of the test through the HTTP client; the mock yields the
    enqueued event for each matching pass invocation.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[dict]] = {
            "director": [],
            "writer": [],
            "editor": [],
            "feedback": [],
            "direction_note": [],
            "workflow": [],
        }
        # Raw text-completion queue (complete_raw, document text mode) — separate
        # from the tool_choice-dispatched chat queues above; keyed by the call,
        # not by a pass. capture prompt+params for assertions. Each entry is
        # {"content": str, "probs": list} so a test can attach per-token probs.
        self._raw_queue: list[dict] = []
        self.raw_calls: list[dict] = []
        self.completion_mode = "chat"
        self._gates: dict[str, list[PassGate]] = {
            "director": [],
            "writer": [],
            "editor": [],
            "feedback": [],
            "direction_note": [],
            "workflow": [],
        }
        # Mirror LLMClient: the turn's clients share one abort token, so an
        # abort signalled on any of them is visible to all.
        self.abort_token = AbortToken()
        # Public assertion surface: tests inspect ``calls`` directly for
        # dispatch order and invocation counts, so its shape is part of
        # the mock's contract -- do not rename or restructure.
        self.calls: list[tuple[str, Any]] = []
        # Full wire payload of every ``complete()`` call, for KV-cache tests
        # that need to compare the exact messages/tools each pass sent. Deep
        # copies are taken at call time because the editor mutates its ``msgs``
        # list in place across ReAct iterations -- a shallow reference would
        # show the final state for every iteration, not what each one sent.
        self.captured: list[dict] = []

    def enqueue_director(self, tool_calls: list[dict]) -> None:
        """Queue a director response.

        The director pass calls ``parse_tool_calls`` on the result, so
        *tool_calls* must follow the OpenAI ``message.tool_calls`` shape
        (``{"id", "type": "function", "function": {"name", "arguments"}}``).
        A malformed shape would otherwise parse to an empty tool-call list
        downstream and the test would silently exercise a no-op director turn
        rather than the scene it meant to stage -- so validate the shape here
        and raise at enqueue time, where the offending call site is obvious.
        """
        _validate_tool_calls(tool_calls)
        self._queues["director"].append({"tool_calls": tool_calls})

    def enqueue_writer(self, text: str, probs: list[dict] | None = None) -> None:
        # Optional *probs* is a list of normalized token-prob records
        # ({"token","prob","top":[{"t","p"}]}); the mock interleaves them as
        # token_probs chunks after the content delta (chat doc path with logprobs).
        self._queues["writer"].append({"content": text, "probs": probs or []})

    def enqueue_editor(self, decision: dict | None = None) -> None:
        """Queue an editor response. ``decision`` is the ``message`` dict
        the editor pass receives. ``None`` (the default) yields an empty
        message with no tool calls, which causes the editor loop to stop.
        """
        self._queues["editor"].append({"message": decision or {"tool_calls": []}})

    def enqueue_feedback(self, tool_calls: list[dict]) -> None:
        """Queue a feedback response. Like the director, the feedback pass calls
        ``parse_tool_calls`` on the result, so *tool_calls* must follow the
        OpenAI ``message.tool_calls`` shape.
        """
        _validate_tool_calls(tool_calls)
        self._queues["feedback"].append({"tool_calls": tool_calls})

    def enqueue_direction_note(self, tool_calls: list[dict]) -> None:
        """Queue a director-notes response (the ``record_direction_note`` forced call)."""
        _validate_tool_calls(tool_calls)
        self._queues["direction_note"].append({"tool_calls": tool_calls})

    def enqueue_workflow(self, message: dict) -> None:
        self._queues["workflow"].append({"message": message})

    def enqueue_raw(self, text: str, probs: list[dict] | None = None) -> None:
        """Queue a raw text-completion response (``complete_raw``, doc text mode).

        Optional *probs* is a list of normalized token-prob records
        (``{"token","prob","top":[{"t","p"}]}``); when given the mock interleaves
        them as token_probs chunks after the content delta (mirrors the real
        client when n_probs is requested)."""
        self._raw_queue.append({"content": text, "probs": probs or []})

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
        self.abort_token.abort()

    @property
    def is_aborted(self) -> bool:
        return self.abort_token.is_aborted

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
        self.captured.append(
            {
                "pass": pass_name,
                "model": model,
                "tool_choice": copy.deepcopy(tool_choice),
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                # Full param spread (prefill, reasoning kwargs, hyperparams) so
                # doc-mode tests can assert the assisted prefill / reasoning shape.
                "params": copy.deepcopy(params),
            }
        )

        gates = self._gates[pass_name]
        if gates:
            gate = gates.pop(0)
            gate.reached.set()
            await gate.release.wait()

        if self.abort_token.is_aborted:
            return

        if pass_name == "writer":
            payload = self._queues["writer"].pop(0) if self._queues["writer"] else {"content": ""}
            text = payload.get("content", "")
            if text:
                yield {"type": "content", "delta": text}
            # Faithful to a real provider: probs come back only when logprobs
            # were requested (the token_probs flag threads through to params).
            if params.get("logprobs"):
                for rec in payload.get("probs") or []:
                    yield {"type": "token_probs", **rec}
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

        if pass_name == "feedback":
            payload = self._queues["feedback"].pop(0) if self._queues["feedback"] else {"tool_calls": []}
            yield {
                "type": "done",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": payload.get("tool_calls", []),
                },
            }
            return

        if pass_name == "direction_note":
            payload = self._queues["direction_note"].pop(0) if self._queues["direction_note"] else {"tool_calls": []}
            yield {
                "type": "done",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": payload.get("tool_calls", []),
                },
            }
            return

        payload = self._queues["director"].pop(0) if self._queues["director"] else {"tool_calls": []}
        yield {
            "type": "done",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": payload.get("tool_calls", []),
            },
        }

    async def complete_raw(self, prompt: str, model: str, **params) -> AsyncIterator[dict]:
        """Raw text-completion stand-in (document text mode). Captures the
        verbatim prompt + params, then yields the next enqueued raw response."""
        self.raw_calls.append({"prompt": prompt, "model": model, "params": params})
        if self.abort_token.is_aborted:
            return
        payload = self._raw_queue.pop(0) if self._raw_queue else {"content": "", "probs": []}
        text = payload["content"]
        if text:
            yield {"type": "content", "delta": text}
        # llama.cpp returns completion_probabilities only when n_probs is set.
        if params.get("n_probs"):
            for rec in payload.get("probs") or []:
                yield {"type": "token_probs", **rec}
        yield {"type": "done", "message": {"content": text}, "usage": None}


def llm_factory(fake: FakeLLMClient):
    """Wrap *fake* so calling ``LLMClient(url, api_key=..., profile=...)``
    inside production code yields the same shared instance the test holds.

    Propagates the ``completion_mode`` ctor kwarg onto the shared fake so a
    route that constructs its client with the endpoint's mode (e.g. the document
    generate route) makes ``DocumentContinuer`` branch on the real value.
    """

    def make(*_args, **kwargs):
        if "completion_mode" in kwargs:
            fake.completion_mode = kwargs["completion_mode"]
        return fake

    return make
