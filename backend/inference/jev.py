"""Transport and strict normalization for the TypeSafe/jev decision gateway.

One request carries one shared ``state`` and any number of independent
questions, which is what makes batching identical inputs free. The response
carries one probability per question.

**The contract this module implements is documented, not yet verified against
the live gateway.** ``docs/plans/decision-fragments.md`` makes that verification
a release gate: the request shape, the REST spelling, question-count and payload
limits, usage fields, and the behavior when one question of several is invalid
all have to be pinned with saved fixtures before the feature ships. Everything
here is therefore written so a wrong guess fails *closed*: an unusable answer is
a failure for its own question, never an implicit ``false``, and the caller's
authored fallback is what reaches the story.

Layer note: this module is ``inference``. It performs the call, normalizes the
answer, and caches validated raw answers. It does not know what a fragment is,
never resolves an outcome, never draws a random number, and imports neither
``pipeline`` nor ``database``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..core import OUTCOME_KEYS
from .client import AbortToken
from .errors import llm_call_error

logger = logging.getLogger(__name__)

#: The adapter's own contract version. It is part of every raw-answer cache key,
#: so changing how a request is serialized or an answer validated invalidates
#: every cached answer produced by the previous behavior rather than silently
#: mixing two contracts inside one TTL window.
DECISION_CONTRACT_VERSION = "jev/1"

#: The model Orb requests by default. Version-specific on purpose: the plan
#: prefers a pinned version over an alias, and the *returned* identifier is
#: recorded separately because an alias can move under us.
DEFAULT_DECISION_MODEL = "typesafe/jev-1.13"

#: Conservative Orb-side limits, to be replaced by the gateway's verified ones.
#: They exist so an oversized input uses the author's fallback with a visible
#: reason instead of being silently truncated -- a truncated state can drop the
#: one fact that decides the answer.
MAX_STATE_BYTES = 16 * 1024
MAX_QUESTION_BYTES = 8 * 1024
MAX_QUESTIONS_PER_REQUEST = 16
MAX_REQUEST_BYTES = 64 * 1024

#: Process-local raw-answer cache bounds (plan: 512 entries, ten minutes).
CACHE_CAPACITY = 512
CACHE_TTL_SECONDS = 600.0


class DecisionCancelled(Exception):
    """The user stopped generation while a decision request was in flight.

    Deliberately not an ``LLMCallError``: a stop is not a provider failure, and
    conflating the two would hand the story a fallback outcome and keep
    generating after the user asked for neither.
    """


class DecisionTransportError(Exception):
    """The gateway answered, but not with something this adapter can read.

    Raised for a non-JSON body or a JSON body with no readable ``answers``
    object. A per-question problem is *not* this: it is reported per question so
    the valid siblings in the same batch still resolve.
    """


def decisions_url(base_url: str) -> str:
    """The alpha decisions route for an OpenRouter-style *base_url*.

    Orb stores endpoints as the chat-completions base (``.../api/v1``), while
    the decisions surface sits beside it at ``.../api/alpha/decisions`` -- which
    is why jev is absent from ``/api/v1/models`` and why its model page 404s.
    Derived rather than stored so a user who edits one endpoint's URL does not
    have to remember a second one, and overridable in settings because this
    spelling is exactly the part the release gate has to confirm.
    """
    parts = urlsplit(base_url.strip().rstrip("/"))
    segments = [segment for segment in parts.path.split("/") if segment]
    while segments and segments[-1] in ("chat", "completions", "v1", "responses"):
        segments.pop()
    path = "/".join([*segments, "alpha", "decisions"])
    return urlunsplit((parts.scheme, parts.netloc, f"/{path}", "", ""))


@dataclass(frozen=True, slots=True)
class NoulQuestion:
    """One yes/no question, already rendered.

    *key* names the question inside its request and its answer; Orb uses the
    fragment id, so a batched response maps back to fragments without a
    positional assumption. ``criteria`` carries both outcome descriptions --
    TypeSafe treats them as optional, Orb requires both so each outcome is
    explicit to the classifier and to the author reading it back.
    """

    key: str
    instructions: str
    criteria: Mapping[str, str]
    question_type: str = "noul"

    def payload(self) -> dict[str, Any]:
        return {
            "type": self.question_type,
            "instructions": self.instructions,
            # Outcome order is part of the canonical form, so it is spelled from
            # OUTCOME_KEYS rather than from the mapping's own iteration order:
            # two definitions that differ only in dict order are the same
            # question and must share a cache entry.
            "criteria": {key: self.criteria[key] for key in OUTCOME_KEYS if key in self.criteria},
        }

    def canonical(self) -> str:
        """The exact bytes that identify this question for caching.

        No whitespace or case normalization: the classifier reads the prose, so
        two spellings of the same idea are two questions, and pretending
        otherwise would serve one's answer for the other.
        """
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"))

    def rendered_bytes(self) -> int:
        return len(self.canonical().encode("utf-8"))


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """One outbound request: a shared state plus independent questions."""

    model: str
    state: str
    questions: tuple[NoulQuestion, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "state": self.state,
            "questions": {question.key: question.payload() for question in self.questions},
        }

    def body(self) -> bytes:
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    """A normalized gateway answer.

    ``answers`` holds only the questions that produced a usable probability.
    ``invalid`` names the rest -- missing, malformed, or out of range -- so the
    caller applies each one's own fallback while its valid siblings resolve
    normally.
    """

    answers: Mapping[str, float]
    invalid: tuple[str, ...] = ()
    returned_model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = ""
    elapsed_ms: int = 0


def _probability(value: Any) -> float | None:
    """A finite probability in ``[0, 1]``, or ``None``.

    ``bool`` is rejected before the numeric check because it is an ``int`` in
    Python: a gateway that answered ``true`` would otherwise be read as
    ``p = 1.0``, inventing certainty the classifier never expressed. Numeric
    strings are rejected for the same reason -- a value Orb had to reinterpret
    is a value Orb does not actually understand.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not (0.0 <= number <= 1.0):
        return None
    return number


