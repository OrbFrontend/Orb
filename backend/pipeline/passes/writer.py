"""
passes/writer.py — The writer pass: streams the main story response.

With no active Writer tool this is the one-shot pass it has always been: one
completion, ``tool_choice="none"`` whenever the shared base carries schemas,
and a host instruction telling the model not to call anything. That path is
byte-identical to what it was before Writer tools existed, and every install
without a selected v2 resolver takes it.

With one active Writer tool it becomes a bounded ReAct loop: up to
``MAX_WRITER_TOOL_CALLS_PER_TURN`` calls, each answered and followed by another
completion the model can react in, and a final completion with
``tool_choice="none"`` once the budget is spent. Three rules make that safe
rather than merely limited:

* **Only standard structured ``tool_calls`` count.** The generic
  ``parse_tool_calls`` content-body fallbacks are useful for a forced,
  non-streamed agent call; after Writer prose has already been streamed to the
  user, reinterpreting narrative JSON as a call would let the story invoke an
  extension.
* **The captured allowlist is the authority, not the prompt.** The OOC block
  improves tool choice. What Orb *executes* is checked against the one binding
  this turn published a schema for.
* **Every failure still lets the Writer finish.** An unknown call, a bad
  argument, or an extension error becomes a fixed error result and another
  completion. Non-retryable failures disable the tool for the following completion;
  invalid arguments alone may be corrected while call budget remains.

The budget is charged per *iteration that returned calls*, not per successful
resolution. The configured Writer ``max_tokens`` is also one allowance shared
by every completion, and extension flow quotas are shared by every invocation.
The call ceiling therefore cannot multiply either model output or package work
by resetting a per-call counter.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ...core import (
    INVALID_ARGUMENTS,
    MAX_WRITER_TOOL_CALLS_PER_TURN,
    RESOLVER_UNAVAILABLE,
    TOOL_NOT_AVAILABLE,
    AssistantToolMessage,
    ContentPart,
    WireMessage,
    WriterToolInvocation,
    build_multimodal_content,
    extract_hyperparams,
    valid_call_id,
    writer_tool_error,
    writer_tool_ok,
)
from ...inference import CachedBase, LLMClient, _KVCacheTracker, reasoning_cfg
from ...workflows import WriterToolRequest, WriterToolTurnBudget
from .editor.length_guard import LengthGuard, writer_nudge

if TYPE_CHECKING:
    from ..state import ExtensionContext, TurnState, WriterToolPolicy, _PipelineConfig

logger = logging.getLogger(__name__)

NO_TOOLS_NUDGE = "**Do not use tool or function calls this turn.**\n\n"

WRITER_TOOL_POLICY = """[OOC: Writer tool policy for this turn.
You may write normally or call ONLY `{name}`.
{purpose}Call it only when the uncertain action described by the tool should be
resolved before you decide what happens.
You may call it up to {budget} times this turn, one call per message. Never
call Director or Editor tools.
Each time you call it, pause at the current point. After Orb returns the
result, continue from that exact point without repeating prior prose.
]

"""
"""The closed host-authored tail block, with exactly two package-influenced
holes: the derived tool name and a bounded purpose line built from the
package's description and a schema-derived parameter list. The authority,
exclusivity, call budget, and continuation wording are fixed Orb text -- a
package that could rewrite those would be writing the rule it is bound by. The
budget is interpolated from the host constant for the same reason: a number the
model reads and a number the loop enforces that could disagree is worse than
either alone.

