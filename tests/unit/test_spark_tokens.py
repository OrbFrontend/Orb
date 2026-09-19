"""Tests for the Spark-TTS token contract."""

from __future__ import annotations

import pytest

from backend.inference.local_models.spark_tts import tokens


def test_the_two_audio_families_are_contiguous_and_adjacent():
    """The two token families use adjacent ranges."""
    assert tokens.SEMANTIC_LAST == tokens.SEMANTIC_BASE + tokens.SEMANTIC_COUNT - 1 == 163952
    assert tokens.SEMANTIC_BASE == tokens.GLOBAL_BASE + tokens.GLOBAL_COUNT


def test_clone_prompt_is_the_exact_id_sequence():
    """The cloning prompt has the expected ids and structure."""
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
    # The prompt ends after the global speaker block.
    assert tokens.START_SEMANTIC not in prompt


def test_clone_prompt_rejects_a_malformed_voice_before_it_reaches_the_model():
    with pytest.raises(tokens.InvalidSpeakerTokens):
        tokens.clone_prompt([1], list(range(31)))
    with pytest.raises(tokens.InvalidSpeakerTokens):
        tokens.clone_prompt([1], [4096] * 32)


@pytest.mark.parametrize(
    "raw",
    [None, "not a list", list(range(31)), list(range(33)), [0.5] * 32, [True] * 32, [-1] * 32, [4096] * 32],
)
def test_validate_rejects(raw):
    with pytest.raises(tokens.InvalidSpeakerTokens):
        tokens.validate_speaker_tokens(raw)


def test_validate_accepts_the_edges_of_the_codebook():
    assert tokens.validate_speaker_tokens([0] * 32) == [0] * 32
    assert tokens.validate_speaker_tokens((4095,) * 32) == [4095] * 32


def test_semantic_indices_drops_everything_outside_its_range():
    """Only semantic codebook ids are returned."""
    generated = [
        tokens.START_SEMANTIC,
        tokens.SEMANTIC_BASE,
        tokens.SEMANTIC_LAST,
        tokens.GLOBAL_BASE,
        151643,  # <|endoftext|>
    ]
    assert tokens.semantic_indices(generated) == [0, 8191]


def test_token_budget_bounds_a_degenerate_generation():
    """Generation stays within the configured floor and ceiling."""
    assert tokens.token_budget("") == tokens.TOKEN_FLOOR
    assert tokens.token_budget("x" * 10) == tokens.TOKEN_FLOOR + 80
    assert tokens.token_budget("x" * 10_000) == tokens.TOKEN_CEILING


def test_an_advanced_prompt_continues_into_the_reference_speech():
    """The excerpt follows the speaker block as speech already under way."""
    speaker = list(range(32))
    prompt = tokens.clone_prompt([9001, 9002], speaker, [0, 7, 8191])
    plain = tokens.clone_prompt([9001, 9002], speaker)
    assert prompt[: len(plain)] == plain
    assert prompt[len(plain) :] == [
        165151,  # <|start_semantic_token|>
        155761,  # <|bicodec_semantic_0|>
        155768,
        163952,  # <|bicodec_semantic_8191|>
    ]


@pytest.mark.parametrize("raw", [None, [], [8192], [-1], [0.5], [True], list(range(tokens.MAX_REFERENCE_TOKENS + 1))])
def test_validate_reference_rejects(raw):
    with pytest.raises(tokens.InvalidReferenceTokens):
        tokens.validate_reference_tokens(raw)


def test_a_malformed_reference_never_reaches_the_prompt():
    with pytest.raises(tokens.InvalidReferenceTokens):
        tokens.clone_prompt([1], list(range(32)), [8192])


def test_the_reference_ceiling_covers_the_longest_excerpt():
    """An excerpt is at most TARGET_SECONDS plus its edges; the ceiling must hold it."""
    from backend.inference.local_models.spark_tts import reference

    longest = (reference.TARGET_SECONDS + 2 * reference.EDGE_MS / 1000) * tokens.SEMANTIC_RATE
    assert longest <= tokens.MAX_REFERENCE_TOKENS


@pytest.mark.parametrize(
    ("said", "line", "joined"),
    [
        ("One must know it.", "Hello there.", "One must know it. Hello there."),
        ("本节目由燕之屋冠名播出。", "你好。", "本节目由燕之屋冠名播出。你好。"),
        ("One must know it.", "你好。", "One must know it.你好。"),
        ("  padded  ", "  line ", "padded line"),
        ("", "line", "line"),
        ("said", "", "said"),
    ],
)
def test_join_content_spaces_words_but_not_cjk(said, line, joined):
    assert tokens.join_content(said, line) == joined


def test_the_context_holds_the_longest_advanced_prompt():
    """Reference, transcript and a line at the generation ceiling fit together."""
    from backend.inference.local_models.spark_tts import config

    assert config.CTX_SIZE >= tokens.TOKEN_CEILING + 1096 + tokens.MAX_REFERENCE_TOKENS + tokens.MAX_REFERENCE_TEXT
