"""Run one assistant request: choose a tool, ask for its arguments, execute, repeat until a reply."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
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
    replay_reasoning,
    strict_tool_arguments,
    tool_result,
    without_reasoning,
)
from .contracts import (
    CONTEXT_BUDGET_CHARS,
    CONTEXT_SLACK_CHARS,
    IMAGE_CONTEXT_CHARS,
    MAX_MODEL_CALLS,
    MAX_RESULT_CHARS,
    MAX_SAME_CALLS,
    MAX_TOOL_EXECUTIONS,
    REPLY,
    STEP_TOOL_NAME,
    AssistantEvent,
    AssistantTool,
    AssistantUnavailable,
    RequestMark,
    ToolArguments,
    ToolFailure,
    ToolOutput,
    Transcript,
    ViewContext,
    encode_result,
    is_request_message,
    tool_pictures,
)
from .prompt import reply_tool, request_message, step_tool, system_prompt, tool_schema

logger = logging.getLogger(__name__)

_UNREADABLE = "The model's reply was not a readable step."
_SEVERAL = "The model's reply made several calls at once, where one was asked for."
_CORRECT = "Nothing ran. Choose the tool again and correct its arguments."
_LAST_RUN = f"That was the last tool run for this request. Choose {REPLY} now and answer from what you have."
_NOT_RUN = "Not run: the request was stopped before this change started. Nothing was changed."
_HANDED_OFF = "Handed to the chat, which starts the work there if it is still open."
_CONTEXT_CHANGED = "This request ends here, since what it would read next has changed; ask again to go on from there."
_DELIVERED = "Shown to the user."
_ANSWER_NOW = "Write your complete final answer to the user now, as plain text."
_NO_ANSWER = "The model's reply held no answer."
# What the conversation keeps of a request that ended without an answer, for the requests after it.
_NO_RESULT = "No result was received: the request ended while this ran. It may have happened; check before running it again."
_STOPPED = "The user stopped this request before it finished."
_FAILED = "This request ended early and was not answered."

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
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(part.get("text", "")) if part.get("type") == "text" else IMAGE_CONTEXT_CHARS for part in content)
        total += sum(len(call["function"]["arguments"]) for call in message.get("tool_calls") or ())
    return total


def _text_only_messages(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The messages as a text-only transport takes them: each picture's caption, and that the model was not shown it.

    The transcript itself keeps the pictures, so a transport that takes them later is sent them.
    """
    projected: list[Mapping[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(part["text"] for part in content if part.get("type") == "text")
            if any(part.get("type") == "image_url" for part in content):
                text += "\nThe user can see this picture. You cannot inspect it: the current transport accepts text only."
            projected.append({**message, "content": text})
        else:
            projected.append(message)
    return projected


def _newest_requests(history: Sequence[WireMessage], room: int) -> list[WireMessage]:
    """The newest part of *history* that fits *room* characters, starting at a request."""
    over = _chars(history) - room
    passed = 0
    for index, message in enumerate(history):
        if is_request_message(message) and passed >= over:
            return list(history[index:])
        passed += _chars([message])
    return []


def _closed(messages: list[WireMessage], note: str) -> list[WireMessage]:
    """*messages* as the next request can extend them: every call answered, and *note* saying how an unanswered request ended."""
    last: Mapping[str, Any] = messages[-1]
    if calls := last.get("tool_calls"):
        messages.append(tool_result(calls[0]["id"], _NO_RESULT))
    if note:
        messages.append({"role": "assistant", "content": note})
    return messages


def _step_numbers(history: Sequence[Mapping[str, Any]]) -> Iterator[int]:
    """Step numbers whose ``step``/``call`` ids no call in *history* holds: some providers refuse a repeated tool call id."""
    taken = {call.get("id") for message in history for call in message.get("tool_calls") or ()}
    return (number for number in itertools.count(1) if f"step{number}" not in taken and f"call{number}" not in taken)


def _repeated(output: ToolOutput) -> str:
    """The refusal for a change this request already made: true about what exists, so the model cannot claim a second one."""
    ids = encode_result(dict(output.ids))
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
    content = encode_result(data)
    if len(content) <= MAX_RESULT_CHARS:
        return content
    # A tool bounds its own result; this only stops a mistake there from spending the whole context. It stays valid JSON, and
    # says so instead of cutting a value mid-way.
    logger.warning("Assistant result of %d characters passed the %d cap", len(content), MAX_RESULT_CHARS)
    return encode_result(
        {
            "error": f"The result was {len(content)} characters, over the {MAX_RESULT_CHARS} limit. Narrow the request.",
            "summary": summary[:500],
        }
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


def _step_choice(
    arguments: Mapping[str, Any], names: Mapping[str, Any], *, plain_answer: bool = False
) -> tuple[str, str] | None:
    """The step's ``(tool, message)``, or ``None`` when it is not exactly that pair of usable strings.

    A ``reply`` carries its answer in ``message`` unless *plain_answer*, where the answer is the call after it.
    """
    tool, message = arguments.get("tool"), arguments.get("message")
    if set(arguments) != {"tool", "message"} or not isinstance(tool, str) or not isinstance(message, str):
        return None
    if tool not in names and tool != REPLY:
        return None
    message = message.strip()
    return (tool, message) if message or tool != REPLY or plain_answer else None


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
    request: str,
    view: ViewContext,
    transcript: Transcript | None = None,
    progress: asyncio.Queue[str] | None = None,
    single_call: bool | None = None,
    keep: Callable[[Transcript], None] | None = None,
    reasoning: bool = False,
    note: str = "",
    request_id: str = "",
) -> AsyncIterator[AssistantEvent]:
    """Answer *request*, yielding progress and exactly one ``done`` unless the model or provider fails.

    *transcript* is the conversation so far, and the run extends it: the request, then each call and its whole result, in
    order, as a coding agent's session does. Nothing in it is shortened or summarized, so the model never has to read again
    what it has already been shown, and the server's prefix cache holds through all of it. An unusable reply is retried
    against the same prefix, told to make one call when it made several, and every call has its own id and one matching
    result. However the run ends, *keep* is handed the conversation with this request in it: a call cut off before its
    result is answered as unknown, and a request that got no answer is closed with a line saying how it ended.

    Only a conversation past ``CONTEXT_BUDGET_CHARS`` loses anything: its oldest requests, whole, a quarter of the budget at
    a time. A single request that cannot fit ends with ``limit``.

    The same call made ``MAX_SAME_CALLS`` times in a row ends the run before the last of them executes.

    A write never runs on a retried or refused reply. Once it starts it settles whole, a ``mutation`` follows its commit, the
    same change is not made twice in one request, nor the same edit proposed twice, and an early ``done`` names the changes
    already committed. A confirm tool's proposal becomes a ``confirm`` event and the run carries on. *progress* is where a
    tool that reports progress puts its labels; each one becomes a ``progress`` event while that tool runs.

    *single_call* makes each step one call over every tool's schema plus ``reply``, in place of a step call followed by an
    argument call. Left ``None``, the run takes it wherever the endpoint sends native tool schemas and honours forced tool
    choice, and two calls a step elsewhere; the assistant benchmark (scripts/bench/assistant_bench.py) measures both.

    *note* is the caller's word about state the request should know of, such as changes earlier requests proposed that still
    wait for the user; it follows the view note. *request_id* is the browser's name for this request, kept with what its
    writes committed, so a later edit of the request can find where it began.

    *reasoning* turns thinking on for every call of the run. Its reasoning replays under the names it streamed, and the template
    is asked to keep it on earlier turns, so a later request renders what this run read. A run with thinking off sends no
    reasoning back, the conversation's earlier reasoning included.
    """
    by_name = {tool.name: tool for tool in tools}
    latest = request_message(request, view, note=note)
    endpoint = str(getattr(client, "base_url", ""))
    # Restrict the tool list when forced choice is unreliable.
    thinking = reasoning_cfg(reasoning, preserve=True)
    shared_tools = honors_forced_tool_choice(endpoint, model, thinking)
    native_tools = client.sends_tool_schemas([latest], model)
    if single_call is None:
        # Decided once for the run, so every call in it shares one tool blob. Structured output and text completion send no
        # schemas to choose among, and an endpoint that ignores forced choice may ignore ``required`` too.
        single_call = shared_tools and native_tools
    # A JSON grammar ends a string at the first bare quote, so a grammar-decoded step cannot carry the answer.
    plain_answer = not single_call and not native_tools
    step = step_tool(tools, plain_answer=plain_answer)
    if single_call:
        schemas = [reply_tool(), *(tool_schema(tool, described=True) for tool in tools)]
    else:
        schemas = [step, *(tool_schema(tool) for tool in tools if tool.takes_arguments)]
    # The same call path and samplers as the pipeline's agent passes: the lane's own hyperparameters, its max_tokens included,
    # with the system prompt and the conversation so far as the cached prefix every call of the run shares.
    samplers = extract_hyperparams(settings, lane="agent")

    system: ChatMessage = {
        "role": "system",
        "content": system_prompt(tools, single_call=single_call, plain_answer=plain_answer),
    }
    history: list[WireMessage] = []
    if transcript is not None:
        own = reasoning and (transcript.model, transcript.endpoint) == (model, endpoint)
        history = [*transcript.messages] if own else [without_reasoning(message) for message in transcript.messages]
    if _chars([latest]) > CONTEXT_BUDGET_CHARS:
        raise AssistantUnavailable("That request is too long for the assistant. Shorten it and send it again.")
    run: list[WireMessage] = []
    model_calls = executions = 0
    # What this run's writes committed: one line each for an early ``done``, and each exact call so it is never made twice.
    committed: list[str] = []
    made: dict[str, ToolOutput] = {}
    last_call, same_calls = "", 0

    def fit() -> bool:
        """Whether the conversation fits its budget, once its oldest requests have gone if it did not. This request stays."""
        request_chars = _chars([latest, *run])
        if _chars(history) + request_chars > CONTEXT_BUDGET_CHARS:
            history[:] = _newest_requests(history, CONTEXT_BUDGET_CHARS - CONTEXT_SLACK_CHARS - request_chars)
        return _chars(history) + request_chars <= CONTEXT_BUDGET_CHARS

    def done(reason: str, message: str = "") -> AssistantEvent:
        """The terminal event; an early end names the confirmed changes, so it still reports the work already committed."""
        if committed and reason != "reply":
            message = f"{message} Already done: {'; '.join(committed)}".strip()
        return {"type": "done", "reason": reason, "message": message}

    def result(call_id: str, name: str, summary: str, output: ToolOutput | None = None) -> AssistantEvent:
        ids = dict(output.ids) if output else {}
        return {"type": "tool_result", "call_id": call_id, "tool": name, "ok": bool(output), "summary": summary, "ids": ids}

    async def issue(label: str, offered: Sequence[dict[str, Any]], tool_choice: dict | str, note: str) -> CompletionMessage:
        """One model call over the run so far; *note* joins the message it answers."""
        nonlocal model_calls
        if client.is_aborted:
            raise _Stopped
        if model_calls >= MAX_MODEL_CALLS:
            raise _Limit
        model_calls += 1
        trailing: list[Mapping[str, Any]] = [latest, *run]
        if note:
            # The retry says what was wrong on the message it answers, the request or a tool result: no call from the
            # refused reply is replayed, so every call still has its one result and the prefix before it is unchanged.
            content = trailing[-1]["content"]
            amended = f"{content}\n\n{note}" if isinstance(content, str) else [*content, {"type": "text", "text": note}]
            trailing[-1] = {**trailing[-1], "content": amended}
        response: CompletionMessage = {}
        # The base ends where the previous request did: a provider that caches only at marked positions then finds that
        # request's conversation under this one's.
        async for event in CachedBase(
            prefix=(system, *history),
            tools=tuple(offered),
            model=model,
            resolve=None if client.supports_image_inputs else _text_only_messages,
        ).complete(
            client,
            label=f"assistant:{label}",
            trailing=trailing,
            tool_choice=tool_choice,
            # An endpoint that answers a forced choice as ``auto`` may otherwise reply with several calls at once.
            single_tool_call=True,
            **samplers,
            **thinking,
        ):
            if event["type"] == "done":
                response = event.get("message") or {}
        if client.is_aborted:
            raise _Stopped
        return response

    async def ask(forced: str | None) -> tuple[CompletionMessage, str, dict[str, Any]]:
        """One forced call, its tool and its arguments, retried once when the reply is cut off or unusable.

        *forced* ``None`` is the single-call protocol's step: one call to any tool, or to ``reply``.
        """
        problem = note = ""
        for attempt in range(2):
            offered = (
                schemas
                if forced is None or shared_tools
                else [step if forced == STEP_TOOL_NAME else tool_schema(by_name[forced])]
            )
            choice = {"type": "function", "function": {"name": forced}} if forced else "required"
            response = await issue(forced or "step", offered, choice, note)
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
                    if arguments is not None and (
                        forced != STEP_TOOL_NAME or _step_choice(arguments, by_name, plain_answer=plain_answer)
                    ):
                        return response, forced, arguments
                if arguments is None and _several_calls(response):
                    problem, note = _SEVERAL, _one_at_a_time(forced)
                else:
                    problem = _UNREADABLE
            logger.warning("Assistant: %s reply unusable on attempt %d: %s", forced or "step", attempt + 1, problem)
        raise AssistantUnavailable(f"{problem} The assistant stopped after one retry.")

    async def answer() -> tuple[CompletionMessage, str]:
        """The final answer as plain text, retried once when it is cut off or empty."""
        problem = ""
        for attempt in range(2):
            response = await issue("answer", schemas, "none", "")
            text = str(response.get("content") or "").strip()
            if response.get("finish_reason") == "length":
                problem = agent_lane_cut_off(settings)
            elif text:
                return response, text
            else:
                problem = _NO_ANSWER
            logger.warning("Assistant: answer unusable on attempt %d: %s", attempt + 1, problem)
        raise AssistantUnavailable(f"{problem} The assistant stopped after one retry.")

    # How a request that got no answer ended, for the conversation to keep; a reply or a handoff needs no such line.
    ending = _FAILED
    fit()
    try:
        for step_number in _step_numbers(history):
            yield {"type": "progress", "label": "Thinking…"}
            if single_call:
                response, name, arguments = await ask(None)
                message = str(arguments.get("message") or "").strip() if name == REPLY else ""
                call_id = f"call{step_number}"
                called = assistant_tool_call(response, name, arguments, call_id)
            else:
                response, _, chosen = await ask(STEP_TOOL_NAME)
                name, message = _step_choice(chosen, by_name, plain_answer=plain_answer) or (REPLY, "")
                arguments = {}
                call_id = f"step{step_number}"
                called = assistant_tool_call(response, STEP_TOOL_NAME, {"tool": name, "message": message}, call_id)
            if name == REPLY:
                if plain_answer:
                    run.extend([called, tool_result(call_id, _ANSWER_NOW)])
                    yield {"type": "progress", "label": "Writing…"}
                    response, message = await answer()
                    run.append({"role": "assistant", "content": message, **replay_reasoning(response)})
                else:
                    run.extend([called, tool_result(call_id, _DELIVERED)])
                ending = ""
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
                run.append(tool_result(call_id, f"{_encode(output.data, output.summary)}{last}"))
                if output.images:
                    run.append(tool_pictures(name, call_id, output.images))
                yield result(call_id, name, output.summary, output)
                if tool.behavior == "confirm" and output.proposal is not None:
                    # Nothing was written. The user reviews it once this run ends, and their Apply commits it outside the run.
                    yield {"type": "confirm", "call_id": call_id, "tool": name, "proposal": dict(output.proposal)}
                if tool.behavior == "handoff" and output.handoff is not None:
                    # Nothing ran here. The browser starts the work in the chat's own stream, once this one has settled, and
                    # never after a Stop.
                    if client.is_aborted:
                        raise _Stopped
                    ending = ""
                    yield {"type": "handoff", "call_id": call_id, "tool": name, "handoff": dict(output.handoff)}
                    yield done("handoff", _HANDED_OFF)
                    return
                if write and output.ends_run:
                    # Every change this request made, since the model gives no answer after this one.
                    ending = f"{' '.join(committed)} {_CONTEXT_CHANGED}"
                    yield {"type": "assistant_message", "text": ending, "final": True}
                    yield done("reply")
                    return
            if not fit():
                ending = "This request gathered more than the assistant can hold at once. Ask for less at a time."
                yield done("limit", ending)
                return
    except _Stopped:
        ending = _STOPPED
        yield done("stopped")
    except _Repeating:
        ending = (
            f"The model made the same {last_call.split(':', 1)[0]} call {MAX_SAME_CALLS} times in a row, so it was stopped."
        )
        yield done("limit", ending)
    except _Limit:
        spent = f"{MAX_TOOL_EXECUTIONS} tool runs" if executions >= MAX_TOOL_EXECUTIONS else f"{MAX_MODEL_CALLS} model calls"
        ending = f"The assistant reached its limit of {spent} for one request."
        yield done("limit", ending)
    finally:
        if keep is not None:
            # A Stop can also end the run by cancelling it, where no ``_Stopped`` is raised.
            note = _STOPPED if ending == _FAILED and client.is_aborted else ending
            messages = tuple(_closed([*history, latest, *run], note))
            marks = (*(transcript.requests if transcript else ()), RequestMark(request_id, tuple(committed)))
            # Requests the budget dropped take their marks with them.
            held = sum(is_request_message(message) for message in messages)
            keep(Transcript(messages, model, endpoint, marks[-held:]))
