from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .client import AbortToken
from .errors import llm_call_error

logger = logging.getLogger(__name__)

MAX_STATE_BYTES = 16 * 1024
MAX_QUESTION_BYTES = 8 * 1024
MAX_QUESTIONS_PER_REQUEST = 32
MAX_REQUEST_BYTES = 64 * 1024
CACHE_CAPACITY = 512
CACHE_TTL_SECONDS = 600.0


class DecisionCancelled(Exception):
    pass


class DecisionTransportError(Exception):
    pass


# Path segments that belong to a chat route rather than to the gateway itself,
# plus the route prefix this module appends. A trailing ``alpha`` is stripped for
# the same reason the rest are: the judge URL a user pastes is as likely to be
# the decisions route as the chat base, and appending to one that already ends in
# ``alpha`` produced ``/alpha/alpha/decisions`` -- a 404 whose only symptom was
# the Test button.
_ROUTE_SUFFIXES = ("chat", "completions", "v1", "responses", "alpha")


def decisions_url(base_url: str) -> str:
    """The alpha decisions route for *base_url*, derived idempotently.

    A URL that already names a ``decisions`` route is returned as the user spelled
    it, so a gateway that mounts the contract somewhere else needs no second
    setting -- pasting the route is the override. Everything else is treated as a
    base: chat-route tails come off and ``alpha/decisions`` goes on.
    """
    parts = urlsplit(base_url.strip().rstrip("/"))
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and segments[-1] == "decisions":
        return urlunsplit((parts.scheme, parts.netloc, "/" + "/".join(segments), "", ""))
    while segments and segments[-1] in _ROUTE_SUFFIXES:
        segments.pop()
    return urlunsplit((parts.scheme, parts.netloc, "/" + "/".join((*segments, "alpha", "decisions")), "", ""))


@dataclass(frozen=True, slots=True)
class DecisionQuestion:
    key: str
    instructions: str
    criteria: Mapping[str, str] | Sequence[str]
    question_type: str = "noul"

    def payload(self) -> dict[str, Any]:
        criteria = dict(self.criteria) if isinstance(self.criteria, Mapping) else list(self.criteria)
        return {"type": self.question_type, "instructions": self.instructions, "criteria": criteria}

    def canonical(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    selected: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    probabilities: Mapping[str, float]
    confidence: float
    legend: Mapping[str, str]


NormalizedAnswer = float | ChoiceAnswer | ScoreAnswer


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    answers: Mapping[str, NormalizedAnswer]
    returned_model: str = ""
    elapsed_ms: int = 0


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _probability(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and 0 <= number <= 1 else None


def _distribution(value: Any, keys: Sequence[str]) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        return None
    result: dict[str, float] = {}
    for key in keys:
        if (probability := _probability(value[key])) is None:
            return None
        result[key] = probability
    total = sum(result.values())
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=0.02):
        return None
    # Accept provider rounding, but do not assign its missing mass to the last
    # option (which can have zero odds) or truncate the tail when mass exceeds 1.
    return {key: probability / total for key, probability in result.items()}


def _answer(entry: Any, question: DecisionQuestion) -> NormalizedAnswer | None:
    if not isinstance(entry, Mapping):
        return None
    if entry.get("type") not in (None, question.question_type):
        return None
    if question.question_type == "noul":
        return _probability(entry.get("noul"))
    if question.question_type == "choice" and isinstance(question.criteria, Mapping):
        keys = tuple(question.criteria)
        selected = entry.get("choice")
        probabilities = _distribution(entry.get("probabilities"), keys)
        confidence = _probability(entry.get("confidence"))
        if selected not in keys or probabilities is None or confidence is None:
            return None
        return ChoiceAnswer(selected=str(selected), probabilities=probabilities, confidence=confidence)
    if question.question_type == "score" and not isinstance(question.criteria, Mapping):
        keys = tuple(str(index) for index in range(len(question.criteria)))
        score = _finite(entry.get("score"))
        probabilities = _distribution(entry.get("probabilities"), keys)
        confidence = _probability(entry.get("confidence"))
        legend = entry.get("legend")
        if (
            score is None
            or not 0 <= score <= len(keys) - 1
            or probabilities is None
            or confidence is None
            or not isinstance(legend, Mapping)
            or set(legend) != set(keys)
            or any(not isinstance(legend[key], str) for key in keys)
        ):
            return None
        return ScoreAnswer(score=score, probabilities=probabilities, confidence=confidence, legend=dict(legend))
    return None


def normalize_response(payload: Any, questions: Sequence[DecisionQuestion], *, elapsed_ms: int = 0) -> DecisionResponse:
    """Keep the answers that match their question; a missing or malformed one is simply absent."""
    if not isinstance(payload, Mapping):
        raise DecisionTransportError("decision response was not a JSON object")
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, Mapping):
        raise DecisionTransportError("decision response carried no 'answers' object")
    answers = {question.key: _answer(raw_answers.get(question.key), question) for question in questions}
    model = payload.get("model")
    return DecisionResponse(
        answers={key: answer for key, answer in answers.items() if answer is not None},
        returned_model=model if isinstance(model, str) else "",
        elapsed_ms=elapsed_ms,
    )


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    answer: NormalizedAnswer
    returned_model: str


