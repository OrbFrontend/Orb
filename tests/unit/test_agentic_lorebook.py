"""Unit tests for lorebook activation.

Covers the direct_scene ``selected_lorebook_entries`` parameter, the Director
catalog, the unified three-source selection core (``select_active_entries`` and
its two named wrappers), macro resolution, the ``LorebookTurn`` per-turn bundle,
activation gating, and keyword-scan parity.
"""

from __future__ import annotations

import logging

from backend.features.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    LOREBOOK_SCAN_DEPTH,
    agentic_lorebook_active,
    build_lorebook_catalog,
    compute_agentic_lorebook_block,
    compute_constant_lorebook_block,
    compute_lorebook_block,
    compute_lorebook_injection_block,
    render_lorebook_block,
    select_active_entries,
    select_keyword_entries,
)
from backend.inference import (
    TOOLS,
    CachedBase,
    build_direct_scene_tool,
    build_lorebook_select_prompt,
)
from backend.pipeline import LorebookTurn
from backend.pipeline.passes.director import lorebook_select_step
from backend.pipeline.passes.writer import build_writer_content


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


# ── direct_scene never carries lorebook (decoupled) ──────────────────────────


class TestDirectSceneNoLorebookArg:
    def test_absent_for_empty_fragments(self):
        props = build_direct_scene_tool([])["function"]["parameters"]["properties"]
        assert "selected_lorebook_entries" not in props
        assert "moods" in props


# ── select_lorebook tool: the standalone selection schema ────────────────────


class TestSelectLorebookTool:
    def test_schema_exposes_param(self):
        props = TOOLS["select_lorebook"]["schema"]["function"]["parameters"]["properties"]
        assert "selected_lorebook_entries" in props
        assert props["selected_lorebook_entries"]["type"] == "array"
        assert props["selected_lorebook_entries"]["items"] == {"type": "string"}


# ── compute_agentic_lorebook_block ───────────────────────────────────────────


class TestComputeAgenticLorebookBlock:
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

    def test_director_pick_naming_constant_stays_excluded(self):
        # A pick that names a constant entry must not duplicate it into the
        # trailing block — the entry already rides the system prefix.
        assert compute_agentic_lorebook_block([_entry("Both", constant=True)], ["Both"]) == ""

    def test_no_selection_is_empty(self):
        assert compute_agentic_lorebook_block([_entry("A")], []) == ""

    def test_priority_sort_desc(self):
        entries = [_entry("Low", priority=10), _entry("High", priority=200)]
        block = compute_agentic_lorebook_block(entries, ["Low", "High"])
        assert block.index("High") < block.index("Low")

    def test_render_order_stable_under_input_permutation(self):
        # Equal priority: order must come from sort_order/id, not input order,
        # so a fixed active set renders byte-identically across turns (KV cache).
        a = {**_entry("Raiden"), "id": 1, "sort_order": 0}
        b = {**_entry("Inazuma"), "id": 2, "sort_order": 0}
        c = {**_entry("Yae"), "id": 3, "sort_order": 0}
        first = render_lorebook_block([a, b, c])
        second = render_lorebook_block([b, c, a])  # permuted input
        assert first == second
        assert first.index("Raiden") < first.index("Inazuma") < first.index("Yae")

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
    def test_constant_excluded_keyword_still_matches(self):
        msgs = [{"role": "user", "content": "I draw my sword"}]
        entries = [_entry("Const", constant=True), _entry("Var", keywords=["sword"])]
        block = compute_lorebook_injection_block(msgs, entries)
        assert "Const" not in block
        assert "Var: Var content" in block

    def test_case_insensitive_match(self):
        msgs = [{"role": "user", "content": "A SWORD"}]
        entries = [_entry("Var", keywords=["sword"], case_insensitive=True)]
        assert "Var" in compute_lorebook_injection_block(msgs, entries)

    def test_case_sensitive_no_match(self):
        msgs = [{"role": "user", "content": "i draw my sword"}]
        entries = [_entry("Var", keywords=["Sword"], case_insensitive=False)]
        assert compute_lorebook_injection_block(msgs, entries) == ""

    def test_no_match_returns_empty(self):
        msgs = [{"role": "user", "content": "nothing relevant here"}]
        assert compute_lorebook_injection_block(msgs, [_entry("Var", keywords=["sword"])]) == ""

    def test_renderer_matches_keyword_path_on_same_set(self):
        # The shared render_lorebook_block reproduces the keyword-scan output
        # exactly for the same matched entry set.
        msgs = [{"role": "user", "content": "sword"}]
        entries = [_entry("Var", keywords=["sword"])]
        assert compute_lorebook_injection_block(msgs, entries) == render_lorebook_block([entries[0]])


