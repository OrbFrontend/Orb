"""Tests for how Spark-TTS synthesis drives the model, with a fake llama-server."""

from __future__ import annotations

import contextlib

import pytest

from backend.inference.local_models.spark_tts import config, service, tokens
from backend.inference.local_models.spark_tts.reference import Reference

VALID = list(range(32))
EXCERPT = [5, 6, 7]


class FakeServer:
    """Answers each generate call with the next scripted ``(tokens, stopped)``."""

    def __init__(self, *answers: tuple[list[int], bool]) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.tokenized: list[str] = []

    async def tokenize(self, text: str, *, parse_special: bool = False) -> list[int]:
        assert parse_special is False, "user text must never become control tokens"
        self.tokenized.append(text)
        return [9000 + len(word) for word in text.split()]

    async def generate_tokens(self, prompt, **kwargs):
        self.calls.append({"prompt": list(prompt), **kwargs})
        return self.answers.pop(0)


@pytest.fixture
def fake(monkeypatch):
    def install(*answers):
        server = FakeServer(*answers)

        @contextlib.asynccontextmanager
        async def use(_profile):
            yield server

        monkeypatch.setattr(service.HOST, "use", use)
        monkeypatch.setattr(config, "launch_profile", lambda gpu=True: object())
        monkeypatch.setattr(service.codec, "decode", lambda semantic, speaker: bytes(2 * len(semantic)))
        return server

    return install


def _speech(*indexes: int) -> list[int]:
    return [tokens.SEMANTIC_BASE + i for i in indexes]


async def test_a_basic_voice_is_one_unconstrained_generation(fake):
    server = fake(([tokens.START_SEMANTIC, *_speech(1, 2)], True))
    pcm, rate = await service.synthesize("Hello there.", VALID)
    assert len(pcm) == 4 and rate == 16000
    assert len(server.calls) == 1
    call = server.calls[0]
    assert "banned" not in call
    assert call["prompt"][-1] == tokens.END_GLOBAL
    assert server.tokenized == ["Hello there."]


async def test_an_advanced_voice_cannot_end_at_its_first_token(fake):
    """Measured: after a slow speaker's excerpt, top-k left some lines ending at
    token one every time. The first token is sampled with ending banned; the
    rest continues from it, unconstrained."""
    server = fake((_speech(3), False), (_speech(4, 5), True))
    pcm, _rate = await service.synthesize("Hello there.", VALID, reference=Reference("One must know it.", EXCERPT))

    first, rest = server.calls
    assert first["banned"] == tokens.STOP_TOKENS
    assert first["n_predict"] == 1
    assert first["prompt"][-len(EXCERPT) - 1 :] == [tokens.START_SEMANTIC, *_speech(*EXCERPT)]
    assert "banned" not in rest
    assert rest["prompt"] == first["prompt"] + _speech(3)
    assert rest["n_predict"] == tokens.token_budget("Hello there.") - 1
    assert len(pcm) == 2 * 3  # the first token is part of the line
    # The transcript is read with the line, as one piece of content.
    assert server.tokenized == ["One must know it. Hello there."]


async def test_the_budget_counts_the_line_not_the_transcript(fake):
    server = fake((_speech(1), False), (_speech(2), True))
    await service.synthesize("Hi.", VALID, reference=Reference("A long transcript " * 10, EXCERPT))
    assert 1 + server.calls[1]["n_predict"] == tokens.token_budget("Hi.")


async def test_a_reference_without_a_transcript_is_refused(fake):
    fake()
    with pytest.raises(service.SynthesisFailed, match="no transcript"):
        await service.synthesize("Hi.", VALID, reference=Reference("  ", EXCERPT))


async def test_no_audio_tokens_is_a_synthesis_failure(fake):
    fake(([151645], True))
    with pytest.raises(service.SynthesisFailed, match="no audio tokens"):
        await service.synthesize("Hi.", VALID)
