"""Wiring tests: constant lorebook entries ride the cached system prefix.

Drives the real prefix-assembly seam (``_build_prefixes``) with a
``PipelineContext`` carrying one constant and one keyword entry, and asserts
the split: the constant entry lands as a byte-identical ``## Lorebook`` section
in both the writer and agent prefixes (KV cache Invariant 1), while the
trailing block excludes it.

Plus the ``at_depth`` (``@ Depth``) opt-out: such an entry leaves the
prefix for the per-turn tail block, where its inline macros re-roll every turn.
"""

from __future__ import annotations

from backend.core import Macros
from backend.features.lorebook import (
    compute_depth_lorebook_block,
    compute_lorebook_injection_block,
)
from backend.pipeline.context import PipelineContext, _build_prefixes
from backend.pipeline.passes.writer import build_writer_content

_CONSTANT = {
    "id": 1,
    "name": "Canon",
    "content": "The moon is shattered.",
    "keywords": [],
    "case_insensitive": True,
    "constant": 1,
    "priority": 100,
    "sort_order": 0,
    "world_name": "World",
}
_KEYWORD = {
    "id": 2,
    "name": "Sword",
    "content": "A legendary blade.",
    "keywords": ["sword"],
    "case_insensitive": True,
    "constant": 0,
    "priority": 100,
    "sort_order": 0,
    "world_name": "World",
}


_AT_DEPTH = {
    "id": 3,
    "name": "Dice",
    "content": "Pool: {{roll::1d100}}.",
    "keywords": [],
    "case_insensitive": True,
    "constant": 1,
    "at_depth": 1,
    "priority": 100,
    "sort_order": 0,
    "world_name": "World",
}


def _ctx(*, agent_system_prompt=None, entries=None) -> PipelineContext:
    return PipelineContext(
        settings={"user_name": "User"},
        conv={
            "id": "conv-1",
            "character_name": "Aria",
            "character_scenario": "A quiet harbor town.",
            "post_history_instructions": "",
        },
        card=None,
        director={},
        mood_fragments=[],
        interactive_fragments=[],
        phrase_bank=[],
        lorebook_entries=[_CONSTANT, _KEYWORD] if entries is None else entries,
        client=None,
        system_prompt="You are an assistant.",
        char_persona="Aria is a sailor.",
        mes_example="",
        active_persona=None,
        agent_client=None,
        agent_system_prompt=agent_system_prompt,
    )


def _system_body(prefix: list) -> str:
    assert prefix[0]["role"] == "system"
    return prefix[0]["content"]


def test_constant_section_in_prefix_between_persona_and_scenario():
    prefix, agent_prefix = _build_prefixes(_ctx(), [])
    body = _system_body(prefix)
    assert agent_prefix is None
    assert "## Lorebook\n\nCanon: The moon is shattered." in body
    assert body.index("Aria is a sailor.") < body.index("## Lorebook") < body.index("## Scenario")


def test_keyword_entry_not_in_prefix():
    prefix, _ = _build_prefixes(_ctx(), [])
    assert "Sword" not in _system_body(prefix)


def test_writer_and_agent_prefixes_carry_identical_section():
    # Dual-model mode: only the base system prompt differs; the constant
    # lorebook section must be byte-identical in both prefixes.
    prefix, agent_prefix = _build_prefixes(_ctx(agent_system_prompt="You are a director."), [])
    section = "## Lorebook\n\nCanon: The moon is shattered."
    assert agent_prefix is not None
    assert section in _system_body(prefix)
    assert section in _system_body(agent_prefix)


def test_trailing_block_excludes_constant():
    msgs = [{"role": "user", "content": "I draw my sword"}]
    block = compute_lorebook_injection_block(msgs, [_CONSTANT, _KEYWORD])
    assert "Sword: A legendary blade." in block
    assert "Canon" not in block


# ── at_depth (@ Depth) ───────────────────────────────────────────────────────


def test_at_depth_constant_leaves_the_prefix():
    entries = [_CONSTANT, _KEYWORD, _AT_DEPTH]
    prefix, agent_prefix = _build_prefixes(_ctx(agent_system_prompt="You are a director.", entries=entries), [])
    assert agent_prefix is not None
    for body in (_system_body(prefix), _system_body(agent_prefix)):
        assert "Canon: The moon is shattered." in body  # plain constant still rides it
        assert "Dice" not in body


def test_depth_block_holds_only_at_depth_constants():
    block = compute_depth_lorebook_block([_CONSTANT, _KEYWORD, _AT_DEPTH], Macros("User", "Aria", seed="conv-1"))
    assert block.startswith("**Lorebook (Depth)**")
    assert "Dice: Pool: " in block
    assert "Canon" not in block and "Sword" not in block


def test_at_depth_entry_never_reaches_the_keyword_block():
    # It is `constant`, so the trailing keyword/director block must skip it even
    # when a message happens to mention it — no double injection.
    msgs = [{"role": "user", "content": "roll the Dice"}]
    assert "Dice" not in compute_lorebook_injection_block(msgs, [_AT_DEPTH])


def test_depth_block_sits_after_the_user_message_in_the_writer_tail():
    # Depth 0: last thing the model reads before generating.
    content = build_writer_content("", "", {}, "I attack", None, None, depth_block="**Lorebook (Depth)**\n\nDice: 7")
    assert content == "___\n\nI attack\n\n___\n\n**Lorebook (Depth)**\n\nDice: 7\n\n"


def test_depth_block_rerolls_every_turn_where_the_prefix_freezes():
    # The seeded prefix path is byte-stable per conversation (KV cache); the depth
    # path unseeds, which is the only reason {{roll}} is usable at all.
    macros = Macros("User", "Aria", seed="conv-1")
    assert len({compute_depth_lorebook_block([_AT_DEPTH], macros) for _ in range(20)}) > 1
    frozen = dict(_AT_DEPTH, at_depth=0)
    assert len({_system_body(_build_prefixes(_ctx(entries=[frozen]), [])[0]) for _ in range(20)}) == 1
