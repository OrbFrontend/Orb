"""Wiring tests: constant lorebook entries ride the cached system prefix.

Drives the real prefix-assembly seam (``_build_prefixes``) with a
``PipelineContext`` carrying one constant and one keyword entry, and asserts
the split: the constant entry lands as a byte-identical ``## Lorebook`` section
in both the writer and agent prefixes (KV cache Invariant 1), while the
trailing block excludes it.
"""

from __future__ import annotations

from backend.features.lorebook import compute_lorebook_injection_block
from backend.pipeline.context import PipelineContext, _build_prefixes

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


def _ctx(*, agent_system_prompt=None) -> PipelineContext:
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
        lorebook_entries=[_CONSTANT, _KEYWORD],
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