The prompt is not the security boundary. It is expected to improve tool choice,
especially in single-model mode where the shared base also carries the agent's
schemas, and every returned call is validated against the captured binding
regardless of what this says.
"""


def writer_tool_block(policy: WriterToolPolicy) -> str:
    """Render the tail OOC policy for *policy*, or ``""`` when no tool is active."""
    if not policy.active:
        return ""
    purpose = ""
    if policy.description:
        purpose = f"What it does: {policy.description}\n"
    if policy.parameter_summary:
        purpose += f"It takes: {policy.parameter_summary}\n"
    return WRITER_TOOL_POLICY.format(
        name=policy.wire_name,
        purpose=purpose,
        budget=MAX_WRITER_TOOL_CALLS_PER_TURN,
    )


def build_writer_content(
    lorebook_block: str,
    inj_block: str,
    has_tool_schemas: bool,
    effective_msg: str,
    attachments: Sequence[Mapping[str, Any]] | None,
    length_guard: LengthGuard | None,
    text_mode: bool = False,
    extension_block: str = "",
    writer_tool_policy: WriterToolPolicy | None = None,
) -> str | list[ContentPart]:
    """Build the writer's user-message content (string or multimodal list).

    Built once and threaded into both the writer pass and the editor, which
    replays it verbatim to extend the writer's KV-cached prefix. The length-guard
    nudge (preventive arm) fires only in enforce mode; a non-None *length_guard*
    already means the feature is enabled. In *text_mode* the no-tools nudge is
    dropped — no tool harness is rendered, so the instruction is meaningless.

    When a Writer tool is active the no-tools instruction is replaced by the
    policy block, and that block is the **semantic tail**: with attachments it
    is a final text part *after* the images, not part of the combined body. A
    policy separated from the model's turn boundary by every attached image is
    not the last thing the model reads, which is the one property it relies on.
    """
    tail = ""
    if lorebook_block:
        tail += "___\n\n" + lorebook_block + "\n\n"
    if inj_block:
        tail += "___\n\n" + inj_block + "\n\n"
    # Community-extension context sits with the other per-turn trailing blocks,
    # ahead of the effective user message. Built once here like everything else
    # in this tail, so the editor's verbatim replay extends the same KV prefix.
    if extension_block:
        tail += "___\n\n" + extension_block + "\n\n"
    policy_block = writer_tool_block(writer_tool_policy) if writer_tool_policy is not None else ""
    # Retained as defense in depth when nothing is callable: ``tool_choice`` is
    # already ``"none"`` there, and the instruction costs one line.
    if has_tool_schemas and not text_mode and not policy_block:
        tail += NO_TOOLS_NUDGE
    tail += writer_nudge(length_guard)
    tail += "___\n\n" + effective_msg + "\n\n"

    return build_multimodal_content(tail, attachments, trailing_text=policy_block)


def standard_tool_calls(message: Mapping[str, Any]) -> list[dict]:
    """The message's standard structured ``tool_calls``, and nothing else.

    Deliberately *not* :func:`~backend.inference.parse_tool_calls`. Its
    content-body fallbacks (Hermes tags, Gemma tokens, bare JSON) exist for
    forced, non-streamed agent calls where the content *is* the arguments. In
    the Writer they would be applied to prose that has already been streamed to
    the user, so a character writing a JSON object would become a tool call --
    and a well-chosen one would become a call to the extension.
    """
    calls = message.get("tool_calls")
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _decode_arguments(call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Decode one call's arguments, or ``None`` when they are unusable."""
    function = _call_function(call)
    if function is None:
        return None
    raw = function.get("arguments", "")
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _assistant_message(content: str, calls: Sequence[Mapping[str, Any]]) -> AssistantToolMessage:
    """A sanitized assistant message for replay: content plus structured calls.

    Provider-only fields -- ``finish_reason``, usage, reasoning, raw chunks --
    are dropped rather than carried. They are not part of any request Orb
    sends, and a trace that accumulated them would eventually put one in front
    of a model.
    """
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": str(call.get("id", "")),
                "type": "function",
                "function": {
                    "name": str((_call_function(call) or {}).get("name", "")),
                    "arguments": _argument_text(call),
                },
            }
            for call in calls
        ],
    }


