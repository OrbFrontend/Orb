"""Unit tests for agentic lorebook activation.

Covers the direct_scene ``active_lorebook`` parameter, the Director catalog, the
selection/rendering helpers, and keyword-scan parity after the renderer refactor.
"""

from __future__ import annotations

from backend.inference import (
    build_lorebook_catalog,
    compute_agentic_lorebook_block,
    compute_lorebook_injection_block,
    render_lorebook_block,
)
from backend.inference import build_direct_scene_tool


def _entry(
    name,
    content="",
    keywords=None,
    *,
    constant=False,
    priority=100,
    world_name="World",
    case_insensitive=True,
):
    return {
        "name": name,
        "content": content or f"{name} content",
        "keywords": keywords or [],
        "case_insensitive": case_insensitive,
        "constant": constant,
        "priority": priority,
        "world_name": world_name,
    }


# ── build_direct_scene_tool: active_lorebook arg ─────────────────────────────


class TestDirectSceneActiveLorebookArg:
    def test_absent_by_default(self):
        props = build_direct_scene_tool([])["function"]["parameters"]["properties"]
        assert "selected_lorebook_entries" not in props

    def test_absent_when_false(self):
        props = build_direct_scene_tool([], agentic_lorebook=False)["function"]["parameters"]["properties"]
        assert "selected_lorebook_entries" not in props

    def test_present_when_true(self):
        props = build_direct_scene_tool([], agentic_lorebook=True)["function"]["parameters"]["properties"]
        assert "selected_lorebook_entries" in props
        assert props["selected_lorebook_entries"]["type"] == "array"
        assert props["selected_lorebook_entries"]["items"] == {"type": "string"}

    def test_optional_not_required(self):
        tool = build_direct_scene_tool([], agentic_lorebook=True)
        assert "selected_lorebook_entries" not in tool["function"]["parameters"]["required"]

    def test_moods_and_fragments_unaffected(self):
        frags = [{"id": "kw", "field_type": "array", "description": "d", "required": False}]
        props = build_direct_scene_tool(frags, agentic_lorebook=True)["function"]["parameters"]["properties"]
        assert "moods" in props and "kw" in props and "selected_lorebook_entries" in props

    def test_byte_stable_for_identical_input(self):
        import json

        a = build_direct_scene_tool([], agentic_lorebook=True)
        b = build_direct_scene_tool([], agentic_lorebook=True)
        assert json.dumps(a, sort_keys=False) == json.dumps(b, sort_keys=False)


# ── compute_agentic_lorebook_block ───────────────────────────────────────────


class TestComputeAgenticLorebookBlock:
    def test_constants_always_included(self):
        entries = [_entry("Const", constant=True), _entry("Other")]
        block = compute_agentic_lorebook_block(entries, [])
        assert "Const: Const content" in block
        assert "Other" not in block

    def test_name_match(self):
        entries = [_entry("Dragon"), _entry("Castle")]
        block = compute_agentic_lorebook_block(entries, ["Dragon"])
        assert "Dragon: Dragon content" in block
        assert "Castle" not in block

    def test_name_match_case_insensitive_and_trimmed(self):
        entries = [_entry("Dragon")]
        block = compute_agentic_lorebook_block(entries, ["  dRaGoN "])
        assert "Dragon: Dragon content" in block

    def test_unknown_names_ignored(self):
        entries = [_entry("Dragon")]
        assert compute_agentic_lorebook_block(entries, ["Nonexistent"]) == ""

    def test_duplicate_names_activate_all(self):
        entries = [_entry("Dup", content="A"), _entry("Dup", content="B")]
        block = compute_agentic_lorebook_block(entries, ["Dup"])
        assert "Dup: A" in block and "Dup: B" in block

    def test_constant_and_selected_not_duplicated(self):
        block = compute_agentic_lorebook_block([_entry("Both", constant=True)], ["Both"])
        assert block.count("Both: Both content") == 1

    def test_empty_entries(self):
        assert compute_agentic_lorebook_block([], ["x"]) == ""

    def test_no_selection_no_constants_is_empty(self):
        assert compute_agentic_lorebook_block([_entry("A")], []) == ""

    def test_priority_sort_desc(self):
        entries = [_entry("Low", priority=10), _entry("High", priority=200)]
        block = compute_agentic_lorebook_block(entries, ["Low", "High"])
        assert block.index("High") < block.index("Low")

    def test_substring_scan_activates_in_parallel(self):
        # Director overlooks "Natlan", but the keyword scan catches it.
        entries = [_entry("Natlan", keywords=["Natlan"])]
        msgs = [{"role": "user", "content": "Tell me about Natlan."}]
        block = compute_agentic_lorebook_block(entries, [], messages=msgs)
        assert "Natlan: Natlan content" in block

    def test_substring_scan_unions_with_director(self):
        entries = [_entry("Dragon"), _entry("Natlan", keywords=["natlan"])]
        msgs = [{"role": "user", "content": "We travel to Natlan."}]
        block = compute_agentic_lorebook_block(entries, ["Dragon"], messages=msgs)
        assert "Dragon: Dragon content" in block
        assert "Natlan: Natlan content" in block

    def test_substring_and_director_not_duplicated(self):
        entries = [_entry("Natlan", keywords=["Natlan"])]
        msgs = [{"role": "user", "content": "Natlan again."}]
        block = compute_agentic_lorebook_block(entries, ["Natlan"], messages=msgs)
        assert block.count("Natlan: Natlan content") == 1

    def test_substring_no_match_without_messages(self):
        entries = [_entry("Natlan", keywords=["Natlan"])]
        assert compute_agentic_lorebook_block(entries, []) == ""

    def test_substring_scan_limited_to_current_turn(self):
        # The keyword appears only in older history, not in the current turn
        # (last assistant + user), so the fallback must not activate it.
        entries = [_entry("Natlan", keywords=["Natlan"])]
        msgs = [
            {"role": "user", "content": "We arrive in Natlan."},
            {"role": "assistant", "content": "The city greets you."},
            {"role": "user", "content": "Let's keep going."},
        ]
        assert compute_agentic_lorebook_block(entries, [], messages=msgs) == ""


