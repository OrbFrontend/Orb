from backend.pipeline.passes.editor.editor import apply_patches


def test_non_dict_patch_element_skipped():
    # The guard exists for runtime input the type system forbids, so the bad
    # element is deliberately the wrong type here.
    patches = [{"search": "foo", "replace": "bar"}, "junk"]
    draft, errors = apply_patches("draft foo", patches)  # type: ignore[arg-type]
    assert draft == "draft bar"
    assert errors == []


# ── Boundary-marker reconciliation ────────────────────────────────────────────
# The audit report's splitter eats a trailing closing marker (quote, emphasis *)
# off a sentence but keeps the opening one, so a search copied from the report is
# missing/extra a marker versus the draft. Match on the marker-stripped core and
# leave the draft's own surrounding markers in place.


def test_dangling_leading_quote_preserves_dialogue_quotes():
    # search carries a spurious leading `"` (its closing quote was split off) and
    # replace has none — the draft's quotes must survive, not go dangling.
    draft = '"Do not mistake my compliance for vulnerability." She remains still.'
    patch = {
        "search": '"Do not mistake my compliance for vulnerability.',
        "replace": "Do not confuse my stillness for weakness.",
    }
    out, errors = apply_patches(draft, [patch])
    assert out == '"Do not confuse my stillness for weakness." She remains still.'
    assert errors == []


def test_missing_trailing_asterisk_not_doubled():
    # search omits the trailing `*` the draft has; replace re-adds it. Result must
    # not end in `**`.
    draft = '"I\'m fine," *Akane mumbles, her voice dropping an octave.*'
    patch = {
        "search": 'fine," *Akane mumbles, her voice dropping an octave.',
        "replace": 'fine," *Akane mumbles, barely audible.*',
    }
    out, errors = apply_patches(draft, [patch])
    assert out == '"I\'m fine," *Akane mumbles, barely audible.*'
    assert errors == []


def test_balanced_dialogue_replace_keeps_quotes():
    draft = 'He said "Hello there." loudly.'
    out, errors = apply_patches(draft, [{"search": '"Hello there."', "replace": '"Hi."'}])
    assert out == 'He said "Hi." loudly.'
    assert errors == []


def test_ambiguous_core_falls_through_to_marked_span():
    # The core "He smiled." occurs twice (bare + emphasized); the marked search
    # must still uniquely target the emphasized span.
    draft = "He smiled. Then *He smiled.* again."
    out, errors = apply_patches(draft, [{"search": "*He smiled.*", "replace": "*He grinned.*"}])
    assert out == "He smiled. Then *He grinned.* again."
    assert errors == []


def test_emphasis_removal_still_applies():
    # search/replace differ only by the emphasis markers — the edit strips them.
    draft = "She was *very* sure."
    out, errors = apply_patches(draft, [{"search": "*very*", "replace": "very"}])
    assert out == "She was very sure."
    assert errors == []
