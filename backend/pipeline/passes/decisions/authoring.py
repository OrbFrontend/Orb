from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....core import OUTCOME_KEYS, decision_definition_errors, parse_decision_definition
from ....inference import (
    MAX_QUESTION_BYTES,
    MAX_STATE_BYTES,
    DecisionClient,
    NoulQuestion,
)
from .render import (
    TEXT_MACROS,
    DecisionSnapshot,
    UnavailableMacro,
    macro_errors,
    render,
    template_errors,
)
from .stage import REQUEST_TIMEOUT_SECONDS, DecisionConfig

SAMPLE_SNAPSHOT = DecisionSnapshot(
    last_message="I put my shoulder to the door and shove.",
    last_assistant_message="Maren braces both hands against the frame, breathing hard. The latch has not caught.",
    recent_history="User: I follow her into the passage.\n\nAssistant: She reaches the door first and turns, blocking it with her body.",
    user="You",
    char="Maren",
    description="A wizard, unarmoured, quick with her hands and quicker with her temper.",
)
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
        texts = value.values() if isinstance(value, Mapping) else [value] if isinstance(value, str) else []
        for text in texts:
            problems.extend(macro_errors(text, allowed=TEXT_MACROS, field=label))
    return problems


def preview(row: Mapping[str, Any], snapshot: DecisionSnapshot = SAMPLE_SNAPSHOT) -> dict[str, Any]:
    definition = parse_decision_definition(row)
    if definition is None:
        return {"ok": False, "problems": definition_problems(row)}
    try:
        state = render(definition.state_template, snapshot)
        instructions = render(definition.instructions, snapshot, allowed=TEXT_MACROS)
        criteria = {key: render(definition.criteria[key], snapshot, allowed=TEXT_MACROS) for key in OUTCOME_KEYS}
        outputs = {key: render(definition.outputs[key], snapshot, allowed=TEXT_MACROS) for key in OUTCOME_KEYS}
    except UnavailableMacro as unavailable:
        return {
            "ok": False,
            "problems": [f"{{{{{unavailable.macro}}}}} is not available in this scope"],
            "unavailable_macro": unavailable.macro,
        }
    state_bytes = len(state.encode())
    question_bytes = len(instructions.encode()) + sum(len(text.encode()) for text in criteria.values())
    return {
        "ok": True,
        "problems": [],
        "state": state,
        "instructions": instructions,
        "criteria": criteria,
        "outputs": outputs,
        "state_bytes": state_bytes,
        "state_limit": MAX_STATE_BYTES,
        "question_bytes": question_bytes,
        "question_limit": MAX_QUESTION_BYTES,
        "oversized": state_bytes > MAX_STATE_BYTES or question_bytes > MAX_QUESTION_BYTES,
    }


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
