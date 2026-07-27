"""Character Card Spec V3 ingest: the `ccv3` chunk, V3-only field parking, and
the lorebook semantics Orb can act on (`use_regex`, `selective`/`secondary_keys`,
decorator stripping).

Before this, a card declaring ``spec: "chara_card_v3"`` fell through to the V1
parser and silently lost its character_book, tags, alternate_greetings and
extensions.
"""

from __future__ import annotations

import base64
import json

import pytest
from PIL import Image, PngImagePlugin

from backend.api.deps import _normalise_lorebook_entry, lorebook_to_book
from backend.features.cards.parsing import card_to_dict, parse, to_png
from backend.inference.lorebook import select_keyword_entries


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode("ascii")


def _v3(**data) -> dict:
    return {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "Amy", "first_mes": "hi", **data}}


def _png(tmp_path, name="card.png", **chunks) -> str:
    info = PngImagePlugin.PngInfo()
    for k, v in chunks.items():
        info.add_text(k, v)
    path = tmp_path / name
    Image.new("RGBA", (8, 8)).save(path, format="PNG", pnginfo=info)
    return str(path)


_BOOK = {
    "name": "marvel",
    "entries": [{"keys": ["doom"], "content": "Victor von Doom.", "use_regex": True, "selective": True}],
}


# ── Ingest ────────────────────────────────────────────────────────────────────


def test_v3_card_keeps_everything_v1_used_to_drop(tmp_path):
    payload = _v3(
        character_book=_BOOK,
        alternate_greetings=["alt"],
        tags=["fictional"],
        system_prompt="be brief",
        post_history_instructions="stay in character",
        extensions={"world": "marvel"},
    )
    d = card_to_dict(parse(_png(tmp_path, ccv3=_b64(payload))))

    assert d["source_format"] == "tavern_v3"
    assert len(d["character_book"]["entries"]) == 1
    assert d["alternate_greetings"] == ["alt"]
    assert d["tags"] == ["fictional"]
    assert d["system_prompt"] == "be brief"
    assert d["post_history_instructions"] == "stay in character"
    assert d["extensions"]["world"] == "marvel"


def test_ccv3_wins_over_a_disagreeing_chara(tmp_path):
    """A downgraded V2 projection in `chara` must not shadow the V3 payload."""
    v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "Amy", "first_mes": "hi"}}
    path = _png(tmp_path, ccv3=_b64(_v3(character_book=_BOOK)), chara=_b64(v2))

    assert card_to_dict(parse(path))["character_book"]["name"] == "marvel"


def test_chara_only_card_still_parses(tmp_path):
    v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "Amy", "tags": ["t"]}}
    d = card_to_dict(parse(_png(tmp_path, chara=_b64(v2))))
    assert (d["source_format"], d["tags"]) == ("tavern_v2", ["t"])


def test_malformed_ccv3_falls_back_to_chara(tmp_path):
    v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "Amy", "tags": ["kept"]}}
    d = card_to_dict(parse(_png(tmp_path, ccv3="not base64 json!!", chara=_b64(v2))))
    assert d["tags"] == ["kept"]


def test_missing_both_chunks_raises(tmp_path):
    with pytest.raises(ValueError, match="missing 'chara' field"):
        parse(_png(tmp_path))


def test_odd_spec_version_does_not_degrade_to_v1(tmp_path):
    """spec_version is not a Literal — a cosmetic mismatch must not lose the card."""
    payload = _v3(tags=["kept"])
    payload["spec_version"] = "3.0.0"
    assert card_to_dict(parse(_png(tmp_path, ccv3=_b64(payload))))["tags"] == ["kept"]


# ── V3-only fields park at extensions.orb.v3 ─────────────────────────────────


