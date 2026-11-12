"""Run one assistant request: choose a tool, ask for its arguments, execute, repeat until a reply."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar

from pydantic import ValidationError

from ...core import ChatMessage, WireMessage, agent_lane_cut_off, extract_hyperparams
from ...core.llm_types import CompletionMessage
from ...core.settings import Settings
from ...inference import (
    CachedBase,
    LLMClient,
    assistant_tool_call,
    honors_forced_tool_choice,
    parse_tool_calls,
    reasoning_cfg,
    strict_tool_arguments,
    tool_result,
    without_reasoning,
)
from .contracts import (
    CONTEXT_BUDGET_CHARS,
    CONTEXT_SLACK_CHARS,
    MAX_MODEL_CALLS,
    MAX_RESULT_CHARS,
    MAX_SAME_CALLS,
    MAX_TOOL_EXECUTIONS,
    REPLY,
    STEP_TOOL_NAME,
    AssistantEvent,
    AssistantTool,
    AssistantUnavailable,
    Continuation,
    HistoryItem,
    ToolArguments,
    ToolFailure,
    ToolOutput,
    ViewContext,
)
from .prompt import history_messages, reply_tool, request_message, step_tool, system_prompt, tool_schema

logger = logging.getLogger(__name__)

_UNREADABLE = "The model's reply was not a readable step."
_SEVERAL = "The model's reply made several calls at once, where one was asked for."
_CORRECT = "Nothing ran. Choose the tool again and correct its arguments."
_LAST_RUN = f"That was the last tool run for this request. Choose {REPLY} now and answer from what you have."
_NOT_RUN = "Not run: the request was stopped before this change started. Nothing was changed."
_HANDED_OFF = "Handed to the chat, which starts the work there if it is still open."
_CONTEXT_CHANGED = "This request ends here, since what it would read next has changed; ask again to go on from there."
_DELIVERED = "Shown to the user."

_T = TypeVar("_T")
_PROGRESS_LABEL_CHARS = 200


class _Limit(Exception):
    """A run budget is spent."""


class _Repeating(Exception):
    """The model made the same call again and again."""


class _Stopped(Exception):
    """The run's Stop or a dropped connection landed."""


async def settled(operation: Awaitable[_T]) -> _T:
    """Await *operation* through any cancellation of the caller, then let that cancellation continue.

    An admitted write runs to its end: cancelling the coroutine awaiting a commit proves nothing about whether SQLite rolled it
    back, so the caller waits for the same task rather than abandoning it half-way.
    """
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and task.exception() is not None:
            logger.error("Assistant write failed while its request was cancelled", exc_info=task.exception())
        raise


