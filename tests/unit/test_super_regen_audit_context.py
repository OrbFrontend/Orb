"""Regression test: editor_pass must accept an explicit audit_context_msgs list and use it instead of extracting previous
assistant messages from prefix.

The bug this guards: during handle_super_regenerate, the prefix includes target["content"] (the message being replaced) as an
assistant message. Without the fix, the editor's repetition scanner picked up that message as "prior context" and flagged the
new draft for repeating the message it was literally told to replace.
"""

from unittest.mock import patch

import pytest

from backend.analysis import AuditReport
from backend.analysis.detectors.opening_monotony import MonotonyResult
from backend.analysis.detectors.slop_detector import DetectionResult
from backend.analysis.detectors.structural_repetition import StructuralResult
from backend.analysis.detectors.template_repetition import TemplateResult
from backend.inference import CachedBase, LLMClient
from backend.pipeline.passes.editor.editor import editor_pass
from backend.prompting.tool_catalog import enabled_schemas

PRIOR = "He looked out the window. The city hummed below."
REPLACED = "She spun around. Her breath caught. The room fell silent."
_UNSET = object()


def _report(repetitive: bool = False) -> AuditReport:
    return AuditReport(
        cliche_result=DetectionResult(flagged_sentences=[], unique_cliches=[], total_sentences=1, flagged_count=0),
        monotony_result=MonotonyResult([], {}, 0, 0.0),
        template_result=TemplateResult([], {}, 0, 0, 0.0),
        not_but_result=[],
        structural_repetition_result=(
            StructuralResult(is_repetitive=True, min_similarity=0.9, mean_similarity=0.9, pairs=[]) if repetitive else None
        ),
    )


async def _edit(prefix: list[dict], audit_context_msgs=_UNSET, draft: str = "Some new draft.") -> tuple[list[list[str]], list]:
    """Run the editor over *prefix* (the patch tool enabled, as super-regen does); return the audit contexts it scanned."""
    seen: list[list[str]] = []

    async def fake_contextual_audit(draft, phrase_bank, previous_assistant_msgs, audit_toggles=None, user_message=""):
        seen.append(list(previous_assistant_msgs))
        # The scanner flags structural repetition when the replaced message is in the context.
        return (_report(True), "structural repetition detected") if REPLACED in previous_assistant_msgs else (_report(), "")

    base = CachedBase(prefix=tuple(prefix), tools=tuple(enabled_schemas({"editor_apply_patch": True}, {})), model="test-model")
    extra = {} if audit_context_msgs is _UNSET else {"audit_context_msgs": audit_context_msgs}
    with patch("backend.pipeline.passes.editor.editor._run_contextual_audit", new=fake_contextual_audit):
        events = [
            event
            async for event in editor_pass(
                LLMClient("http://localhost:9999"),
                base,
                effective_msg="[OOC: rewrite]",
                draft=draft,
                settings={"model_name": "test-model"},
                phrase_bank=[],
                audit_enabled=True,
                length_guard=None,
                **extra,
            )
        ]
    return seen, events


def _turns(*assistant: str) -> list[dict]:
    """A system message, then each assistant reply after its own user turn."""
    return [
        {"role": "system", "content": "sys"},
        *({"role": r, "content": c} for a in assistant for r, c in (("user", "u"), ("assistant", a))),
    ]


@pytest.mark.parametrize(
    "prefix,context,scanned",
    [
        # An explicit list is forwarded directly, even an empty one.
        (_turns(REPLACED), [], []),
        # Without one, assistant messages are extracted from prefix, most recent first.
        (_turns("first assistant", "second assistant"), _UNSET, ["second assistant", "first assistant"]),
        # Surgical: prior history is still scanned; only the replaced message is excluded.
        (_turns(PRIOR, REPLACED), [PRIOR], [PRIOR]),
    ],
)
async def test_the_audit_context(prefix, context, scanned):
    seen, _ = await _edit(prefix, context)
    assert seen == [scanned]


async def test_super_regen_does_not_flag_replaced_message():
    """An identical draft is clean when the context excludes the replaced message: no LLM call, draft unchanged (None)."""
    _, events = await _edit(_turns(REPLACED), [], draft=REPLACED)
    assert [e["draft"] for e in events if e.get("type") == "done"] == [None]