# ── render_lorebook_block: macro resolution ──────────────────────────────────


class TestRenderMacros:
    def test_name_and_content_resolved(self):
        class _Upper:
            def resolve_message(self, text):
                return text.upper()

        block = render_lorebook_block([_entry("name", content="body")], _Upper())
        assert "NAME: BODY" in block


# ── compute_constant_lorebook_block: the system-prefix section ───────────────


class TestComputeConstantLorebookBlock:
    def test_only_constants_included(self):
        entries = [_entry("Const", constant=True), _entry("Var", keywords=["v"])]
        block = compute_constant_lorebook_block(entries)
        assert "Const: Const content" in block
        assert "Var" not in block

    def test_empty_when_no_constants(self):
        assert compute_constant_lorebook_block([_entry("Var", keywords=["v"])]) == ""
        assert compute_constant_lorebook_block([]) == ""

    def test_byte_stable_under_input_permutation(self):
        # The prefix section must render byte-identically across turns
        # regardless of input order (KV cache).
        a = {**_entry("Raiden", constant=True), "id": 1, "sort_order": 0}
        b = {**_entry("Inazuma", constant=True), "id": 2, "sort_order": 0}
        c = {**_entry("Yae", constant=True), "id": 3, "sort_order": 0}
        assert compute_constant_lorebook_block([a, b, c]) == compute_constant_lorebook_block([b, c, a])

    def test_macros_resolved(self):
        class _Upper:
            def resolve_message(self, text):
                return text.upper()

        block = compute_constant_lorebook_block([_entry("name", content="body", constant=True)], _Upper())
        assert "NAME: BODY" in block


# ── constants-only pool: trailing block stays empty ──────────────────────────


