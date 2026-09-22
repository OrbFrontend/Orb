"""The decision gateway adapter: strict normalization, cache keys, transport.

The contract this adapter implements is documented but **not yet verified
against the live gateway** — pinning it with saved fixtures is a release gate in
``docs/plans/decision-fragments.md``. These tests therefore guard the property
that makes a wrong guess safe rather than silently wrong: an answer this adapter
cannot read is a failure for its own question, never an implicit ``false``.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from backend.inference import (
    DECISION_CONTRACT_VERSION,
    AbortToken,
    CachedAnswer,
    ChoiceAnswer,
    DecisionCancelled,
    DecisionClient,
    DecisionQuestion,
    DecisionRequest,
    DecisionTransportError,
    LLMCallError,
    NoulQuestion,
    RawAnswerCache,
    ScoreAnswer,
    cache_key,
    cache_namespace,
    decisions_url,
    normalize_response,
)

QUESTION = NoulQuestion(
    key="outcome",
    instructions="Does Alric prevail in this exchange?",
    criteria={"true": "Alric ends in control.", "false": "Alric is driven back."},
)


def _payload(**overrides) -> dict:
    payload = {
        "model": "typesafe/jev-1.13.2",
        "id": "req-1",
        "usage": {"total_tokens": 12},
        "answers": {"outcome": {"noul": 0.83}},
    }
    payload.update(overrides)
    return payload


# ── the route ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base",
    [
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/",
        "https://openrouter.ai/api/v1/chat/completions",
        "https://openrouter.ai/api",
        # The route itself, and the prefix the panel's own placeholder shows.
        # Deriving from either must land on the same URL: appending blindly is
        # what produced /api/alpha/alpha/decisions and a 404 with no explanation.
        "https://openrouter.ai/api/alpha",
        "https://openrouter.ai/api/alpha/",
        "https://openrouter.ai/api/alpha/decisions",
    ],
)
def test_the_decisions_route_is_derived_from_the_chat_base(base):
    assert decisions_url(base) == "https://openrouter.ai/api/alpha/decisions"


def test_deriving_the_route_twice_changes_nothing():
    once = decisions_url("https://openrouter.ai/api/v1")
    assert decisions_url(once) == once


def test_a_gateway_that_spells_the_route_itself_keeps_that_spelling():
    # The escape hatch that replaced the separate override setting: a URL that
    # already names a decisions route is the route, wherever it is mounted.
    assert decisions_url("https://gw.test/v2/judge/decisions") == "https://gw.test/v2/judge/decisions"


def test_a_non_openrouter_base_keeps_its_own_path():
    assert decisions_url("https://gateway.test/proxy/v1") == "https://gateway.test/proxy/alpha/decisions"


# ── request serialization ────────────────────────────────────────────────────


def test_the_request_carries_the_shared_state_once_and_one_entry_per_question():
    request = DecisionRequest(model="m", state="the scene", questions=(QUESTION, NoulQuestion("b", "Q?", QUESTION.criteria)))
    payload = request.payload()
    assert payload["state"] == "the scene"
    assert set(payload["questions"]) == {"outcome", "b"}
    assert payload["questions"]["outcome"]["type"] == "noul"
    assert list(payload["questions"]["outcome"]["criteria"]) == ["true", "false"]


def test_criteria_serialize_in_outcome_order_regardless_of_dict_order():
    # The canonical form is what identifies a question for caching, so two
    # definitions differing only in dict order are one question and must share an
    # entry rather than each paying for its own call.
    reversed_order = NoulQuestion("outcome", QUESTION.instructions, {"false": "b", "true": "a"})
    natural_order = NoulQuestion("outcome", QUESTION.instructions, {"true": "a", "false": "b"})
    assert reversed_order.canonical() == natural_order.canonical()
    assert list(json.loads(reversed_order.canonical())["criteria"]) == ["true", "false"]


# ── response normalization ───────────────────────────────────────────────────


def test_a_valid_answer_normalizes_with_its_metadata():
    response = normalize_response(_payload(), [QUESTION], elapsed_ms=5)
    assert response.answers == {"outcome": 0.83}
    assert response.invalid == ()
    assert response.returned_model == "typesafe/jev-1.13.2"
    assert response.usage == {"total_tokens": 12}
    assert response.request_id == "req-1"
    assert response.elapsed_ms == 5


@pytest.mark.parametrize(
    "answer",
    [
        {"noul": True},  # a bool is not a probability, even though it is an int
        {"noul": "0.83"},  # a numeric string is a value we had to reinterpret
        {"noul": 1.5},
        {"noul": -0.1},
        {"noul": float("nan")},
        {"noul": float("inf")},
        {"noul": None},
        {"score": 0.83},  # the wrong primitive
        {},
        "not an object",
    ],
)
def test_an_unusable_answer_is_a_failure_for_its_own_question(answer):
    response = normalize_response(_payload(answers={"outcome": answer}), [QUESTION])
    assert response.answers == {}
    assert response.invalid == ("outcome",)


def test_a_missing_answer_is_not_an_implicit_false():
    response = normalize_response(_payload(answers={}), [QUESTION])
    assert response.answers == {}
    assert response.invalid == ("outcome",)


def test_probability_boundaries_are_accepted():
    for value in (0, 0.0, 1, 1.0):
        response = normalize_response(_payload(answers={"outcome": {"noul": value}}), [QUESTION])
        assert response.answers == {"outcome": float(value)}


def test_a_mixed_batch_keeps_its_valid_answers():
    questions = [QUESTION, NoulQuestion("other", "Q?", QUESTION.criteria)]
    response = normalize_response(_payload(answers={"outcome": {"noul": 0.4}, "other": {"noul": "nope"}}), questions)
    assert response.answers == {"outcome": 0.4}
    assert response.invalid == ("other",)


def test_choice_and_score_answers_normalize_without_coercion():
    choice = DecisionQuestion("beat", "Beat?", {"clean": "Clean", "messy": "Messy"}, "choice")
    score = DecisionQuestion("cost", "Cost?", ("Low", "High"), "score")
    response = normalize_response(
        {
            "answers": {
                "beat": {
                    "type": "choice",
                    "choice": "messy",
                    "probabilities": {"clean": 0.2, "messy": 0.8},
                    "confidence": 0.7,
                },
                "cost": {
                    "type": "score",
                    "score": 0.75,
                    "legend": {"0": "Low", "1": "High"},
                    "probabilities": {"0": 0.25, "1": 0.75},
                    "confidence": 0.9,
                },
            }
        },
        [choice, score],
    )
    assert response.answers["beat"] == ChoiceAnswer("messy", {"clean": 0.2, "messy": 0.8}, 0.7)
    assert response.answers["cost"] == ScoreAnswer(0.75, {"0": 0.25, "1": 0.75}, 0.9, {"0": "Low", "1": "High"})


@pytest.mark.parametrize("payload", [None, [], "text", {"no_answers": 1}, {"answers": []}])
def test_an_unreadable_envelope_raises_rather_than_answering(payload):
    with pytest.raises(DecisionTransportError):
        normalize_response(payload, [QUESTION])


# ── the raw-answer cache ─────────────────────────────────────────────────────


def _key(**overrides) -> str:
    args = {"namespace": cache_namespace(endpoint_identity="host#1", config_revision=1), "model": "m", "state": "s"}
    args.update(overrides)
    return cache_key(args["namespace"], args["model"], args["state"], args.get("question", QUESTION))


def test_the_namespace_carries_the_contract_and_the_revision_but_no_credential():
    namespace = cache_namespace(endpoint_identity="host#1", config_revision=3)
    assert DECISION_CONTRACT_VERSION in namespace
    assert "r3" in namespace
    assert "secret" not in namespace


def test_the_cache_key_covers_every_classifier_input():
    base = _key()
    assert _key(model="other") != base
    assert _key(state="other") != base
    assert _key(namespace=cache_namespace(endpoint_identity="host#1", config_revision=2)) != base
    assert _key(namespace=cache_namespace(endpoint_identity="host#2", config_revision=1)) != base
    assert _key(question=NoulQuestion("outcome", "A different question?", QUESTION.criteria)) != base
    assert _key(question=NoulQuestion("outcome", QUESTION.instructions, {"true": "x", "false": "y"})) != base


def test_the_cache_key_does_not_normalize_whitespace_or_case():
    assert _key(state="The Scene") != _key(state="the scene")
    assert _key(state="the  scene") != _key(state="the scene")


def test_the_cache_key_ignores_the_question_key_itself():
    # Two fragments asking an identical question share an answer; the fragment id
    # is Orb's routing, not part of what the classifier was asked.
    assert _key(question=NoulQuestion("a", QUESTION.instructions, QUESTION.criteria)) == _key(
        question=NoulQuestion("b", QUESTION.instructions, QUESTION.criteria)
    )


def test_the_cache_is_bounded_and_evicts_oldest_first():
    cache = RawAnswerCache(capacity=2, ttl=60)
    for index in range(3):
        cache.put(f"k{index}", CachedAnswer(0.5, "m"))
    assert len(cache) == 2
    assert cache.get("k0") is None
    assert cache.get("k2") is not None


def test_a_cached_answer_expires_on_its_ttl(monkeypatch):
    cache = RawAnswerCache(capacity=8, ttl=10)
    cache.put("k", CachedAnswer(0.5, "m"))
    assert cache.get("k") is not None
    stored_at = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: stored_at + 11)
    assert cache.get("k") is None


def test_clearing_a_namespace_leaves_the_others_alone():
    cache = RawAnswerCache()
    one = cache_namespace(endpoint_identity="a#1", config_revision=1)
    two = cache_namespace(endpoint_identity="a#1", config_revision=2)
    cache.put(cache_key(one, "m", "s", QUESTION), CachedAnswer(0.5, "m"))
    cache.put(cache_key(two, "m", "s", QUESTION), CachedAnswer(0.6, "m"))
    assert cache.clear_namespace(one) == 1
    assert cache.get(cache_key(one, "m", "s", QUESTION)) is None
    assert cache.get(cache_key(two, "m", "s", QUESTION)) is not None


# ── transport ────────────────────────────────────────────────────────────────


def _client(handler, **kwargs) -> DecisionClient:
    client = DecisionClient("https://gw.test/alpha/decisions", api_key="secret", model="m", **kwargs)

    async def _post(body, timeout):
        return handler(json.loads(body))

    client._post = _post  # type: ignore[method-assign]
    return client


async def test_a_successful_call_returns_a_normalized_response():
    client = _client(lambda body: _payload())
    response = await client.decide("the scene", [QUESTION])
    assert response.answers == {"outcome": 0.83}
    assert response.elapsed_ms >= 0


async def test_a_stop_before_sending_raises_without_a_request():
    sent = []
    client = _client(lambda body: sent.append(body) or _payload())
    abort = AbortToken()
    abort.abort()
    with pytest.raises(DecisionCancelled):
        await client.decide("the scene", [QUESTION], abort=abort)
    assert sent == []


async def test_an_http_rejection_keeps_the_providers_own_sentence():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={"error": {"message": "No endpoint found for typesafe/jev-1.13"}})
        )
    ) as http:
        client = DecisionClient("https://gw.test/alpha/decisions", api_key="secret", model="typesafe/jev-1.13")

        async def _post(body, timeout):
            response = await http.post(client.url, content=body)
            if response.status_code >= 400:
                from backend.inference.errors import llm_call_error

                raise llm_call_error(
                    response=response, body=response.text, url=client.url, model=client.model, api_key=client.api_key
                )
            return response.json()

        client._post = _post  # type: ignore[method-assign]
        with pytest.raises(LLMCallError) as raised:
            await client.decide("the scene", [QUESTION])
    assert "No endpoint found" in raised.value.sentence
    assert "secret" not in raised.value.body