def normalize_response(
    payload: Any, questions: Sequence[NoulQuestion], *, elapsed_ms: int = 0, request_id: str = ""
) -> DecisionResponse:
    """Validate *payload* against the questions that were asked.

    Raises :class:`DecisionTransportError` only when the envelope itself is
    unreadable. A readable envelope always yields a response, even when every
    question inside it failed -- that distinction is what lets a partially valid
    batch supply its valid answers.
    """
    if not isinstance(payload, Mapping):
        raise DecisionTransportError("decision response was not a JSON object")
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, Mapping):
        raise DecisionTransportError("decision response carried no 'answers' object")

    answers: dict[str, float] = {}
    invalid: list[str] = []
    for question in questions:
        entry = raw_answers.get(question.key)
        probability = _probability(entry.get(question.question_type)) if isinstance(entry, Mapping) else None
        if probability is None:
            invalid.append(question.key)
        else:
            answers[question.key] = probability

    usage = payload.get("usage")
    returned_model = payload.get("model")
    response_id = payload.get("id")
    return DecisionResponse(
        answers=answers,
        invalid=tuple(invalid),
        returned_model=returned_model if isinstance(returned_model, str) else "",
        usage=dict(usage) if isinstance(usage, Mapping) else {},
        request_id=request_id or (response_id if isinstance(response_id, str) else ""),
        elapsed_ms=elapsed_ms,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Raw-answer cache
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    """A validated probability and the model version that produced it."""

    probability: float
    returned_model: str


def cache_namespace(*, endpoint_identity: str, config_revision: int) -> str:
    """The cache namespace for one classifier configuration.

    Identity, not credential: the endpoint's host and id, never its key. Changing
    the configuration bumps the revision, which changes the namespace, which is
    what "changing classifier configuration clears its namespace" means in
    practice -- the old entries become unreachable and age out on the TTL.
    """
    return f"{DECISION_CONTRACT_VERSION}|{endpoint_identity}|r{config_revision}"


def cache_key(namespace: str, model: str, state: str, question: NoulQuestion) -> str:
    """The full raw-answer cache key.

    Everything the gateway's answer depends on and nothing it does not: adapter
    contract, endpoint identity, configuration revision, requested model, the
    exact rendered state, and the question's type, instructions and criteria.
    Labels, authored outputs, thresholds and roll mode are absent on purpose --
    they are Orb's policy, not classifier input, so editing guidance must not
    cost another call.
    """
    return "\x1f".join((namespace, model, state, question.canonical()))


class RawAnswerCache:
    """A small, bounded, short-lived, process-local cache of valid answers.

    Only validated raw answers enter it. A fallback result never does (it is not
    an answer), and neither does a random draw (a new occurrence deserves a
    fresh one even when its classifier answer is reused). Insertion-ordered so
    eviction is oldest-first without a heap.
    """

    def __init__(self, capacity: int = CACHE_CAPACITY, ttl: float = CACHE_TTL_SECONDS) -> None:
        self.capacity = capacity
        self.ttl = ttl
        self._entries: dict[str, tuple[float, CachedAnswer]] = {}

    def get(self, key: str) -> CachedAnswer | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, answer = entry
        if time.monotonic() - stored_at > self.ttl:
            self._entries.pop(key, None)
            return None
        return answer

    def put(self, key: str, answer: CachedAnswer) -> None:
        # Re-inserting moves the key to the end, so a refreshed entry is not the
        # next one evicted.
        self._entries.pop(key, None)
        self._entries[key] = (time.monotonic(), answer)
        while len(self._entries) > self.capacity:
            self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        self._entries.clear()

    def clear_namespace(self, namespace: str) -> int:
        """Drop every entry in *namespace*; returns how many were removed."""
        prefix = f"{namespace}\x1f"
        stale = [key for key in self._entries if key.startswith(prefix)]
        for key in stale:
            self._entries.pop(key, None)
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)


