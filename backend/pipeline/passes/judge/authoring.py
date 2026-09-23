from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....core import decision_definition_errors, parse_decision_definition
from ....inference import DecisionClient, DecisionQuestion
from .judge import REQUEST_TIMEOUT_SECONDS, JudgeConfig
from .render import definition_macro_errors

CONNECTION_TEST_STATE = "Alric attempts to force Maren back from the doorway."
CONNECTION_TEST_QUESTION = DecisionQuestion(
    key="connection_test",
    instructions="Does Alric prevail in this exchange?",
    criteria={
        "true": "Alric ends the exchange in control of the doorway.",
        "false": "Alric is driven back or forced to disengage.",
    },
)


def definition_problems(row: Mapping[str, Any]) -> list[str]:
    problems = decision_definition_errors(row)
    if definition := parse_decision_definition({**row, "field_type": "decision"}):
        problems.extend(definition_macro_errors(definition))
    return problems


async def connection_test(config: JudgeConfig) -> dict[str, Any]:
    if not config.configured:
        return {"ok": False, "error": "No decision endpoint is configured"}
    response = await DecisionClient(
        config.url, config.api_key, config.model, timeout=REQUEST_TIMEOUT_SECONDS, proxy=config.proxy
    ).decide(CONNECTION_TEST_STATE, [CONNECTION_TEST_QUESTION])
    probability = response.answers.get(CONNECTION_TEST_QUESTION.key)
    return {
        "ok": probability is not None,
        "url": config.url,
        "requested_model": config.model,
        "returned_model": response.returned_model,
        "probability": probability,
        "elapsed_ms": response.elapsed_ms,
        "error": "" if probability is not None else "The gateway answered, but not with a usable probability",
    }
