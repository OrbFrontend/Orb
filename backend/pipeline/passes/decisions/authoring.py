from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....core import decision_definition_errors
from ....inference import (
    DecisionClient,
    NoulQuestion,
)
from .render import (
    TEXT_MACROS,
    macro_errors,
    template_errors,
)
from .stage import REQUEST_TIMEOUT_SECONDS, DecisionConfig

CONNECTION_TEST_STATE = "Alric attempts to force Maren back from the doorway."
CONNECTION_TEST_QUESTION = NoulQuestion(
    key="connection_test",
    instructions="Does Alric prevail in this exchange?",
    criteria={
        "true": "Alric ends the exchange in control of the doorway.",
        "false": "Alric is driven back or forced to disengage.",
    },
)
_TEXT_FIELDS = (
    ("decision_instructions", "Question"),
    ("decision_criteria", "Outcome description"),
    ("decision_outputs", "Guidance"),
)


def definition_problems(row: Mapping[str, Any]) -> list[str]:
    problems = list(decision_definition_errors(row))
    if isinstance(template := row.get("decision_state_template"), str):
        problems.extend(template_errors(template, field="Situation template"))
    for key, label in _TEXT_FIELDS:
        value = row.get(key)
        texts = (
            value.values()
            if isinstance(value, Mapping)
            else value
            if isinstance(value, list)
            else [value]
            if isinstance(value, str)
            else []
        )
        for text in texts:
            problems.extend(macro_errors(text, allowed=TEXT_MACROS, field=label))
    return problems


async def connection_test(config: DecisionConfig) -> dict[str, Any]:
    if not config.configured:
        return {"ok": False, "error": "No decision endpoint is configured"}
    response = await DecisionClient(
        config.url,
        api_key=config.api_key,
        model=config.model,
        timeout=REQUEST_TIMEOUT_SECONDS,
        proxy=config.proxy,
    ).decide(CONNECTION_TEST_STATE, [CONNECTION_TEST_QUESTION])
    probability = response.answers.get(CONNECTION_TEST_QUESTION.key)
    return {
        "ok": probability is not None,
        "url": config.url,
        "requested_model": config.model,
        "returned_model": response.returned_model,
        "probability": probability,
        "elapsed_ms": response.elapsed_ms,
        "usage": dict(response.usage),
        "error": "" if probability is not None else "The gateway answered, but not with a usable probability",
    }