def cache_key(url: str, model: str, state: str, question: DecisionQuestion) -> str:
    return "\x1f".join((url, model, state, question.canonical()))


class RawAnswerCache:
    """A small TTL cache that evicts the oldest entry once it is over capacity."""

    def __init__(self, capacity: int = CACHE_CAPACITY, ttl: float = CACHE_TTL_SECONDS) -> None:
        self.capacity = capacity
        self.ttl = ttl
        self._entries: dict[str, tuple[float, CachedAnswer]] = {}

    def get(self, key: str) -> CachedAnswer | None:
        stored_at, answer = self._entries.get(key, (0.0, None))
        if answer is not None and time.monotonic() - stored_at > self.ttl:
            del self._entries[key]
            return None
        return answer

    def put(self, key: str, answer: CachedAnswer) -> None:
        self._entries.pop(key, None)
        self._entries[key] = (time.monotonic(), answer)
        while len(self._entries) > self.capacity:
            self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        self._entries.clear()


RAW_ANSWER_CACHE = RawAnswerCache()


class DecisionClient:
    def __init__(self, url: str, api_key: str, model: str, *, timeout: float, proxy: str | None = None) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.proxy = proxy or None

    async def decide(
        self,
        state: str,
        questions: Sequence[DecisionQuestion],
        *,
        timeout: float | None = None,
        abort: AbortToken | None = None,
    ) -> DecisionResponse:
        if abort is not None and abort.is_aborted:
            raise DecisionCancelled("stopped before the decision request was sent")
        body = {"model": self.model, "state": state, "questions": {q.key: q.payload() for q in questions}}
        started = time.monotonic()
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        call = asyncio.ensure_future(self._post(encoded, timeout if timeout is not None else self.timeout))
        try:
            payload = await self._race_abort(call, abort)
        finally:
            if not call.done():
                call.cancel()
        return normalize_response(payload, questions, elapsed_ms=int((time.monotonic() - started) * 1000))

    async def _race_abort(self, call: asyncio.Future, abort: AbortToken | None) -> Any:
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
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=timeout, proxy=self.proxy) as client:
            response = await client.post(self.url, content=body, headers=headers)
            if response.status_code >= 400:
                logger.error("Decision HTTP %d from %s: %s", response.status_code, self.url, response.text)
                raise llm_call_error(
                    response=response, body=response.text, url=self.url, model=self.model, api_key=self.api_key
                )
            try:
                return response.json()
            except ValueError as error:
                raise DecisionTransportError("decision response was not valid JSON") from error