async def _labels(operation: asyncio.Future[Any], progress: asyncio.Queue[str]) -> AsyncGenerator[str, None]:
    """Each label *progress* receives while *operation* runs; one that arrives after it has finished is stale and dropped.

    Closed early, it cancels *operation* and waits for it to end; a write wrapped in :func:`settled` ends whole.
    """
    try:
        while not operation.done():
            getter = asyncio.ensure_future(progress.get())
            try:
                await asyncio.wait({operation, getter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                getter.cancel()
            if getter.done() and not getter.cancelled():
                yield getter.result()
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.wait({operation})


def _chars(messages: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        total += len(content) if isinstance(content, str) else 0
        total += sum(len(call["function"]["arguments"]) for call in message.get("tool_calls") or ())
    return total


def _replay_window(replay: Sequence[WireMessage], room: int) -> list[WireMessage]:
    """The newest part of *replay* that fits *room* characters, starting at a request.

    The start moves a whole ``CONTEXT_SLACK_CHARS`` at a time, measured from the transcript's own start, so one request and
    the next replay the same opening and the server's prefix cache holds through it. A start that moved with every request
    would re-read the whole history each time.
    """
    drop = max(0, -(-(_chars(replay) - room) // CONTEXT_SLACK_CHARS)) * CONTEXT_SLACK_CHARS
    passed = 0
    for index, message in enumerate(replay):
        if message["role"] == "user" and passed >= drop:
            return list(replay[index:])
        passed += _chars([message])
    return []


def _repeated(output: ToolOutput) -> str:
    """The refusal for a change this request already made: true about what exists, so the model cannot claim a second one."""
    ids = json.dumps(dict(output.ids), ensure_ascii=False)
    return (
        f"Nothing ran. This request already made exactly this change once ({output.summary} ids: {ids}); "
        "a change is made once per request, so no second one exists. If the user asked for more than one, "
        "tell them plainly that only one was made."
    )


def _reproposed(output: ToolOutput) -> str:
    """The refusal for an edit this request already proposed: sent again it would apply twice, or loop until the budget ends."""
    return (
        f"Nothing ran. This request already proposed exactly this edit ({output.summary}). It is still proposed and waits "
        "for the user's Apply; do not propose it again."
    )


def _encode(data: Mapping[str, Any], summary: str = "") -> str:
    content = json.dumps(data, ensure_ascii=False)
    if len(content) <= MAX_RESULT_CHARS:
        return content
    # A tool bounds its own result; this only stops a mistake there from spending the whole context. It stays valid JSON, and
    # says so instead of cutting a value mid-way.
    logger.warning("Assistant result of %d characters passed the %d cap", len(content), MAX_RESULT_CHARS)
    return json.dumps(
        {
            "error": f"The result was {len(content)} characters, over the {MAX_RESULT_CHARS} limit. Narrow the request.",
            "summary": summary[:500],
        },
        ensure_ascii=False,
    )


def _several_calls(response: Mapping[str, Any]) -> bool:
    """Whether an unusable reply held more than one call: the one fault a retry can name, so the model can mend it."""
    return len(response.get("tool_calls") or parse_tool_calls(response)) > 1


def _one_at_a_time(forced: str | None) -> str:
    return (
        f"[Nothing ran: the previous reply made several calls at once. Make exactly one call now{f', to {forced}' if forced else ''}; "
        "anything more waits for its result and comes one call at a time.]"
    )


def _any_call(response: Mapping[str, Any], names: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The single-call protocol's ``(tool, arguments)``: one whole call to a listed tool, or to ``reply`` with its answer."""
    calls = parse_tool_calls(response)
    name = calls[0].get("name") if len(calls) == 1 else None
    if not isinstance(name, str) or (name not in names and name != REPLY):
        return None
    arguments = strict_tool_arguments(response, name)
    if arguments is None:
        return None
    if name == REPLY:
        message = arguments.get("message")
        if set(arguments) != {"message"} or not isinstance(message, str) or not message.strip():
            return None
    return name, arguments


def _step_choice(arguments: Mapping[str, Any], names: Mapping[str, Any]) -> tuple[str, str] | None:
    """The step's ``(tool, message)``, or ``None`` when it is not exactly that pair of usable strings."""
    tool, message = arguments.get("tool"), arguments.get("message")
    if set(arguments) != {"tool", "message"} or not isinstance(tool, str) or not isinstance(message, str):
        return None
    if tool not in names and tool != REPLY:
        return None
    message = message.strip()
    return (tool, message) if message or tool != REPLY else None


def _validated(tool: AssistantTool, arguments: Mapping[str, Any]) -> ToolArguments | str:
    """The tool's arguments, or the reason they were refused.

    A strict-schema endpoint must send every property, so it sends ``null`` for an optional one it leaves out; that is the one
    normalization. Unknown fields and wrong types are refused, not coerced.
    """
    fields = tool.arguments.model_fields
    supplied = {
        key: value
        for key, value in arguments.items()
        if not (value is None and key in fields and not fields[key].is_required())
    }
    try:
        return tool.arguments.model_validate(supplied)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}" for error in exc.errors()]
        return "Invalid arguments. " + "; ".join(problems[:8])


async def run_assistant(
    client: LLMClient,
    model: str,
    *,
    settings: Settings,
    tools: Sequence[AssistantTool],
    history: Sequence[HistoryItem],
    request: str,
    view: ViewContext,
    progress: asyncio.Queue[str] | None = None,
    single_call: bool | None = None,
    resume: Continuation | None = None,
    unchanged: bool = False,
    keep: Callable[[Continuation], None] | None = None,
    reasoning: bool = False,
    note: str = "",
) -> AsyncIterator[AssistantEvent]:
    """Answer *request*, yielding progress and exactly one ``done`` unless the model or provider fails.

    The transcript only grows: an unusable reply is retried against the same prefix, told to make one call when it made several,
    and every replayed call has its own id and one matching result. Old result bodies give way to their summaries when the
    working context passes its budget.

    The same call made ``MAX_SAME_CALLS`` times in a row ends the run before the last of them executes.

    A write never runs on a retried or refused reply. Once it starts it settles whole, a ``mutation`` follows its commit, the
    same change is not made twice in one request, nor the same edit proposed twice, and an early ``done`` names the changes
    already committed. A confirm tool's proposal becomes a ``confirm`` event and the run carries on. *progress* is where a
    tool that reports progress puts its labels; each one becomes a ``progress`` event while that tool runs.

    *single_call* makes each step one call over every tool's schema plus ``reply``, in place of a step call followed by an
    argument call. Left ``None``, the run takes it wherever the endpoint sends native tool schemas and honours forced tool
    choice, and two calls a step elsewhere; the assistant benchmark (scripts/bench/assistant_bench.py) measures both.

    *resume* is the previous run's :class:`Continuation`. Made under this run's system prompt, tools and model, and with room
    left for a run, it takes the place of *history*, and the request says whether anything was committed since (*unchanged*).
    A run that ends with a reply hands its own to *keep*.

    *note* is the caller's word about state the request should know of, such as changes earlier requests proposed that still
    wait for the user; it follows the view note.

    *reasoning* turns thinking on for every call of the run. Its reasoning replays under the names it streamed, and the template
    is asked to keep it on earlier turns, so a follow-up renders what this run read. A run with thinking off sends no reasoning
    back, a resumed one's included.
    """
    by_name = {tool.name: tool for tool in tools}
    latest = request_message(request, view, note=note)
    # Restrict the tool list when forced choice is unreliable.
    thinking = reasoning_cfg(reasoning, preserve=True)
    shared_tools = honors_forced_tool_choice(getattr(client, "base_url", ""), model, thinking)
    if single_call is None:
        # Decided once for the run, so every call in it shares one tool blob. Structured output and text completion send no
        # schemas to choose among, and an endpoint that ignores forced choice may ignore ``required`` too.
        single_call = shared_tools and client.sends_tool_schemas([latest], model)
    step = step_tool(tools)
    if single_call:
        schemas = [reply_tool(), *(tool_schema(tool, described=True) for tool in tools)]
    else:
        schemas = [step, *(tool_schema(tool) for tool in tools if tool.takes_arguments)]
    # The same call path and samplers as the pipeline's agent passes: the lane's own hyperparameters, its max_tokens included,
    # with the system prompt and the replayed history as the cached prefix every call of the run shares.
    samplers = extract_hyperparams(settings, lane="agent")

    system: ChatMessage = {"role": "system", "content": system_prompt(tools, single_call=single_call)}
    fingerprint = hashlib.sha256(
        # The endpoint as well as the model: reasoning signed or encrypted by one provider is not valid on another.
        json.dumps(
            [system["content"], schemas, model, getattr(client, "base_url", "")], sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
    replay: list[WireMessage] = []
    resumed = False
    if resume is not None and resume.fingerprint == fingerprint:
        # The previous run's own messages, its results whole: the server's cache holds through them and nothing is read again.
        followed = request_message(request, view, follow_up=unchanged, note=note)
        if _chars([*resume.messages, followed]) <= CONTEXT_BUDGET_CHARS - CONTEXT_SLACK_CHARS:
            latest, resumed = followed, True
            replay = [*resume.messages] if reasoning else [without_reasoning(message) for message in resume.messages]
    if _chars([latest]) > CONTEXT_BUDGET_CHARS:
        raise AssistantUnavailable("That request is too long for the assistant. Shorten it and send it again.")
    if not resumed:
        # An earlier request that got no answer is replayed as a quoted record, never as part of this one: it is what authorises
        # a write, and an interrupted write must not run again. History therefore never ends with a user turn.
        replay = [*history_messages(history)]
        if replay and replay[-1]["role"] == "user":
            replay.pop()
        replay = _replay_window(replay, CONTEXT_BUDGET_CHARS - _chars([latest]))
    run: list[WireMessage] = []
    # Positions in ``run`` of the results that still carry their full body, oldest first, with what replaces each.
    full_results: list[tuple[int, str, str]] = []
    model_calls = executions = 0
    # What this run's writes committed: one line each for an early ``done``, and each exact call so it is never made twice.
    committed: list[str] = []
    made: dict[str, ToolOutput] = {}
    last_call, same_calls = "", 0

    def fit() -> bool:
        """Bring the working context under its budget; the newest result and the latest request are never dropped.

        Every change here rewrites text the model has read, and the server re-reads the request from that point. So one pass
        frees ``CONTEXT_SLACK_CHARS`` more than it must, and the calls after it only add to the end until the budget is
        passed again. Results go first, since they sit after the history.
        """

        def size() -> int:
            return _chars([*replay, latest, *run])

        if size() <= CONTEXT_BUDGET_CHARS:
            return True
        low = CONTEXT_BUDGET_CHARS - CONTEXT_SLACK_CHARS
        while size() > low and len(full_results) > 1:
            index, call_id, compact = full_results.pop(0)
            run[index] = tool_result(call_id, compact)
        if size() > CONTEXT_BUDGET_CHARS:
            replay[:] = _replay_window(replay, low - _chars([latest, *run]))
        return size() <= CONTEXT_BUDGET_CHARS

    def done(reason: str, message: str = "") -> AssistantEvent:
        """The terminal event; an early end names the confirmed changes, so it still reports the work already committed."""
        if committed and reason != "reply":
            message = f"{message} Already done: {'; '.join(committed)}".strip()
        return {"type": "done", "reason": reason, "message": message}

    def result(call_id: str, name: str, summary: str, output: ToolOutput | None = None) -> AssistantEvent:
        ids = dict(output.ids) if output else {}
        return {"type": "tool_result", "call_id": call_id, "tool": name, "ok": bool(output), "summary": summary, "ids": ids}

    async def ask(forced: str | None) -> tuple[CompletionMessage, str, dict[str, Any]]:
        """One forced call, its tool and its arguments, retried once when the reply is cut off or unusable.

        *forced* ``None`` is the single-call protocol's step: one call to any tool, or to ``reply``.
        """
        nonlocal model_calls
        problem = note = ""
        for attempt in range(2):
            if client.is_aborted:
                raise _Stopped
            if model_calls >= MAX_MODEL_CALLS:
                raise _Limit
            model_calls += 1
            trailing: list[Mapping[str, Any]] = [latest, *run]
            if note:
                # The retry says what was wrong on the message it answers, the request or a tool result: no call from the
                # refused reply is replayed, so every call still has its one result and the prefix before it is unchanged.
                trailing[-1] = {**trailing[-1], "content": f"{trailing[-1]['content']}\n\n{note}"}
            offered = (
                schemas
                if forced is None or shared_tools
                else [step if forced == STEP_TOOL_NAME else tool_schema(by_name[forced])]
            )
            response: CompletionMessage = {}
            # The base ends after the replayed history: a provider that caches only at marked positions then finds the
            # previous request's history under this one's, where the latest request and its run never repeat.
            async for event in CachedBase(prefix=(system, *replay), tools=tuple(offered), model=model).complete(
                client,
                label=f"assistant:{forced or 'step'}",
                trailing=trailing,
                tool_choice={"type": "function", "function": {"name": forced}} if forced else "required",
                # An endpoint that answers a forced choice as ``auto`` may otherwise reply with several calls at once.
                single_tool_call=True,
                **samplers,
                **thinking,
            ):
                if event["type"] == "done":
                    response = event.get("message") or {}
            if client.is_aborted:
                raise _Stopped
            note = ""
            # A reply cut at the budget can decode to arguments that look complete while missing their tail.
            if response.get("finish_reason") == "length":
                problem = agent_lane_cut_off(settings)
            else:
                # Only arguments that arrived as one whole object: a salvaged or degraded ``{}`` would run as another call,
                # its optional fields filled with defaults that name a different target.
                if forced is None:
                    picked = _any_call(response, by_name)
                    if picked is not None:
                        return response, *picked
                    arguments = None
                else:
                    arguments = strict_tool_arguments(response, forced)
                    if arguments is not None and (forced != STEP_TOOL_NAME or _step_choice(arguments, by_name)):
                        return response, forced, arguments
                if arguments is None and _several_calls(response):
                    problem, note = _SEVERAL, _one_at_a_time(forced)
                else:
                    problem = _UNREADABLE
            logger.warning("Assistant: %s reply unusable on attempt %d: %s", forced or "step", attempt + 1, problem)
        raise AssistantUnavailable(f"{problem} The assistant stopped after one retry.")

    try:
        for step_number in itertools.count(1):
            yield {"type": "progress", "label": "Thinking…"}
            if single_call:
                response, name, arguments = await ask(None)
                message = str(arguments.get("message") or "").strip() if name == REPLY else ""
            else:
                response, _, chosen = await ask(STEP_TOOL_NAME)
                name, message = _step_choice(chosen, by_name) or (REPLY, "")
                arguments = {}
            call_id = f"call{step_number}" if single_call else f"step{step_number}"
            if single_call:
                called = assistant_tool_call(response, name, arguments, call_id)
            else:
                called = assistant_tool_call(response, STEP_TOOL_NAME, {"tool": name, "message": message}, call_id)
            if name == REPLY:
                if keep is not None:
                    # A proposal it made is settled by Apply or Cancel after the run; the next request's note says which
                    # are still pending, and the commit check says whether any was applied.
                    keep(Continuation((*replay, latest, *run, called, tool_result(call_id, _DELIVERED)), fingerprint))
                yield {"type": "assistant_message", "text": message, "final": True}
                yield done("reply")
                return
            # Once the tool runs are spent the model still gets this one step, to answer from what it has.
            if executions >= MAX_TOOL_EXECUTIONS:
                raise _Limit
            tool = by_name[name]
            run.append(called)
            if message:
                yield {"type": "assistant_message", "text": message, "final": False}
            if tool.takes_arguments and not single_call:
                run.append(tool_result(call_id, f"Call {name} with its arguments now."))
                yield {"type": "progress", "label": f"Preparing {name}…"}
                response, _, arguments = await ask(name)
                call_id = f"call{step_number}"
                run.append(assistant_tool_call(response, name, arguments, call_id))
            this_call = f"{name}:{json.dumps(arguments, sort_keys=True)}"
            same_calls = same_calls + 1 if this_call == last_call else 1
            last_call = this_call
            if same_calls >= MAX_SAME_CALLS:
                raise _Repeating
            checked = _validated(tool, arguments)
            write = tool.behavior == "write"
            # An idempotent write names an explicit state, so repeating it is harmless and may be what the user asked for. An
            # identical edit proposed again would stack onto the first, or be refused as a no-op over and over.
            guarded = (write and not tool.idempotent) or tool.behavior == "confirm"
            key = f"{name}:{checked.model_dump_json()}" if guarded and not isinstance(checked, str) else ""
            admitted = write and not isinstance(checked, str) and key not in made
            # A Stop that arrived since the arguments came back admits no write.
            if admitted and client.is_aborted:
                raise _Stopped
            yield {"type": "tool_start", "call_id": call_id, "tool": name}
            # The stream sent ``tool_start`` while this generator waited, and a Stop or a disconnect may have landed then. Check
            # again with no await between the check and the write's start, so a write can only start before the Stop.
            if admitted and client.is_aborted:
                yield result(call_id, name, _NOT_RUN)
                raise _Stopped
            output: ToolOutput | None = None
            failure = ""
            if isinstance(checked, str):
                failure = f"{checked} {_CORRECT}"
            elif key in made:
                failure = _repeated(made[key]) if write else _reproposed(made[key])
            else:
                executions += 1
                try:
                    # Once admitted, a write settles whole, even through a Stop or a dropped connection.
                    running = settled(tool.run(checked)) if write else tool.run(checked)
                    if progress is None or not tool.reports_progress:
                        output = await running
                    else:
                        while not progress.empty():
                            progress.get_nowait()
                        task = asyncio.ensure_future(running)
                        async with contextlib.aclosing(_labels(task, progress)) as labels:
                            async for label in labels:
                                yield {"type": "progress", "label": label[:_PROGRESS_LABEL_CHARS]}
                        output = task.result()
                except ToolFailure as exc:
                    failure = str(exc)
            last = f"\n\n{_LAST_RUN}" if executions >= MAX_TOOL_EXECUTIONS else ""
            if output is None:
                run.append(tool_result(call_id, f"{_encode({'error': failure})}{last}"))
                yield result(call_id, name, failure)
            else:
                if key:
                    made[key] = output
                if write and output.committed:
                    # Only a change counts as done; a write that found its state already set ("was already on") did not.
                    committed.append(output.summary)
                    resources = [dict(resource) for resource in output.committed]
                    yield {"type": "mutation", "call_id": call_id, "tool": name, "resources": resources}
                dropped = {
                    "write": "it already happened.",
                    "confirm": "the edit is still proposed; do not propose it again.",
                }.get(tool.behavior, "run the tool again.")
                compact = {
                    "summary": output.summary,
                    "ids": dict(output.ids),
                    "note": f"Full result dropped to save space; {dropped}",
                }
                run.append(tool_result(call_id, f"{_encode(output.data, output.summary)}{last}"))
                full_results.append((len(run) - 1, call_id, json.dumps(compact, ensure_ascii=False)))
                yield result(call_id, name, output.summary, output)
                if tool.behavior == "confirm" and output.proposal is not None:
                    # Nothing was written. The user reviews it once this run ends, and their Apply commits it outside the run.
                    yield {"type": "confirm", "call_id": call_id, "tool": name, "proposal": dict(output.proposal)}
                if tool.behavior == "handoff" and output.handoff is not None:
                    # Nothing ran here. The browser starts the work in the chat's own stream, once this one has settled, and
                    # never after a Stop.
                    if client.is_aborted:
                        raise _Stopped
                    yield {"type": "handoff", "call_id": call_id, "tool": name, "handoff": dict(output.handoff)}
                    yield done("handoff", _HANDED_OFF)
                    return
                if write and output.ends_run:
                    # Every change this request made, since the model gives no answer after this one.
                    yield {"type": "assistant_message", "text": f"{' '.join(committed)} {_CONTEXT_CHANGED}", "final": True}
                    yield done("reply")
                    return
            if not fit():
                yield done("limit", "This request gathered more than the assistant can hold at once. Ask for less at a time.")
                return
    except _Stopped:
        yield done("stopped")
    except _Repeating:
        yield done(
            "limit",
            f"The model made the same {last_call.split(':', 1)[0]} call {MAX_SAME_CALLS} times in a row, so it was stopped.",
        )
    except _Limit:
        spent = f"{MAX_TOOL_EXECUTIONS} tool runs" if executions >= MAX_TOOL_EXECUTIONS else f"{MAX_MODEL_CALLS} model calls"
        yield done("limit", f"The assistant reached its limit of {spent} for one request.")