# ── build_lorebook_catalog ───────────────────────────────────────────────────


class TestBuildLorebookCatalog:
    def test_header_present(self):
        cat = build_lorebook_catalog([_entry("A", keywords=["a"])])
        assert cat.startswith("**Available Lorebook Entries**")
        assert "selected_lorebook_entries" in cat

    def test_excludes_constants(self):
        entries = [
            _entry("Const", constant=True, keywords=["k"]),
            _entry("Var", keywords=["v"]),
        ]
        cat = build_lorebook_catalog(entries)
        assert "Const" not in cat
        assert "- [Var] — v" in cat

    def test_empty_when_only_constants(self):
        assert build_lorebook_catalog([_entry("C", constant=True)]) == ""

    def test_empty_when_no_entries(self):
        assert build_lorebook_catalog([]) == ""

    def test_keywords_joined(self):
        cat = build_lorebook_catalog([_entry("A", keywords=["k1", "k2", "k3"])])
        assert "- [A] — k1, k2, k3" in cat

    def test_entry_without_keywords_has_no_dash(self):
        cat = build_lorebook_catalog([_entry("Solo", keywords=[])])
        assert "- [Solo]" in cat
        assert "- [Solo] —" not in cat

    def test_grouped_by_world_in_first_appearance_order(self):
        entries = [
            _entry("A", keywords=["a"], world_name="Avatar"),
            _entry("C", keywords=["c"], world_name="Other"),
            _entry("B", keywords=["b"], world_name="Avatar"),
        ]
        cat = build_lorebook_catalog(entries)
        assert "### Avatar" in cat and "### Other" in cat
        assert cat.index("### Avatar") < cat.index("### Other")
        # Both Avatar entries fall under the single Avatar heading.
        assert cat.index("- [A]") < cat.index("### Other")
        assert cat.index("- [B]") < cat.index("### Other")


# ── keyword-scan parity after the renderer refactor ──────────────────────────


class TestKeywordScanParity:
    def test_constant_always_included(self):
        msgs = [{"role": "user", "content": "hello"}]
        entries = [_entry("Const", constant=True), _entry("Var", keywords=["sword"])]
        block = compute_lorebook_injection_block(msgs, entries)
        assert "Const: Const content" in block
        assert "Var" not in block

    def test_keyword_match(self):
        msgs = [{"role": "user", "content": "I draw my sword"}]
        block = compute_lorebook_injection_block(msgs, [_entry("Var", keywords=["sword"])])
        assert "Var: Var content" in block

    def test_case_insensitive_match(self):
        msgs = [{"role": "user", "content": "A SWORD"}]
        entries = [_entry("Var", keywords=["sword"], case_insensitive=True)]
        assert "Var" in compute_lorebook_injection_block(msgs, entries)

    def test_no_match_returns_empty(self):
        msgs = [{"role": "user", "content": "nothing relevant here"}]
        assert compute_lorebook_injection_block(msgs, [_entry("Var", keywords=["sword"])]) == ""

    def test_priority_sort_desc(self):
        msgs = [{"role": "user", "content": "sword castle"}]
        entries = [
            _entry("Low", keywords=["sword"], priority=10),
            _entry("High", keywords=["castle"], priority=200),
        ]
        block = compute_lorebook_injection_block(msgs, entries)
        assert block.index("High") < block.index("Low")

    def test_block_starts_with_header(self):
        msgs = [{"role": "user", "content": "sword"}]
        block = compute_lorebook_injection_block(msgs, [_entry("Var", keywords=["sword"])])
        assert block.startswith("**Lorebook**")

    def test_renderer_matches_keyword_path_on_same_set(self):
        # The shared render_lorebook_block reproduces the keyword-scan output
        # exactly for the same matched entry set.
        msgs = [{"role": "user", "content": "sword"}]
        entries = [_entry("Var", keywords=["sword"])]
        assert compute_lorebook_injection_block(msgs, entries) == render_lorebook_block([entries[0]])