#: The process-wide instance. Process-local and short-lived by design: it exists
#: to make one exchange's identical questions one call, not to be a durable
#: record. Persisted evaluation replay is independent of it.
RAW_ANSWER_CACHE = RawAnswerCache()


# ═══════════════════════════════════════════════════════════════════════════════
# Transport
# ═══════════════════════════════════════════════════════════════════════════════


class DecisionClient:
    """A single-call client for the decisions gateway.

    One instance per turn's configuration. It holds no conversation state, so a
    caller may reuse it across requests inside one exchange.
    """

    def __init__(
        self,
        url: str,
        api_key: str = "",
        model: str = DEFAULT_DECISION_MODEL,
        *,
        timeout: float = 3.0,
        proxy: str | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # As in LLMClient: "" is the settings default for "no proxy", and httpx
        # rejects it as a URL.
        self.proxy = proxy or None

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})}

    def request_for(self, state: str, questions: Sequence[NoulQuestion]) -> DecisionRequest:
        return DecisionRequest(model=self.model, state=state, questions=tuple(questions))

    async def decide(
        self, state: str, questions: Sequence[NoulQuestion], *, timeout: float | None = None, abort: AbortToken | None = None
    ) -> DecisionResponse:
        """Ask one batch and return the normalized answer.

        Raises :class:`DecisionCancelled` when *abort* fires, ``LLMCallError``
        for an HTTP rejection (with the provider's own sentence kept), and
        :class:`DecisionTransportError` for an unreadable body. A timeout
        surfaces as ``httpx.TimeoutException``; the caller decides what a
        timeout means for its budget, because only the caller knows how much of
        the exchange's decision time is left.
        """
        if abort is not None and abort.is_aborted:
            raise DecisionCancelled("stopped before the decision request was sent")
        request = self.request_for(state, questions)
        body = request.body()
        started = time.monotonic()
        call = asyncio.ensure_future(self._post(body, timeout if timeout is not None else self.timeout))
        try:
            payload = await self._race_abort(call, abort)
        finally:
            if not call.done():
                call.cancel()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return normalize_response(payload, request.questions, elapsed_ms=elapsed_ms)

    async def _race_abort(self, call: asyncio.Future, abort: AbortToken | None) -> Any:
        """Await *call*, but let a stop win.

        A stop has to interrupt the wait rather than be noticed after it: the
        per-request timeout is seconds long and the whole point of Stop is that
        the user does not wait them out.
        """
        if abort is None:
            return await call
        waiter = asyncio.ensure_future(abort.wait())
        try:
            done, _ = await asyncio.wait({call, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if call in done:
                return call.result()
            raise DecisionCancelled("stopped while the decision request was in flight")
        finally:
            waiter.cancel()

    async def _post(self, body: bytes, timeout: float) -> Any:
        async with httpx.AsyncClient(timeout=timeout, proxy=self.proxy) as client:
            response = await client.post(self.url, content=body, headers=self._headers())
            if response.status_code >= 400:
                text = response.text
                logger.error("Decision HTTP %d from %s: %s", response.status_code, self.url, text)
                raise llm_call_error(response=response, body=text, url=self.url, model=self.model, api_key=self.api_key)
            try:
                return response.json()
            except ValueError as error:
                raise DecisionTransportError("decision response was not valid JSON") from error


__all__ = [
    "CACHE_CAPACITY",
    "CACHE_TTL_SECONDS",
    "DECISION_CONTRACT_VERSION",
    "DEFAULT_DECISION_MODEL",
    "MAX_QUESTIONS_PER_REQUEST",
    "MAX_QUESTION_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_STATE_BYTES",
    "RAW_ANSWER_CACHE",
    "CachedAnswer",
    "DecisionCancelled",
    "DecisionClient",
    "DecisionRequest",
    "DecisionResponse",
    "DecisionTransportError",
    "NoulQuestion",
    "RawAnswerCache",
    "cache_key",
    "cache_namespace",
    "decisions_url",
    "normalize_response",
]