def _call_function(call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return a call's nested function object only when it is replayable."""
    function = call.get("function")
    return function if isinstance(function, Mapping) else None


def _argument_text(call: Mapping[str, Any]) -> str:
    function = _call_function(call)
    raw = function.get("arguments", "") if function is not None else ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _completion_output_tokens(done: Mapping[str, Any]) -> int:
    """Provider output-token usage, with a conservative local fallback."""
    usage = done.get("usage")
    if isinstance(usage, Mapping):
        for key in ("completion_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return max(1, int(value))

    message = done.get("message")
    if not isinstance(message, Mapping):
        return 0
    generated = {
        key: message[key] for key in ("content", "reasoning_content", "tool_calls") if message.get(key) not in (None, "", [])
    }
    if not generated:
        return 0
    rendered = json.dumps(generated, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    # Providers that omit usage give us no tokenizer-specific count. UTF-8
    # bytes are a deliberately pessimistic upper bound for the tokenizers Orb
    # supports; under-counting here would let later completions multiply the
    # configured allowance, while over-counting merely ends this turn early.
    return len(rendered.encode("utf-8"))


def _bounded_hyperparams(hyperparams: Mapping[str, Any], remaining_tokens: int | None) -> dict[str, Any]:
    """Copy hyperparameters with the remaining turn-wide output allowance."""
    params = dict(hyperparams)
    if remaining_tokens is not None:
        params["max_tokens"] = max(1, remaining_tokens)
    return params


def _tool_result(call_id: str, payload: Mapping[str, Any]) -> WireMessage:
    """One tool-role reply, carrying canonical bounded JSON and nothing else."""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }


CONTINUE_WITHOUT_TOOLS = (
    "[OOC: Continue the reply from exactly where it stopped. Do not repeat prior prose and do not call any tool.]"
)
"""The recovery branch for a call Orb cannot answer as a tool exchange.

A call with a missing or malformed id cannot be replayed: a matching tool
result needs the provider's own id, and fabricating one would put a message in
the transcript claiming the provider made a call it did not make. The clean
branch drops the unusable assistant message entirely and asks for a
continuation from the accumulated prose instead."""


async def writer_pass(
    client: LLMClient,
    base: CachedBase,
    settings: Mapping[str, Any],
    content: str | list[ContentPart],
    *,
    kv_tracker=None,
    reasoning_on: bool = True,
    reasoning_prefill: str = "",
    policy: WriterToolPolicy | None = None,
    conversation_id: str | None = None,
    turn_seed: str = "",
    card: Mapping[str, Any] | None = None,
    character_id: str | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    effective_msg: str = "",
    direction: Mapping[str, Any] | None = None,
) -> AsyncIterator[dict]:
    """Yield ``{"type": "content"|"reasoning"|"tool_status"|"trace", ...}`` dicts.

    *content* is the writer's user-message body, prebuilt by
    ``build_writer_content`` and shared with the editor. The tool blob comes from
    *base* so it stays byte-identical with the director and editor passes.

    The terminal ``trace`` event carries the sanitized message list a downstream
    pass may replay. It is always emitted, and for a turn with no tool call it
    is exactly the ``user request + assistant draft`` pair the pipeline built
    before this loop existed.

    Each iteration re-sends the same shared prefix and a *growing* trailing
    transcript, so the cache extends rather than forking: the cost of a second
    call is the tokens the first exchange added, not a fresh prefill.
    """
    active = policy is not None and policy.active
    trailing: list[WireMessage] = [{"role": "user", "content": content}]
    hyperparams = extract_hyperparams(settings)
    configured_max_tokens = hyperparams.get("max_tokens")
    remaining_tokens = (
        int(configured_max_tokens)
        if isinstance(configured_max_tokens, (int, float))
        and not isinstance(configured_max_tokens, bool)
        and configured_max_tokens > 0
        else None
    )
    turn_budget = WriterToolTurnBudget() if active else None
    tool_available = active
    logger.info(
        "Writer pass: tools included=%s, writer tool=%s",
        json.dumps([t["function"]["name"] for t in base.tools]) if base.tools else "[]",
        policy.wire_name if active and policy is not None else "none",
    )

    prose = ""
    calls_made = 0
    while True:
        callable_now = tool_available and calls_made < MAX_WRITER_TOOL_CALLS_PER_TURN
        segment = ""
        message: dict = {}
        done: dict = {}
        async for item in _complete(
            client,
            base,
            trailing,
            # ``auto`` only while budget remains. Otherwise the historical
            # behavior exactly: no tools means no ``tool_choice``, and a shared
            # blob the Writer may not touch means ``"none"``.
            tool_choice="auto" if callable_now else ("none" if base.tools else None),
            kv_tracker=kv_tracker,
            reasoning_on=reasoning_on,
            reasoning_prefill=reasoning_prefill,
            hyperparams=_bounded_hyperparams(hyperparams, remaining_tokens),
        ):
            if item["type"] == "done":
                message = item["message"]
                done = item
                break
            if item["type"] == "content":
                segment += item["delta"]
            yield item
        prose += segment
        if remaining_tokens is not None:
            remaining_tokens = max(0, remaining_tokens - _completion_output_tokens(done))

        calls = standard_tool_calls(message) if callable_now else []
        if calls and remaining_tokens == 0:
            # The configured Writer output allowance is turn-scoped, not reset
            # for every ReAct segment. There is no budget left to return a tool
            # result and let the model react, so execute nothing and retain only
            # the ordinary prose from this terminal message.
            logger.warning("Writer exhausted its turn output budget while requesting a tool; ignoring the call")
            trailing.append({"role": "assistant", "content": segment})
            yield {"type": "trace", "messages": list(trailing), "used_tool": calls_made > 0}
            return
        if not calls:
            if standard_tool_calls(message):
                # A provider that ignored ``tool_choice="none"``. Execute
                # nothing, keep whatever ordinary prose came with it, and never
                # extend the loop -- the budget is a host property, not a
                # provider promise.
                logger.warning("Writer returned a tool call with no budget left; ignoring it")
            trailing.append({"role": "assistant", "content": segment})
            # A failed or refused call still warmed this exact transcript.
            # Downstream replay is about protocol shape and KV continuity, not
            # whether the extension returned success.
            yield {"type": "trace", "messages": list(trailing), "used_tool": calls_made > 0}
            return

        assert policy is not None and policy.binding is not None
        answered: dict = {}
        async for item in _answer_calls(
            calls,
            prose=prose,
            policy=policy,
            settings=settings,
            client=client,
            conversation_id=conversation_id,
            turn_seed=turn_seed,
            card=card,
            character_id=character_id,
            history=history,
            effective_msg=effective_msg,
            direction=direction,
            invocation_index=calls_made,
            turn_budget=turn_budget,
        ):
            if item["type"] == "answered":
                answered = item
                break
            yield item

        if answered.get("aborted"):
            return
        if answered.get("disable_tool"):
            tool_available = False
        if answered.get("recover_clean"):
            # Unanswerable: a tool result must carry the provider's own id, and
            # inventing one would claim a call the provider never made. Drop the
            # whole exchange and finish from the accumulated prose instead.
            async for item in _continue_clean(
                client,
                base,
                trailing[0],
                prose,
                kv_tracker=kv_tracker,
                reasoning_on=reasoning_on,
                reasoning_prefill=reasoning_prefill,
                hyperparams=_bounded_hyperparams(hyperparams, remaining_tokens),
            ):
                yield item
            return

        # The assistant message carries only *this* iteration's prose. Appending
        # the accumulation would repeat every earlier segment once per call.
        trailing.append(_assistant_message(segment, calls))
        trailing.extend(answered["replies"])
        calls_made += 1


def _complete(
    client: LLMClient,
    base: CachedBase,
    trailing: Sequence[WireMessage],
    *,
    tool_choice: Any,
    kv_tracker,
    reasoning_on: bool,
    reasoning_prefill: str,
    hyperparams: Mapping[str, Any],
) -> AsyncIterator[dict]:
    return base.complete(
        client,
        label="writer",
        trailing=trailing,
        tool_choice=tool_choice,
        kv_tracker=kv_tracker,
        **reasoning_cfg(reasoning_on, reasoning_prefill),
        **hyperparams,
    )


async def _answer_calls(
    calls: Sequence[Mapping[str, Any]],
    *,
    prose: str,
    policy: WriterToolPolicy,
    settings: Mapping[str, Any],
    client: LLMClient,
    conversation_id: str | None,
    turn_seed: str,
    card: Mapping[str, Any] | None,
    character_id: str | None,
    history: Sequence[Mapping[str, Any]] | None,
    effective_msg: str,
    direction: Mapping[str, Any] | None,
    invocation_index: int,
    turn_budget: WriterToolTurnBudget | None,
) -> AsyncIterator[dict]:
    """Answer one iteration's calls, yielding progress then a terminal verdict.

    "Answer" covers refusal: every standard call gets a tool-role reply so the
    transcript stays protocol-valid, and only a single call to the captured wire
    name with a usable id and valid arguments actually executes anything.

    Yields ``tool_status`` events while work runs, then exactly one
    ``{"type": "answered", ...}`` the caller consumes and does not forward --
    the same shape :func:`_complete` uses for ``done``. The caller owns every
    completion, so this function issues none and the loop stays in one place.
    """
    binding = policy.binding
    assert binding is not None

    replies: list[WireMessage] = []
    disable_tool = False

    # Multiple calls in one message execute nothing. Not the per-turn budget,
    # which counts messages and lets the model call again after seeing a result:
    # this refuses a single message asking for two resolutions at once, because
    # that is not a request whose first half is meaningful.
    only = calls[0] if len(calls) == 1 else None
    if only is None:
        logger.warning("Writer returned %d tool calls in one message; executing none", len(calls))
        disable_tool = True

    for call in calls:
        raw_call_id = call.get("id")
        function = _call_function(call)
        name = function.get("name") if function is not None else None
        if not valid_call_id(raw_call_id) or not isinstance(name, str) or not name:
            # Unanswerable: a tool result must carry the provider's own id, and
            # the assistant call must carry a replayable function object. The
            # caller recovers cleanly rather than inventing either protocol value.
            logger.warning("Writer tool call has an unusable id or function shape; recovering without a tool exchange")
            yield {
                "type": "answered",
                "replies": [],
                "aborted": False,
                "recover_clean": True,
                "disable_tool": True,
            }
            return
        assert isinstance(raw_call_id, str)
        call_id = raw_call_id
        if call is not only or name != policy.wire_name:
            replies.append(_tool_result(call_id, writer_tool_error(TOOL_NOT_AVAILABLE)))
            disable_tool = True
            continue
        arguments = _decode_arguments(call)
        if arguments is None:
            replies.append(_tool_result(call_id, writer_tool_error(INVALID_ARGUMENTS)))
            continue

        # Emitted around the invocation rather than in a ``finally``: an async
        # generator cannot yield while unwinding a ``GeneratorExit``, so a
        # ``finally`` that tried to clear the pill on an aborted turn would
        # raise instead. The frontend's ``afterStream()`` sweep is what
        # guarantees the pill clears on abort, disconnect, and exception; this
        # pair covers the ordinary success and failure paths.
        yield {"type": "tool_status", "running": True, "label": policy.label}
        try:
            result = await binding.invoke(
                WriterToolRequest(
                    invocation=WriterToolInvocation(
                        key=binding.spec.key,
                        call_id=call_id,
                        arguments=arguments,
                        # Host-supplied, from the prose already streamed. The
                        # package cannot ask the model to echo the draft, and a
                        # model that tried could not redirect the invocation.
                        draft=prose,
                        conversation_id=conversation_id,
                        turn_seed=turn_seed,
                        invocation_index=invocation_index,
                    ),
                    settings=settings,
                    client=client,
                    is_cancelled=lambda: bool(getattr(client, "is_aborted", False)),
                    character_id=character_id,
                    card=card,
                    history=tuple(history or ()),
                    last_user_message=effective_msg,
                    direction=direction,
                    turn_budget=turn_budget,
                )
            )
        except Exception as exc:
            # Cancellation is a turn-level stop, not a resolver result. The
            # binding cannot expose its feature-local FlowCancelled type across
            # the layer boundary, so the shared abort token is the neutral
            # contract: once set, run no further completion and end the loop.
            if client.is_aborted:
                logger.info("Writer tool %r cancelled with the owning turn", policy.wire_name)
                yield {"type": "tool_status", "running": False, "label": policy.label}
                yield {
                    "type": "answered",
                    "replies": [],
                    "aborted": True,
                    "recover_clean": False,
                    "disable_tool": True,
                }
                return
            # Timeout, revoked permission, invalid output, and a sanitized flow
            # error all become one fixed code.
            # The Writer receives no internal text: it is a model reading a
            # transcript, and an exception string there is both a leak and an
            # instruction Orb did not author.
            logger.warning("Writer tool %r failed: %s", policy.wire_name, exc)
            replies.append(_tool_result(call_id, writer_tool_error(RESOLVER_UNAVAILABLE)))
            disable_tool = True
        else:
            replies.append(_tool_result(call_id, writer_tool_ok(result.value)))
        yield {"type": "tool_status", "running": False, "label": policy.label}

    yield {
        "type": "answered",
        "replies": replies,
        "aborted": False,
        "recover_clean": False,
        "disable_tool": disable_tool,
    }


async def _continue_clean(
    client: LLMClient,
    base: CachedBase,
    request: WireMessage,
    prose: str,
    *,
    kv_tracker,
    reasoning_on: bool,
    reasoning_prefill: str,
    hyperparams: Mapping[str, Any],
) -> AsyncIterator[dict]:
    """Continue from a branch with no tool messages at all.

    Used when a returned call cannot be represented as a valid exchange. The
    unusable assistant message is dropped and the accumulated prose is replayed
    as an ordinary assistant turn, so the transcript Orb sends is one it could
    have produced without any tool at all.

    It takes the original *request* rather than the loop's accumulated trailing
    for that same reason: by iteration two the trailing holds earlier
    assistant/tool exchanges whose prose is already inside *prose*, and
    replaying both would send every earlier segment twice.
    """
    branch: list[WireMessage] = [
        request,
        {"role": "assistant", "content": prose},
        {"role": "user", "content": CONTINUE_WITHOUT_TOOLS},
    ]
    continuation = ""
    async for item in _complete(
        client,
        base,
        branch,
        tool_choice="none",
        kv_tracker=kv_tracker,
        reasoning_on=reasoning_on,
        reasoning_prefill=reasoning_prefill,
        hyperparams=hyperparams,
    ):
        if item["type"] == "done":
            break
        if item["type"] == "content":
            continuation += item["delta"]
        yield item
    # The recovery request is host scaffolding, not conversation. Downstream
    # passes replay the normalized shape instead, so the trace reports no tool
    # use and carries only the request and the finished prose.
    yield {
        "type": "trace",
        "messages": [request, {"role": "assistant", "content": prose + continuation}],
        "used_tool": False,
    }


async def writer_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    settings: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    kv_tracker: _KVCacheTracker,
    extension_context: ExtensionContext | None = None,
    conversation_id: str | None = None,
    character_id: str | None = None,
    card: Mapping[str, Any] | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    turn_seed: str = "",
) -> AsyncIterator[dict]:
    """Input-prep + writer pass + event translation.

    Builds ``state.writer_content`` once (replayed verbatim by the editor to
    extend the writer's KV-cached prefix), runs :func:`writer_pass` translating
    ``content``→``token`` and ``reasoning``→``reasoning`` events, and accumulates
    the writer's wall time into ``state.latency``.

    ``state.resp_text`` is the concatenation of Writer content across the
    pre-call and continuation messages. Tool arguments and results never become
    prose: they live in ``state.writer_trace``, which persistence does not read.
    """
    state.writer_content = build_writer_content(
        state.writer_lorebook_block,
        state.inj_block,
        bool(cfg.writer_lane.base.tools),
        state.effective_msg,
        attachments,
        cfg.length_guard,
        cfg.writer_text_mode,
        extension_block=extension_context.writer if extension_context else "",
        writer_tool_policy=cfg.writer_tool_policy,
    )
    writer_t0 = time.monotonic()
    async for item in writer_pass(
        cfg.writer_lane.client,
        cfg.writer_lane.base,
        settings,
        state.writer_content,
        kv_tracker=kv_tracker,
        reasoning_on=cfg.writer_reasoning_on,
        reasoning_prefill=cfg.writer_reasoning_prefill,
        policy=cfg.writer_tool_policy,
        conversation_id=conversation_id,
        # Host-owned attempt identity. It stays stable for this pipeline run but
        # changes on regenerate even when history and input text are identical.
        # The executor additionally mixes in revision, tool key, and call id.
        turn_seed=turn_seed,
        card=card,
        character_id=character_id,
        history=history,
        effective_msg=state.effective_msg,
        direction=state.as_direction_view(),
    ):
        if item["type"] == "reasoning":
            state.reasoning_writer += item["delta"]
            yield {
                "event": "reasoning",
                "data": {"pass": "writer", "delta": item["delta"]},
            }
        elif item["type"] == "tool_status":
            # One fixed host-owned channel. The package chooses neither the
            # event name nor the payload keys, and no argument, draft, result,
            # or error text crosses it. Clearing on abort, disconnect, and
            # exception is the frontend's ``afterStream()`` sweep, not a
            # ``finally`` here: an async generator cannot yield while unwinding
            # a ``GeneratorExit``, so the cleanup that *looked* most reliable is
            # the one that would raise on the path it was written for.
            yield {
                "event": "writer_tool_status",
                "data": {"running": bool(item["running"]), "label": item["label"]},
            }
        elif item["type"] == "trace":
            state.writer_trace = list(item["messages"])
            state.writer_tool_used = bool(item["used_tool"])
        else:
            state.resp_text += item["delta"]
            yield {"event": "token", "data": item["delta"]}
    # agent_latency_ms is the whole turn's wall time; accumulate the writer's
    # span here (director + editor add their own).
    state.latency += int((time.monotonic() - writer_t0) * 1000)


__all__ = [
    "build_writer_content",
    "standard_tool_calls",
    "writer_pass",
    "writer_stage",
    "writer_tool_block",
]
