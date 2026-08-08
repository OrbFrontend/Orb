"""One description of a failed turn, shaped for the wire and for a human.

``backend/workflows/errors.py`` names the principle: *"Hiding the first behind
'see server logs' is the difference between a user who fixes it in the settings
panel and a user who files a bug."* The chat pipeline is the generation path that
never adopted it -- every exception became the constant string
``"Generation failed; see server logs"``. :func:`describe_failure` is the
adoption.

It lives in ``pipeline/`` (L2) rather than ``inference/`` (L4) because it has to
classify both an ``httpx`` transport error (L4's dependency) and a
``WorkflowUserFacingError`` (L3), and L2 is the lowest layer allowed to see both.

**Classification keys on the status class only.** No vocabulary matching against
provider prose: a marker list over words no two providers share is wrong often
enough that it replaces an actionable message with a confident wrong one
(``workflows/image_gen/engine/openai_image_client.py:6-14`` argues the same trade
at length). The headline says what Orb can be sure of; ``sentence`` carries what
the provider actually said, unedited apart from the credential.

``kind`` distinguishes a misconfiguration from a defect. ``"internal"`` means an
exception nobody classified -- a bug, not something the user did -- so it ships no
``body`` and does not pretend otherwise. It is still strictly more than "see
server logs".
"""

from __future__ import annotations

from typing import Any

import httpx

from ..inference import LLMCallError, provider_sentence
from ..workflows.errors import WorkflowUserFacingError

# Cap on an unclassified exception's repr. The full traceback is in the log; this
# is the line that reaches a chat bubble.
INTERNAL_SENTENCE_LIMIT = 300

# Same cap the transport applies, for the branch that reads a body itself.
BODY_LIMIT = 20_000

# Which pass raised, written onto the exception by the orchestrator. An attribute
# rather than a parameter because the failure travels from inside a pass generator
# to ``entrypoints._run_turn_handler`` with no shared object between them, and the
# alternatives are worse: ``turn_scratch`` is part of the public workflow-hook
# surface (``workflows/contracts.py``), and ``PipelineContext`` is frozen and not
# passed to ``_run_pipeline`` at all.
_STAGE_ATTR = "_orb_stage"

STAGE_DIRECTOR = "director pass"
STAGE_WRITER = "writer pass"
STAGE_EDITOR = "editor pass"
STAGE_WORKFLOWS = "workflow hook"


def mark_stage(exc: BaseException, stage: str) -> None:
    """Record that *exc* escaped *stage*, unless an inner stage already claimed it.

    First writer wins, and unwinding runs innermost-first, so a nested stage keeps
    the more specific label.
    """
    try:
        if not getattr(exc, _STAGE_ATTR, ""):
            setattr(exc, _STAGE_ATTR, stage)
    except AttributeError:
        # Every exception carries a __dict__ (BaseException grants one even under
        # __slots__), so this only covers an exotic type with a __setattr__ that
        # refuses. The stage is a nicety; never let labelling mask the failure.
        pass


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

    ``HTTPError.request`` raises ``RuntimeError`` when the exception was built
    without one -- which a hand-rolled transport error in a test is -- so this is
    never read bare.
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


def describe_failure(exc: BaseException) -> dict[str, Any]:
    """Turn *exc* into the ``error`` event's data payload.

    Keys: ``headline`` (always), ``sentence`` (always, possibly empty), ``kind``
    (always), ``stage`` (always, ``""`` when no pass claimed it), and
    ``status``/``host``/``model``/``body`` when the failure is one the transport
    could attribute. A consumer renders ``headline`` big, ``sentence`` small, and
    hides ``body`` behind a disclosure.
    """
    stage = stage_of(exc)

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
        # A status failure raised somewhere the typed error is not built (a bare
        # raise_for_status on a non-streaming call). The response is still right
        # here, so read it rather than printing httpx's canned "Client error '400
        # Bad Request' for url ..." -- the exact line ``inference/errors.py`` was
        # written to stop showing people. Falls back to the repr when the body is
        # unreadable or says nothing.
        #
        # Unredacted, unlike the LLMCallError branch: the credential is not in
        # scope here. Acceptable because this branch covers Orb's own internal
        # calls, whose bodies do not echo an Authorization header -- but it is the
        # reason a key must never be routed through a bare raise_for_status.
        body = _body_of(exc)
        payload = {
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
        # Left unwrapped at the transport seam on purpose so RetryPolicy's
        # isinstance check over RETRYABLE_TRANSPORT_ERRORS still fires; this is
        # where the classification it skipped happens instead.
        return {
            "headline": TRANSPORT_HEADLINE,
            "sentence": _internal_sentence(exc),
            "host": _host_of(exc),
            "kind": "transport",
            "stage": stage,
        }

    if isinstance(exc, WorkflowUserFacingError):
        # The message is already sanitized -- that is what raising this type promises.
        return {
            "headline": WORKFLOW_HEADLINE,
            "sentence": str(exc)[:INTERNAL_SENTENCE_LIMIT],
            "kind": "workflow",
            "stage": stage,
        }

    return {
        "headline": INTERNAL_HEADLINE,
        "sentence": _internal_sentence(exc),
        "kind": "internal",
        "stage": stage,
    }
