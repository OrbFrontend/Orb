"""The Spark-TTS token contract, which is silent when it is wrong.

An off-by-one in a base constant does not raise. It shifts every speaker token
by one codebook entry and the model speaks in a voice that is almost, but not,
the one the user enrolled — so the exact id sequence is pinned here rather than
described.

The constants themselves came from the checkpoint's own tokenizer.json:

    <|bicodec_global_0|>   151665      <|bicodec_global_4095|>   155760
    <|bicodec_semantic_0|> 155761      <|bicodec_semantic_8191|> 163952
"""

from __future__ import annotations

import pytest

from backend.inference.local_models.spark_tts import tokens


def test_the_two_audio_families_are_contiguous_and_adjacent():
    """The whole reason this module does arithmetic instead of regex."""
    assert tokens.GLOBAL_LAST == tokens.GLOBAL_BASE + tokens.GLOBAL_COUNT - 1 == 155760
    assert tokens.SEMANTIC_LAST == tokens.SEMANTIC_BASE + tokens.SEMANTIC_COUNT - 1 == 163952
    assert tokens.SEMANTIC_BASE == tokens.GLOBAL_LAST + 1


def test_clone_prompt_is_the_exact_id_sequence():
    """Structure AND values, for a known (text, speaker) pair."""
    speaker = list(range(32))
    prompt = tokens.clone_prompt([9001, 9002, 9003], speaker)
    assert prompt == [
        165137,  # <|task_tts|>
        165146,  # <|start_content|>
        9001,
        9002,
        9003,
        165152,  # <|end_content|>
        165150,  # <|start_global_token|>
        *range(151665, 151665 + 32),  # <|bicodec_global_0..31|>
        165156,  # <|end_global_token|>
    ]
    # No semantic block, and no trailing <|start_semantic_token|>: this is the
    # global-only path, and adding either would be the in-context path.
    assert tokens.START_SEMANTIC not in prompt


def test_clone_prompt_rejects_a_malformed_voice_before_it_reaches_the_model():
    with pytest.raises(tokens.InvalidSpeakerTokens):
        tokens.clone_prompt([1], list(range(31)))
    with pytest.raises(tokens.InvalidSpeakerTokens):
        tokens.clone_prompt([1], [4096] * 32)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not a list",
        list(range(31)),
        list(range(33)),
        [0.5] * 32,
        [True] * 32,  # bool is an int in Python and would index the codebook at 1
        [-1] * 32,
        [4096] * 32,
    ],
)
def test_validate_rejects(raw):
    assert not tokens.looks_like_speaker_tokens(raw)


def test_validate_accepts_the_edges_of_the_codebook():
    assert tokens.validate_speaker_tokens([0] * 32) == [0] * 32
    assert tokens.validate_speaker_tokens((4095,) * 32) == [4095] * 32


def test_semantic_indices_drops_everything_outside_its_range():
    """A generation opens with <|start_semantic_token|> and may end with EOS;
    neither is a codebook index, and passing one to the decoder is a crash."""
    generated = [
        tokens.START_SEMANTIC,
        tokens.SEMANTIC_BASE,
        tokens.SEMANTIC_LAST,
        tokens.GLOBAL_BASE,
        151643,  # <|endoftext|>
    ]
    assert tokens.semantic_indices(generated) == [0, 8191]
    assert tokens.global_indices(generated) == [0]


def test_token_budget_bounds_a_degenerate_generation():
    """Upstream passes a flat 3000, so a line that needs eleven seconds can cost
    two and a half minutes on CPU before it fails."""
    assert tokens.token_budget("") == tokens.TOKEN_FLOOR
    assert tokens.token_budget("x" * 10) == tokens.TOKEN_FLOOR + 80
    assert tokens.token_budget("x" * 10_000) == tokens.TOKEN_CEILING