class TestConstantsOnlyTrailing:
    _entries = [_entry("Const", constant=True)]

    def test_wrappers_return_empty(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert compute_lorebook_injection_block(msgs, self._entries) == ""
        assert compute_agentic_lorebook_block(self._entries, ["Const"], None, msgs) == ""

    def test_writer_block_empty_and_no_separator(self):
        lt = LorebookTurn(entries=self._entries, messages=[], agentic=True)
        block = lt.writer_block(["Const"])
        assert block == ""
        # An empty block must not leave a stray ___ separator in the writer content.
        content = build_writer_content(block, "", False, "hi", None, None)
        assert content == "___\n\nhi\n\n"


# ── select_active_entries: the unified three-source core ─────────────────────


class TestSelectActiveEntries:
    def test_substring_equivalence(self):
        # With no director picks at depth 6, the unified core selects exactly the
        # same set (same objects) as the standalone keyword scan.
        msgs = [{"role": "user", "content": "a sword in the castle"}]
        entries = [
            _entry("Const", constant=True),
            _entry("Sword", keywords=["sword"]),
            _entry("Unmatched", keywords=["dragon"]),
        ]
        assert select_active_entries(entries, msgs, scan_depth=LOREBOOK_SCAN_DEPTH) == select_keyword_entries(msgs, entries)

    def test_agentic_union_matches_wrapper(self):
        entries = [_entry("Dragon"), _entry("Natlan", keywords=["natlan"])]
        msgs = [{"role": "user", "content": "we travel to natlan"}]
        core = compute_lorebook_block(entries, msgs, scan_depth=AGENTIC_LOREBOOK_SCAN_DEPTH, director_selected=["Dragon"])
        assert core == compute_agentic_lorebook_block(entries, ["Dragon"], None, msgs)


# ── Catalog delimiters on Director picks ─────────────────────────────────────


class TestDirectorPickDelimiters:
    """The catalog renders ``- [Name] — kw``; models copy the brackets too.

    ``.strip()`` removes whitespace and not delimiters, so before this a correct
    relevance judgment arriving as ``[The Ashen Seal]`` activated nothing,
    injected no lore, and logged nothing.
    """

    _entries = [_entry("The Ashen Seal", keywords=["wax seal", "raven crest"])]

    def _names(self, pick):
        return [e["name"] for e in select_active_entries(self._entries, [], scan_depth=2, director_selected=[pick])]

    def test_bracketed_pick_activates_the_entry(self):
        assert self._names("[The Ashen Seal]") == ["The Ashen Seal"]

    def test_bare_and_folded_picks_still_activate(self):
        assert self._names("The Ashen Seal") == ["The Ashen Seal"]
        assert self._names("  the ashen seal  ") == ["The Ashen Seal"]

    def test_appended_catalog_metadata_is_not_undone(self):
        # The explicit decision: a pick carrying the catalog row's keywords is a
        # model copying the whole line, not a name. It stays unmatched so the
        # prompt defect is visible instead of guessed through.
        assert self._names("[The Ashen Seal] — wax seal") == []
        assert self._names("The Ashen Seal — wax seal") == []

    def test_only_a_matched_outer_pair_is_stripped(self):
        # Two delimited names in one string: the leading bracket closes early, so
        # nothing is unwrapped and the malformed pick matches nothing.
        entries = [*self._entries, _entry("Captain Ilyra")]
        picked = select_active_entries(entries, [], scan_depth=2, director_selected=["[The Ashen Seal] and [Captain Ilyra]"])
        assert picked == []

    def test_a_name_that_contains_brackets_is_matched_as_stored(self):
        # Unconditional bracket deletion would make this name unreachable; only
        # the pick is unwrapped, so both the bare and the wrapped form land.
        entries = [_entry("[Redacted] File")]
        for pick in ("[Redacted] File", "[[Redacted] File]"):
            picked = select_active_entries(entries, [], scan_depth=2, director_selected=[pick])
            assert [e["name"] for e in picked] == ["[Redacted] File"]

    def test_a_recovered_pick_is_logged_as_a_warning(self, caplog):
        # That warning count is the per-model rate of this failure.
        with caplog.at_level(logging.WARNING, logger="backend.inference.lorebook"):
            assert self._names("[The Ashen Seal]") == ["The Ashen Seal"]
        assert "matched only after stripping catalog delimiters" in caplog.text

    def test_a_clean_pick_logs_nothing(self, caplog):
        with caplog.at_level(logging.INFO, logger="backend.inference.lorebook"):
            assert self._names("The Ashen Seal") == ["The Ashen Seal"]
        assert caplog.text == ""

    def test_a_pick_naming_a_constant_entry_stays_silent(self, caplog):
        # Constant entries ride the cached prefix; excluding them here is by
        # design, so it must not read as a failed pick.
        entries = [_entry("Const", constant=True)]
        with caplog.at_level(logging.INFO, logger="backend.inference.lorebook"):
            assert select_active_entries(entries, [], scan_depth=2, director_selected=["[Const]"]) == []
        assert caplog.text == ""

    def test_the_block_renders_from_a_bracketed_pick(self):
        block = compute_agentic_lorebook_block(self._entries, ["[The Ashen Seal]"])
        assert "The Ashen Seal: The Ashen Seal content" in block


# ── LorebookTurn ──────────────────────────────────────────────────────────────


class TestLorebookTurn:
    def test_scan_depth_by_mode(self):
        assert LorebookTurn(entries=(), messages=(), agentic=False).scan_depth == LOREBOOK_SCAN_DEPTH
        assert LorebookTurn(entries=(), messages=(), agentic=True).scan_depth == AGENTIC_LOREBOOK_SCAN_DEPTH

    def test_substring_writer_block_reuses_block_verbatim(self):
        # In substring mode the writer block is the pre-computed Director-facing
        # block; director_selected is ignored and nothing is recomputed.
        lt = LorebookTurn(
            entries=[_entry("X", keywords=["x"])],
            messages=[{"role": "user", "content": "x"}],
            agentic=False,
            block="**Lorebook**\n\nFixed: value",
        )
        assert lt.writer_block(["anything"]) == "**Lorebook**\n\nFixed: value"

    def test_agentic_writer_block_matches_compute_agentic(self):
        entries = [_entry("Dragon"), _entry("Natlan", keywords=["natlan"])]
        msgs = [{"role": "user", "content": "go to natlan"}]
        lt = LorebookTurn(entries=entries, messages=msgs, agentic=True)
        assert lt.writer_block(["Dragon"]) == compute_agentic_lorebook_block(entries, ["Dragon"], None, msgs)


# ── agentic_lorebook_active: gating ──────────────────────────────────────────


class TestAgenticLorebookActive:
    _on = {"agentic_lorebook_enabled": 1}

    def test_enabled_when_all_conditions_met(self):
        assert agentic_lorebook_active(self._on, [_entry("A")], agent_on=True)

    def test_disabled_when_flag_off(self):
        assert not agentic_lorebook_active({}, [_entry("A")], agent_on=True)

    def test_disabled_when_agent_off(self):
        assert not agentic_lorebook_active(self._on, [_entry("A")], agent_on=False)

    def test_disabled_when_only_constants(self):
        assert not agentic_lorebook_active(self._on, [_entry("C", constant=True)], agent_on=True)


# ── lorebook_select_step + build_lorebook_select_prompt ───────────────────────


class _FakeSelectBase:
    """Stands in for ``CachedBase``: serves one canned ``select_lorebook`` completion."""

    prefix: list = []
    complete_into = CachedBase.complete_into

    def __init__(self, args: dict):
        self._args = args

    async def complete(self, *_args, **_kwargs):
        import json

        yield {
            "type": "done",
            "message": {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "select_lorebook", "arguments": json.dumps(self._args)}}],
            },
        }