def test_v3_only_fields_park_and_round_trip(tmp_path):
    payload = _v3(
        nickname="Ames",
        source=["https://example.test/card"],
        group_only_greetings=["hey all"],
        character_book=_BOOK,
        extensions={"orb": {"fragments": [{"n": 1}]}, "third_party": 1},
    )
    d = card_to_dict(parse(_png(tmp_path, ccv3=_b64(payload))))

    assert d["extensions"]["orb"]["v3"] == {
        "nickname": "Ames",
        "source": ["https://example.test/card"],
        "group_only_greetings": ["hey all"],
    }
    # The parking slot sits beside the existing orb.fragments, not on top of it.
    assert d["extensions"]["orb"]["fragments"] == [{"n": 1}]
    assert d["extensions"]["third_party"] == 1

    out = tmp_path / "export.png"
    out.write_bytes(to_png(d))
    back = card_to_dict(parse(str(out)))

    assert back["source_format"] == "tavern_v3"
    assert back["extensions"] == d["extensions"]
    assert back["character_book"]["entries"][0]["keys"] == ["doom"]


def test_exported_chara_chunk_still_parses_as_v2(tmp_path):
    d = card_to_dict(parse(_png(tmp_path, ccv3=_b64(_v3(nickname="Ames", tags=["t"])))))
    out = tmp_path / "export.png"
    out.write_bytes(to_png(d))

    chara = json.loads(base64.b64decode(Image.open(out).info["chara"]))
    assert chara["spec"] == "chara_card_v2"
    assert chara["data"]["tags"] == ["t"]
    # V3-only fields stay out of the V2 projection's top level.
    assert "nickname" not in chara["data"]


# ── Lorebook normalisation ────────────────────────────────────────────────────


def test_blanket_selective_without_secondary_keys_is_not_honoured():
    """The reported card sets selective+use_regex on all 55 entries with no
    secondary_keys — taken literally the whole book would match nothing."""
    e = _normalise_lorebook_entry({"keys": ["doom"], "content": "x", "use_regex": True, "selective": True})
    assert e["selective"] is False
    assert e["use_regex"] is True

    assert select_keyword_entries([{"content": "enter doom"}], [{**e, "keywords": e["keywords"]}])


def test_selective_with_secondary_keys_is_honoured():
    e = _normalise_lorebook_entry({"keys": ["doom"], "selective": True, "secondary_keys": ["latveria"]})
    assert (e["selective"], e["secondary_keys"]) == (True, ["latveria"])


def test_insertion_order_becomes_sort_order():
    assert _normalise_lorebook_entry({"keys": ["a"], "insertion_order": 7})["sort_order"] == 7
    assert _normalise_lorebook_entry({"keys": ["a"]})["sort_order"] == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@@depth 4\nBody text.", "Body text."),
        ("@@depth 4\n@@role assistant\n\nBody text.", "Body text."),
        ("@@depth 4\n@@@fallback\nBody.", "Body."),
        ("Body only.", "Body only."),
        ("Body with @@ inside.\nMore.", "Body with @@ inside.\nMore."),
        ("Body.\n", "Body.\n"),  # no decorators -> content untouched, trailing newline kept
    ],
)
def test_decorators_are_stripped_from_content(raw, expected):
    assert _normalise_lorebook_entry({"keys": ["a"], "content": raw})["content"] == expected


def test_export_emits_the_v3_entry_fields():
    row = {
        "keywords": ["doom"],
        "content": "x",
        "enabled": 1,
        "sort_order": 3,
        "case_insensitive": 1,
        "constant": 0,
        "name": "Doom",
        "priority": 100,
        "id": 1,
        "use_regex": 1,
        "selective": 1,
        "secondary_keys": ["latveria"],
    }
    entry = lorebook_to_book("marvel", [row])["entries"][0]
    assert (entry["use_regex"], entry["selective"], entry["secondary_keys"]) == (True, True, ["latveria"])
    # Round trip back through the importer.
    assert _normalise_lorebook_entry(entry)["secondary_keys"] == ["latveria"]
