"""What the editor needs: full validation, a preview, and a connection test.

The editor and the Inspector use the same rendering contract as the turn -- one
:mod:`.render` -- so a preview cannot show an author something the pipeline
would not send. That is the whole point of this module existing beside the
stage rather than in the API layer.

None of it touches chat state. A preview renders; a test sends one synthetic
scene. Neither advances a cooldown, writes an evaluation record, or reads
conversation content it was not handed.
"""

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
    STATE_MACROS,
    TEXT_MACROS,
    DecisionSnapshot,
    UnavailableMacro,
    macro_errors,
    render,
    template_errors,
)
from .stage import REQUEST_TIMEOUT_SECONDS, DecisionConfig

#: The scene a preview renders against when the editor is not open on a chat.
#: Concrete rather than lorem: an author judging whether their question reads
#: well needs to see it against prose, and a placeholder that says "sample text"
#: hides exactly the sentence that would have shown the problem.
SAMPLE_SNAPSHOT = DecisionSnapshot(
    last_message="I put my shoulder to the door and shove.",
    last_assistant_message="Maren braces both hands against the frame, breathing hard. The latch has not caught.",
    recent_history=(
        "User: I follow her into the passage.\n\nAssistant: She reaches the door first and turns, blocking it with her body."
    ),
    user="You",
    char="Maren",
    cast="",
    description="A wizard, unarmoured, quick with her hands and quicker with her temper.",
    scope="solo",
)

#: The synthetic scene the connection test sends. Deliberately not conversation
#: content: a test is a check that the gateway answers, and it must not ship the
#: user's story to a provider they have not yet decided to use.
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
    """Every reason *row* is not a savable decision, in author-facing prose.

    The union of the definition contract (``core.decisions``) and the renderer's
    macro set, so the authoring API rejects exactly what the stage would later be
    unable to run. Returning all of them at once is deliberate -- an author
    fixing one field per round trip is the same information delivered worse.
    """
    problems = list(decision_definition_errors(row))

    template = row.get("decision_state_template")
    if isinstance(template, str):
        problems.extend(template_errors(template, allowed=STATE_MACROS, field="Situation template"))

    for key, label in _TEXT_FIELDS:
        value = row.get(key)
        texts = list(value.values()) if isinstance(value, Mapping) else ([value] if isinstance(value, str) else [])
        for text in texts:
            problems.extend(macro_errors(text, allowed=TEXT_MACROS, field=label))
    return problems


def preview(row: Mapping[str, Any], snapshot: DecisionSnapshot = SAMPLE_SNAPSHOT) -> dict[str, Any]:
    """Render *row* against *snapshot* exactly as the stage would.

    Reports sizes alongside the text because the limits are part of the contract:
    an oversized input uses the fallback with a visible reason, and an author who
    can see the number coming is an author who does not discover it mid-scene.
    """
    definition = parse_decision_definition(row)
    if definition is None:
        return {"ok": False, "problems": definition_problems(row)}
    try:
        state = render(definition.state_template, snapshot, allowed=STATE_MACROS)
        instructions = render(definition.instructions, snapshot, allowed=TEXT_MACROS)
        criteria = {key: render(definition.criteria[key], snapshot, allowed=TEXT_MACROS) for key in OUTCOME_KEYS}
        outputs = {key: render(definition.outputs[key], snapshot, allowed=TEXT_MACROS) for key in OUTCOME_KEYS}
    except UnavailableMacro as unavailable:
        return {
            "ok": False,
            "problems": [f"{{{{{unavailable.macro}}}}} is not available in this scope"],
            "unavailable_macro": unavailable.macro,
        }

    state_bytes = len(state.encode("utf-8"))
    question_bytes = len(instructions.encode("utf-8")) + sum(len(text.encode("utf-8")) for text in criteria.values())
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
    """Send one synthetic question and report what came back.

    An explicit editor action with no chat side effects: it writes no evaluation
    record, advances no cooldown, and is not served from -- or written to -- the
    raw-answer cache, because an author pressing Test wants to know whether the
    gateway answers *now*.
    """
    if not config.configured:
        return {"ok": False, "error": "No decision endpoint is configured"}
    client = DecisionClient(
        config.url,
        api_key=config.api_key,
        model=config.model,
        timeout=REQUEST_TIMEOUT_SECONDS,
        proxy=config.proxy,
    )
    response = await client.decide(CONNECTION_TEST_STATE, [CONNECTION_TEST_QUESTION])
    probability = response.answers.get(CONNECTION_TEST_QUESTION.key)
    return {
        "ok": probability is not None,
        "url": config.url,
        "requested_model": config.model,
        # The version actually served. Recorded even when the requested name is
        # an alias, because an alias can move under a warm cache.
        "returned_model": response.returned_model,
        "probability": probability,
        "elapsed_ms": response.elapsed_ms,
        "usage": dict(response.usage),
        "error": "" if probability is not None else "The gateway answered, but not with a usable probability",
    }


__all__ = [
    "CONNECTION_TEST_QUESTION",
    "CONNECTION_TEST_STATE",
    "SAMPLE_SNAPSHOT",
    "connection_test",
    "definition_problems",
    "preview",
]