class _FakeClient:
    is_aborted = False


def test_select_prompt_includes_catalog_and_user_message():
    # The pending user message must ride the prompt: during the director pass it is not
    # yet in the shared history, so without it the model can't judge scene relevance.
    out = build_lorebook_select_prompt("THE CATALOG", "WHAT THE USER ASKED", reasoning_on=False)
    assert "THE CATALOG" in out
    assert "WHAT THE USER ASKED" in out
    assert "select_lorebook" in out


async def test_select_step_extracts_names():
    base = _FakeSelectBase({"selected_lorebook_entries": ["Dragon", "Castle"]})
    events = [e async for e in lorebook_select_step(_FakeClient(), base, settings={}, catalog="cat", user_message="hi")]  # type: ignore[arg-type]
    result = events[-1]["result"]
    assert result.selected == ["Dragon", "Castle"]
    assert result.calls and result.calls[0]["name"] == "select_lorebook"


async def test_select_step_empty_catalog_skips():
    # No catalog → no call, empty selection (deterministic lorebook still applies downstream).
    base = _FakeSelectBase({"selected_lorebook_entries": ["X"]})
    events = [e async for e in lorebook_select_step(_FakeClient(), base, settings={}, catalog="", user_message="hi")]  # type: ignore[arg-type]
    assert events[-1]["result"].selected == []
