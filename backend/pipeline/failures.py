"""Classify turn failures into user-facing responses."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, TypeVar

import httpx

from ..inference import AbortToken, DecisionTransportError, EndpointConfigError, LLMCallError, provider_sentence
from ..inference.claude_code import ClaudeCodeError
from ..inference.errors import BODY_LIMIT
from ..workflows.errors import WorkflowUserFacingError
from .events import FailureData, PublicTurnEvent, WarningEvent

# Cap on an unclassified exception's repr. The full traceback is in the log; this is the line that reaches a chat bubble.
INTERNAL_SENTENCE_LIMIT = 300

# Which pass raised, written onto the exception by ``staged``. An attribute rather than a parameter because the failure travels
# from inside a pass generator to ``entrypoints._run_turn_handler`` with no shared object between them, and the alternatives are
# worse: ``turn_scratch`` is part of the public workflow-hook surface (``workflows/contracts.py``), and ``PipelineContext`` is
# frozen and not passed to ``run_pipeline`` at all.
_STAGE_ATTR = "_orb_stage"

STAGE_JUDGE = "judge pass"
STAGE_DIRECTOR = "director pass"
STAGE_WRITER = "writer pass"
STAGE_EDITOR = "editor pass"
STAGE_WORKFLOWS = "workflow hook"
# The steps that read the finished reply: the after-reply state update, World proposals and sheet reviews.
STAGE_AFTER_REPLY = "after-reply update"
# Saving the reply is not a pass, but a failure there is the one a stopped turn
# must still report: the user was told nothing else about losing the reply.
STAGE_SAVE = "saving the reply"


def mark_stage(exc: BaseException, stage: str) -> None:
    """Record that *exc* escaped *stage*, unless an inner stage already claimed it.

    First writer wins, and unwinding runs innermost-first, so a nested stage keeps the more specific label.
    """
    try:
        if not getattr(exc, _STAGE_ATTR, ""):
            setattr(exc, _STAGE_ATTR, stage)
    except AttributeError:
        # Every exception carries a __dict__ (BaseException grants one even under __slots__), so this only covers an exotic type
        # with a __setattr__ that refuses. The stage is a nicety; never let labelling mask the failure.
        pass


_Ev = TypeVar("_Ev")


async def staged(stage: str, gen: AsyncIterator[_Ev]) -> AsyncIterator[_Ev]:
    """Pass events through and label uncategorized failures."""
    try:
        async for ev in gen:
            yield ev
    except Exception as e:
        mark_stage(e, stage)
        raise


def stage_of(exc: BaseException) -> str:
    """The pass *exc* escaped, or ``""`` when nothing claimed it."""
    got = getattr(exc, _STAGE_ATTR, "")
    return got if isinstance(got, str) else ""


_STATUS_HEADLINES: tuple[tuple[frozenset[int], str], ...] = (
    (frozenset({400, 422}), "The model provider rejected the request."),
    (frozenset({401, 403}), "The endpoint rejected Orb's credentials."),
    (frozenset({404}), "The endpoint or model was not found."),
    (frozenset({408, 504}), "The provider timed out."),
    (frozenset({429}), "Rate limited, or out of credits."),
)

TRANSPORT_HEADLINE = "Couldn't reach the endpoint."
SERVER_HEADLINE = "The provider had an internal error."
GENERIC_HTTP_HEADLINE = "The model provider rejected the request."
WORKFLOW_HEADLINE = "A workflow step failed."
CONFIG_HEADLINE = "The model settings are incomplete."
INTERNAL_HEADLINE = "Something went wrong inside Orb."


def headline_for_status(status: int) -> str:
    """The one sentence Orb can assert from a status code alone."""
    for codes, text in _STATUS_HEADLINES:
        if status in codes:
            return text
    if 500 <= status < 600:
        return SERVER_HEADLINE
    return GENERIC_HTTP_HEADLINE


def _internal_sentence(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:INTERNAL_SENTENCE_LIMIT]


def _host_of(exc: httpx.HTTPError) -> str:
    """The ``host:port`` the request was aimed at, or ``""``.

    ``HTTPError.request`` raises ``RuntimeError`` when the exception was built without one -- which a hand-rolled transport
    error in a test is -- so this is never read bare.
    """
    try:
        return exc.request.url.netloc.decode("ascii", "replace")
    except (RuntimeError, AttributeError):
        return ""


def _body_of(exc: httpx.HTTPStatusError) -> str:
    """The response text, or ``""`` when it cannot be read.

    ``.text`` raises ``httpx.ResponseNotRead`` (a ``RuntimeError``) on a streaming
    response nobody drained, so it is never read bare.
    """
    try:
        return exc.response.text or ""
    except (RuntimeError, AttributeError):
        return ""


def describe_failure(exc: BaseException) -> FailureData:
    """Turn *exc* into the ``error`` event's data payload.

    Keys: ``headline`` (always), ``sentence`` (always, possibly empty), ``kind`` (always), ``stage`` (always, ``""`` when no
    pass claimed it), and ``status``/``host``/``model``/``body`` when the failure is one the transport could attribute. A
    consumer renders ``headline`` big, ``sentence`` small, and hides ``body`` behind a disclosure.
    """
    stage = stage_of(exc)

    if isinstance(exc, ClaudeCodeError):
        return {
            "headline": "Claude Code CLI failed.",
            "sentence": str(exc)[:INTERNAL_SENTENCE_LIMIT],
            "kind": "provider",
            "stage": stage,
        }

    if isinstance(exc, LLMCallError):
        return {
            "headline": headline_for_status(exc.response.status_code),
            "sentence": exc.sentence,
            "status": exc.response.status_code,
            "host": exc.host,
            "model": exc.model,
            "body": exc.body,
            "kind": "provider",
            "stage": stage,
        }

    if isinstance(exc, httpx.HTTPStatusError):
        # Read response details for bare status failures; fall back to repr if empty. This branch has no credential to redact
        # and is for internal calls only: never route a credential-bearing provider error through bare raise_for_status.
        body = _body_of(exc)
        payload: FailureData = {
            "headline": headline_for_status(exc.response.status_code),
            "sentence": provider_sentence(body) or _internal_sentence(exc),
            "status": exc.response.status_code,
            "host": _host_of(exc),
            "model": "",
            "kind": "provider",
            "stage": stage,
        }
        # Omitted rather than sent empty: the Details pane distinguishes "no body"
        # from "a body that said nothing", and an empty string is not a body.
        if body:
            payload["body"] = body[:BODY_LIMIT]
        return payload

    if isinstance(exc, httpx.TransportError):
        # Left unwrapped at the transport seam on purpose so RetryPolicy's isinstance check over RETRYABLE_TRANSPORT_ERRORS
        # still fires; this is where the classification it skipped happens instead.
        return {
            "headline": TRANSPORT_HEADLINE,
            "sentence": _internal_sentence(exc),
            "host": _host_of(exc),
            "kind": "transport",
            "stage": stage,
        }

    if isinstance(exc, (DecisionTransportError, httpx.HTTPError)):
        return {
            "headline": "The endpoint returned an invalid response.",
            "sentence": _internal_sentence(exc),
            "kind": "provider",
            "stage": stage,
        }

    if isinstance(exc, EndpointConfigError):
        # The message names the setting to change; it carries nothing from a provider.
        return {"headline": CONFIG_HEADLINE, "sentence": str(exc), "kind": "config", "stage": stage}

    if isinstance(exc, WorkflowUserFacingError):
        # The message is already sanitized -- that is what raising this type promises.
        return {
            "headline": WORKFLOW_HEADLINE,
            "sentence": str(exc)[:INTERNAL_SENTENCE_LIMIT],
            "kind": "workflow",
            "stage": stage,
        }

    return {"headline": INTERNAL_HEADLINE, "sentence": _internal_sentence(exc), "kind": "internal", "stage": stage}


# The ``warning`` headline for each step a failed call can leave unfinished, named as the status line names the step
# (frontend/generation_status.js).
_STEP_HEADLINES = {
    "director": "The Director didn't finish.",
    "lorebook": "The lorebook selection didn't finish.",
    "state": "The state update didn't finish.",
    "output_auditor": "The draft audit didn't finish.",
    "length_guard": "The length check didn't finish.",
    "subject_fixation": "The subject fixation edit didn't finish.",
    "post_processing": "Post-processing “{label}” didn't finish.",
    "feedback": "Feedback didn't finish.",
    "editor": "The Editor didn't finish.",
    "world_changes": "The World change check didn't finish.",
    "sheet_updates": "The character sheet review didn't finish.",
}


def step_failure_warning(error: BaseException, step: str, *, stage: str, label: str = "") -> WarningEvent:
    """The non-terminal ``warning`` for a failure its step survived.

    The headline names the step; the failure's own account becomes the sentence, so a timeout still reads as a timeout.
    """
    payload = describe_failure(error)
    reason = " ".join(part for part in (payload["headline"], payload["sentence"]) if part)
    payload["headline"] = _STEP_HEADLINES[step].format(label=label or "fragment")
    payload["sentence"] = reason
    payload["stage"] = stage
    return {"event": "warning", "data": payload}


def _warning_cause(data: Mapping[str, Any]) -> tuple:
    """What a warning reports as having gone wrong. A provider or transport failure is its host and status -- their bodies can
    carry per-request ids -- and anything else is its sentence."""
    if data.get("host"):
        return (data.get("kind"), data["host"], data.get("status"))
    return (data.get("kind"), data.get("sentence"))


async def reported_once(events: AsyncIterator[PublicTurnEvent], abort: AbortToken | None) -> AsyncIterator[PublicTurnEvent]:
    """Pass one turn's SSE events through, thinning its ``warning`` events.

    The rule every step follows: a failed call ends its own step, never the turn -- only the Writer's failure is the turn's
    ``error`` -- and the step says so with a ``warning``. One outage fails every Agent call of a turn, so a warning whose step or
    whose cause this stream already reported is dropped (the log keeps each failure), and none is sent once the turn is
    stopped, which explains itself.
    """
    steps: set[str] = set()
    causes: set[tuple] = set()
    async for event in events:
        if event.get("event") == "warning":
            data = event.get("data")
            if abort is not None and abort.is_aborted:
                continue
            if isinstance(data, Mapping):
                step, cause = str(data.get("headline", "")), _warning_cause(data)
                if step in steps or cause in causes:
                    continue
                steps.add(step)
                causes.add(cause)
        yield event
