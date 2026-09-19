"""Decoding Whisper token ids back into text."""

from __future__ import annotations

import json

from backend.inference.local_models.whisper.vocab import Vocabulary

# Byte-level BPE spellings: "Ġ" is a space, and "ä½" + "ł" is the three UTF-8
# bytes of 你 split across two tokens.
_TOKENS = {"Hello": 0, "Ġthere": 1, ".": 2, "ä½": 3, "ł": 4, "<|endoftext|>": 5}


def test_decode_reads_spaces_and_joins_split_characters():
    vocab = Vocabulary(_TOKENS)
    assert vocab.decode([0, 1, 2]) == "Hello there."
    assert vocab.decode([3, 4]) == "你"


def test_decode_skips_what_it_is_told_to_and_what_it_does_not_know():
    vocab = Vocabulary(_TOKENS)
    assert vocab.decode([0, 5, 50364, 1], skip=(5,)) == "Hello there"


def test_a_character_cut_off_mid_way_does_not_raise():
    assert Vocabulary(_TOKENS).decode([3]) == "�"


def test_the_ids_are_the_text_vocabulary(tmp_path):
    path = tmp_path / "vocab.json"
    path.write_text(json.dumps(_TOKENS), encoding="utf-8")
    assert Vocabulary.load(str(path)).ids == frozenset(range(6))
