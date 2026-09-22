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

DECISION_CONTRACT_VERSION = "jev/2"
DEFAULT_DECISION_MODEL = "typesafe/jev-1.13"
MAX_STATE_BYTES = 16 * 1024
MAX_QUESTION_BYTES = 8 * 1024
MAX_QUESTIONS_PER_REQUEST = 32
MAX_QUESTIONS_PER_EXCHANGE = 128
MAX_REQUEST_BYTES = 64 * 1024
CACHE_CAPACITY = 512
CACHE_TTL_SECONDS = 600.0


class DecisionCancelled(Exception):
    pass


class DecisionTransportError(Exception):
    pass


def decisions_url(base_url: str) -> str:
    parts = urlsplit(base_url.strip().rstrip("/"))
    segments = [segment for segment in parts.path.split("/") if segment]
    while segments and segments[-1] in ("chat", "completions", "v1", "responses"):
        segments.pop()
    return urlunsplit((parts.scheme, parts.netloc, "/" + "/".join((*segments, "alpha", "decisions")), "", ""))


@dataclass(frozen=True, slots=True)
class DecisionQuestion:
    key: str
    instructions: str
    criteria: Mapping[str, str] | Sequence[str]
    question_type: str = "noul"

    def payload(self) -> dict[str, Any]:
        criteria: dict[str, str] | list[str]
        if isinstance(self.criteria, Mapping):
            keys = OUTCOME_KEYS if self.question_type == "noul" else tuple(self.criteria)
            criteria = {key: self.criteria[key] for key in keys if key in self.criteria}
        else:
            criteria = list(self.criteria)
        return {
            "type": self.question_type,
            "instructions": self.instructions,
            "criteria": criteria,
        }

    def canonical(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"))

    def rendered_bytes(self) -> int:
        return len(self.canonical().encode())


NoulQuestion = DecisionQuestion


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    selected: str
    probabilities: Mapping[str, float]
    confidence: float
    answer_type: str = "choice"


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    probabilities: Mapping[str, float]
    confidence: float
    legend: Mapping[str, str]
    answer_type: str = "score"


NormalizedAnswer = float | ChoiceAnswer | ScoreAnswer


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    model: str
    state: str
    questions: tuple[DecisionQuestion, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "state": self.state,
            "questions": {question.key: question.payload() for question in self.questions},
        }

    def body(self) -> bytes:
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    answers: Mapping[str, NormalizedAnswer]
    invalid: tuple[str, ...] = ()
    returned_model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = ""
    elapsed_ms: int = 0


def _probability(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 1 else None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _distribution(value: Any, keys: Sequence[str]) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        return None
    result: dict[str, float] = {}
    for key in keys:
        probability = _probability(value.get(key))
        if probability is None:
            return None
        result[key] = probability
    total = sum(result.values())
    return result if math.isclose(total, 1.0, rel_tol=0.0, abs_tol=0.02) else None


def _answer(entry: Any, question: DecisionQuestion) -> NormalizedAnswer | None:
    if not isinstance(entry, Mapping):
        return None
    tagged_type = entry.get("type")
    if tagged_type is not None and tagged_type != question.question_type:
        return None
    if question.question_type == "noul":
        return _probability(entry.get("noul"))
    if question.question_type == "choice":
        if not isinstance(question.criteria, Mapping):
            return None
        keys = tuple(question.criteria)
        selected = entry.get("choice")
        probabilities = _distribution(entry.get("probabilities"), keys)
        confidence = _probability(entry.get("confidence"))
        if not isinstance(selected, str) or selected not in keys or probabilities is None or confidence is None:
            return None
        return ChoiceAnswer(selected=selected, probabilities=probabilities, confidence=confidence)
    if question.question_type == "score":
        if isinstance(question.criteria, Mapping):
            return None
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
            or any(not isinstance(legend.get(key), str) for key in keys)
        ):
            return None
        return ScoreAnswer(
            score=score,
            probabilities=probabilities,
            confidence=confidence,
            legend={key: str(legend[key]) for key in keys},
        )
    return None


def normalize_response(
    payload: Any, questions: Sequence[DecisionQuestion], *, elapsed_ms: int = 0, request_id: str = ""
) -> DecisionResponse:
    if not isinstance(payload, Mapping):
        raise DecisionTransportError("decision response was not a JSON object")
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, Mapping):
        raise DecisionTransportError("decision response carried no 'answers' object")
    answers: dict[str, NormalizedAnswer] = {}
    invalid: list[str] = []
    for question in questions:
        entry = raw_answers.get(question.key)
        answer = _answer(entry, question)
        if answer is None:
            invalid.append(question.key)
        else:
            answers[question.key] = answer
    usage, model, response_id = payload.get("usage"), payload.get("model"), payload.get("id")
    return DecisionResponse(
        answers=answers,
        invalid=tuple(invalid),
        returned_model=model if isinstance(model, str) else "",
        usage=dict(usage) if isinstance(usage, Mapping) else {},
        request_id=request_id or (response_id if isinstance(response_id, str) else ""),
        elapsed_ms=elapsed_ms,
    )


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    answer: NormalizedAnswer
    returned_model: str

    @property
    def probability(self) -> float | None:
        return self.answer if isinstance(self.answer, float) else None


def cache_namespace(*, endpoint_identity: str, config_revision: int) -> str:
    return f"{DECISION_CONTRACT_VERSION}|{endpoint_identity}|r{config_revision}"


def cache_key(namespace: str, model: str, state: str, question: DecisionQuestion) -> str:
    return "\x1f".join((namespace, model, state, question.canonical()))


class RawAnswerCache:
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
        self._entries.pop(key, None)
        self._entries[key] = (time.monotonic(), answer)
        while len(self._entries) > self.capacity:
            self._entries.pop(next(iter(self._entries)))

    def discard(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def clear_namespace(self, namespace: str) -> int:
        prefix = namespace + "\x1f"
        stale = [key for key in self._entries if key.startswith(prefix)]
        for key in stale:
            self._entries.pop(key)
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)


RAW_ANSWER_CACHE = RawAnswerCache()


class DecisionClient:
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
        self.proxy = proxy or None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def request_for(self, state: str, questions: Sequence[DecisionQuestion]) -> DecisionRequest:
        return DecisionRequest(self.model, state, tuple(questions))

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
        request = self.request_for(state, questions)
        started = time.monotonic()
        call = asyncio.ensure_future(self._post(request.body(), timeout if timeout is not None else self.timeout))
        try:
            payload = await self._race_abort(call, abort)
        finally:
            if not call.done():
                call.cancel()
        return normalize_response(payload, request.questions, elapsed_ms=int((time.monotonic() - started) * 1000))

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
        async with httpx.AsyncClient(timeout=timeout, proxy=self.proxy) as client:
            response = await client.post(self.url, content=body, headers=self._headers())
            if response.status_code >= 400:
                logger.error("Decision HTTP %d from %s: %s", response.status_code, self.url, response.text)
                raise llm_call_error(
                    response=response, body=response.text, url=self.url, model=self.model, api_key=self.api_key
                )
            try:
                return response.json()
            except ValueError as error:
                raise DecisionTransportError("decision response was not valid JSON") from error
