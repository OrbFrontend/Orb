"""The sheet updater's output contract and prompt.

The sibling of ``test_public_profile_draft.py``, pinning the same three things:
what the drafter accepts, what it refuses, and what its one call carries. The
refusals are the interesting half — this call proposes a replacement for the
text the model was handed, so "returned what it was given" and "returned an
essay" are failures rather than merely poor answers.
"""

from __future__ import annotations

import pytest

from backend.features.cards.sheet_update import (
    MAX_SHEET_GROWTH_CHARS,
    MAX_SUMMARY_WORDS,
    MIN_SHEET_CEILING_CHARS,
    SHEET_TOOL_NAME,
    SheetUpdateUnavailable,
    propose_sheet_update,
    sheet_reply_budget,
)

SHEET = "A scout of the Watch, tall and green-cloaked.\n\nPersonality: Terse."
TRANSCRIPT = "User: What now?\n\nAria: She cut her hair to the scalp and threw the cloak on the fire."


class _FakeClient:
    """Yields one forced-call ``done`` message, the way ``LLMClient`` does."""

    def __init__(self, message: dict) -> None:
        self.message = message
        self.calls: list[dict] = []

    async def complete(self, *, messages, model, tools, tool_choice, **params):
        self.calls.append({"messages": messages, "model": model, "tools": tools, "tool_choice": tool_choice, **params})
        yield {"type": "done", "message": self.message}


def _call(name: str = SHEET_TOOL_NAME, **arguments) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


async def _propose(message: dict, *, sheet: str = SHEET):
    return await propose_sheet_update(
        _FakeClient(message),  # type: ignore[arg-type]
        "m",
        member_name="Aria",
        sheet=sheet,
        transcript=TRANSCRIPT,
    )


# ── Accepted ────────────────────────────────────────────────────────────────


async def test_a_reported_change_round_trips_stripped():
    update = await _propose(_call(changed=True, sheet="  A shorn scout of the Watch.  ", summary=" Hair cut, cloak burned "))
    assert update == {"sheet": "A shorn scout of the Watch.", "summary": "Hair cut, cloak burned"}


async def test_no_change_is_the_cheap_answer_and_stages_nothing():
    """The common case, and the one that must be easy to express — a model with
    a tool it has to call will invent a change to fill it otherwise."""
    assert await _propose(_call(changed=False)) is None
    # Not even when it fills the fields anyway: `changed` is the decision.
    assert await _propose(_call(changed=False, sheet="Something else.", summary="Drift.")) is None


async def test_a_missing_summary_costs_a_label_not_the_proposal():
    """The review row shows both sheets in full, so the summary is a
    convenience — losing the whole proposal over it would be the wrong trade."""
    update = await _propose(_call(changed=True, sheet="A shorn scout."))
    assert update is not None and update["summary"] == ""


async def test_an_over_long_summary_is_trimmed_rather_than_refused():
    words = " ".join(f"w{i}" for i in range(MAX_SUMMARY_WORDS + 10))
    update = await _propose(_call(changed=True, sheet="A shorn scout.", summary=words))
    assert update is not None and len(update["summary"].split(" ")) == MAX_SUMMARY_WORDS


# ── Refused ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("arguments", "why"),
    [
        ({"changed": True}, "returned no sheet"),
        ({"changed": True, "sheet": "   "}, "returned an empty sheet"),
        ({"changed": True, "sheet": "A scout named {{char}}."}, "contains a macro"),
        ({"changed": True, "sheet": "x" * (MIN_SHEET_CEILING_CHARS + 1)}, "longer than"),
        ({"changed": True, "sheet": SHEET}, "proposed the sheet it was given"),
    ],
)
async def test_a_proposal_that_fails_the_contract_is_unavailable(arguments, why):
    with pytest.raises(SheetUpdateUnavailable, match=why):
        await _propose(_call(**arguments))


async def test_a_whitespace_only_difference_is_still_a_no_op():
    """Otherwise every exchange could stage a review row that changes nothing but
    the line wrapping, and the queue stops being worth reading."""
    with pytest.raises(SheetUpdateUnavailable, match="proposed the sheet it was given"):
        await _propose(_call(changed=True, sheet=SHEET.replace("\n\n", "\n   \n")))


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "assistant", "content": "Nothing changed."},
        _call(name="some_other_tool", changed=True, sheet="A shorn scout."),
    ],
)
async def test_an_answer_with_no_usable_call_is_unavailable(message):
    with pytest.raises(SheetUpdateUnavailable, match="did not return a usable sheet update"):
        await _propose(message)


# ── The call ────────────────────────────────────────────────────────────────


async def test_the_call_is_forced_and_not_at_the_writing_preset():
    """A roleplay preset at temperature 1.15 would embellish the sheet this call
    was asked to preserve; the hyperparameters are hardcoded for the same reason
    the profile drafter's are."""
    client = _FakeClient(_call(changed=False))
    await propose_sheet_update(client, "m", member_name="Aria", sheet=SHEET, transcript=TRANSCRIPT)  # type: ignore[arg-type]
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "function", "function": {"name": SHEET_TOOL_NAME}}
    assert call["temperature"] == 0.2 and call["max_tokens"] == sheet_reply_budget(SHEET)


async def test_the_reply_budget_can_always_restate_the_sheet_it_was_given():
    """The call has to reproduce a whole sheet verbatim, so a *flat* `max_tokens`
    is a truncation budget on any card longer than it — and a truncated sheet is
    the one bad output the contract cannot catch: non-empty, brace-free, under
    the ceiling and different from the base. The budget therefore tracks the
    same ceiling `_clean_sheet` enforces."""
    long_sheet = "x" * 6000
    client = _FakeClient(_call(changed=False))
    await propose_sheet_update(client, "m", member_name="Aria", sheet=long_sheet, transcript=TRANSCRIPT)  # type: ignore[arg-type]
    budget = client.calls[0]["max_tokens"]
    assert budget > sheet_reply_budget(SHEET)
    # Enough room for the longest proposal this sheet is allowed to grow into,
    # at a deliberately pessimistic three characters per token.
    ceiling = max(MIN_SHEET_CEILING_CHARS, len(long_sheet) + MAX_SHEET_GROWTH_CHARS)
    assert budget >= ceiling / 3

