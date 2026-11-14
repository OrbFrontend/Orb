"""ZONOS3 backend: clip preparation, reference-code cloning, per-line emotion labels and the controls they send."""

import base64
import contextlib
import io
import json
import wave
from pathlib import Path
from types import MappingProxyType

import httpx
import numpy as np
import pytest

from backend.inference.local_ml import GO_EMOTIONS
from backend.inference.local_models.llama_server import manager
from backend.inference.local_models.voice_clip import prepare, separator
from backend.inference.local_models.voice_clip.loudness import integrated_lufs
from backend.inference.local_models.voice_clip.separator import istft, stft
from backend.inference.local_models.zonos_server import Launch, ZonosServerHost
from backend.inference.local_models.zonos_server import host as zonos_host
from backend.workflows.tts import line_emotion
from backend.workflows.tts.engine import zonos_adapter
from backend.workflows.tts.engine.base import SpeakableChunk
from backend.workflows.tts.engine.regex_extractor import regex_extract_lines
from backend.workflows.tts.engine.wav import pcm_to_wav
from backend.workflows.tts.engine.zonos_delivery import (
    DELIVERY_TABLE,
    DOSE_CAPS,
    MAX_STRENGTH,
    VOICE_CONTROLS,
    delivery_controls,
)
from backend.workflows.tts.synth import normalize_profile, synthesize_blocks

_RATE = 44100
CODES = [[(frame * 10 + cb) % 4096 for cb in range(10)] for frame in range(4)]


def _wav(ms: int) -> bytes:
    return pcm_to_wav(b"\x10\x00" * (_RATE * ms // 1000), _RATE)


def _speech_like(seconds: float = 3.0, bed: float = 0.0) -> np.ndarray:
    """Half-second tone bursts with silent gaps, over a noise bed *bed* times the tone's level."""
    t = np.arange(int(_RATE * seconds)) / _RATE
    bursts = 0.3 * np.sin(2 * np.pi * 440 * t) * (np.floor(t * 2) % 2 == 0)
    return (bursts + bed * 0.3 * np.random.default_rng(0).standard_normal(t.size)).astype(np.float32)


def _wav_of(signal: np.ndarray) -> bytes:
    return pcm_to_wav((np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes(), _RATE)


def _decoded(data: bytes) -> tuple[np.ndarray, int, int]:
    with wave.open(io.BytesIO(data)) as handle:
        raw = handle.readframes(handle.getnframes())
        return np.frombuffer(raw, np.int16).astype(np.float32) / 32768, handle.getframerate(), handle.getnchannels()


@pytest.fixture
def server(monkeypatch):
    """A fake patched ZONOS server: records each /v2/generate body and answers with *reply(body)* milliseconds of audio;
    enrolls any clip as ``CODES``. It also stands in for the built-in server, recording each use of it and the host of
    every request."""
    state = {"bodies": [], "clips": [], "hosts": [], "builtin": [], "reply": lambda body: 1500}

    @contextlib.asynccontextmanager
    async def builtin(settings, *, enroll=False):
        state["builtin"].append(enroll)
        yield "http://builtin.test"

    monkeypatch.setattr(zonos_adapter, "zonos_voice_server", builtin)

    def handle(request: httpx.Request) -> httpx.Response:
        state["hosts"].append(request.url.host)
        if request.url.path == "/v2/speakers":
            state["clips"].append(json.loads(request.content))
            return httpx.Response(200, json={"id": "spk_1"})
        if request.url.path == "/v2/speakers/spk_1/reference_codes":
            return httpx.Response(200, json={"id": "spk_1", "codes": CODES})
        body = json.loads(request.content)
        state["bodies"].append(body)
        return httpx.Response(200, content=_wav(state["reply"](body)))

    real = httpx.AsyncClient
    monkeypatch.setattr(zonos_adapter.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    return state


def test_delivery_table_names_real_labels_and_respects_caps():
    assert set(DELIVERY_TABLE) <= {*GO_EMOTIONS, line_emotion.WHISPERED}
    for label in DELIVERY_TABLE:
        for control, dose in delivery_controls(label, MAX_STRENGTH * 10).items():
            assert 0 < dose <= DOSE_CAPS[control]
    assert delivery_controls("anger", 0) == {}
    assert delivery_controls("warm") == {}


@pytest.mark.parametrize(
    ("text", "window"),
    [
        (
            '"Get out of my house," she snapped, slamming the door. He left.',
            '"Get out of my house," she snapped, slamming the door.',
        ),
        ('He stared at the letter. His hands shook. "This can\'t be real."', 'His hands shook. "This can\'t be real."'),
        ('*She sinks to the floor, sobbing.* "I lost him."', 'She sinks to the floor, sobbing. "I lost him."'),
    ],
)
def test_window_reads_the_narration_attached_to_the_line(text, window):
    _, lines = regex_extract_lines(text)
    assert [line.window for line in lines] == [window]


def test_window_stops_at_a_paragraph_break():
    _, lines = regex_extract_lines('She sighed.\n\n"Fine."\n\nShe walked away.')
    assert lines[0].window == '"Fine."'


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        ('She leaned in and whispered, "Come closer."', ["whispered"]),
        ('"Run," she said in a low whisper. He ran.\n\n"Wait!"', ["whispered", "neutral"]),
        ('"Run," she said, and then, after a long while, she whispered.', ["neutral"]),
        ('Then, a hoarse, broken whisper:\n\n"...You\'re going to pay for that."', ["whispered"]),
        ('"Fine." She whispers it. "Go."', ["neutral", "whispered"]),
        ('She sighed. "Fine, have it your way."', ["whispered"]),
        ('*sighs* "Fine."', ["whispered"]),
        ('She caught sight of him. "There you are."', ["neutral"]),
    ],
)
async def test_whisper_near_the_line_wins_without_the_classifier(text, labels):
    _, lines = regex_extract_lines(text)
    assert await line_emotion.line_labels(lines, {"emotion_delivery": True, "language": "vi"}, None) == labels


async def test_labels_are_stored_with_the_chunk_and_replay_without_classifying(client, server, monkeypatch):
    calls = []

    async def classify(texts):
        calls.append(list(texts))
        return [
            {**dict.fromkeys(GO_EMOTIONS, 0.05), "anger": 0.9}
            if "Get out" in text
            else {**dict.fromkeys(GO_EMOTIONS, 0.05), "neutral": 0.9}
            for text in texts
        ]

    monkeypatch.setattr(line_emotion, "classify_emotions", classify)
    monkeypatch.setattr(line_emotion, "local_feature_ready", lambda feature, settings: True)
    profile = normalize_profile({"backend": "zonos", "zonos_reference_codes": CODES, "language": "en-US"})
    text = '"Get out!" she shouted. "The bus is at nine." *She whispers.* "Come closer."'

    _, _, blocks = await synthesize_blocks(text, profile, settings=MappingProxyType({}))

    assert [block["chunk"]["emotion"] for block in blocks] == ["anger", "neutral", "whispered"]
    assert len(calls) == 1 and len(calls[0]) == 4  # the whispered line is not classified
    assert [body.get("controls") for body in server["bodies"]] == [
        delivery_controls("anger"),
        dict(VOICE_CONTROLS),
        delivery_controls("whispered"),
    ]
    assert all(
        body["speaker_reference_codes"] == CODES
        and body["eoa_hold_frames"] == zonos_adapter.EOA_HOLD_FRAMES
        and body["silence_prefill_frames"] == zonos_adapter.SILENCE_PREFILL_FRAMES
        for body in server["bodies"]
    )

    server["bodies"].clear()
    stored = [block["chunk"] for block in blocks] + [{**blocks[1]["chunk"], "emotion": "warm"}]
    await synthesize_blocks(text, profile, speech_chunks=stored)
    assert len(calls) == 1
    assert [body.get("controls") for body in server["bodies"]] == [
        delivery_controls("anger"),
        dict(VOICE_CONTROLS),
        delivery_controls("whispered"),
        dict(VOICE_CONTROLS),
    ]


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        ("Fine.", "Fine"),
        ("I... I don't know...", "I. I don't know"),
        ("Wait… what,", "Wait. what"),
        ("...You're going to pay for that.", "You're going to pay for that"),
        ("So, . . . you came?", "So. you came?"),
        ("Really...?", "Really?"),
        ("No!", "No!"),
        ("I *really* mean it.", "I really mean it"),
        ("*Fine*...", "Fine"),
        ("...", "..."),
    ],
)
async def test_a_line_goes_out_without_ellipses_or_a_closing_period(server, text, sent):
    await zonos_adapter.ZonosAdapter().synthesize(
        [SpeakableChunk(text=text, spoken_text=text)], "cloned", zonos_reference_codes=CODES, settings=MappingProxyType({})
    )

    assert [body["text"] for body in server["bodies"]] == [sent]


async def test_collapsed_line_is_spoken_again_without_controls(server):
    server["reply"] = lambda body: 200 if body.get("controls") else 1500
    chunk = SpeakableChunk(text="I can't do this anymore.", spoken_text="I can't do this anymore.", emotion="sadness")

    result = await zonos_adapter.ZonosAdapter().synthesize(
        [chunk], "cloned", zonos_reference_codes=CODES, settings=MappingProxyType({})
    )

    assert [body.get("controls") for body in server["bodies"]] == [delivery_controls("sadness"), None]
    assert result.duration_ms == 1500


async def test_an_uploaded_clip_becomes_the_characters_zonos_voice(client, server):
    from backend.database import get_workflow_character_state
    from backend.workflows.tts.synth import WORKFLOW_ID

    card_id = await client.create("/api/characters", json={"name": "Cloned"})
    url = f"/api/characters/{card_id}/workflows/tts/upload?backend=zonos&api_url=http://zonos.test:1919"

    await client.post_json(url, files={"file": ("memo.wav", _wav_of(0.05 * _speech_like()), "audio/wav")})

    stored = normalize_profile(await get_workflow_character_state(card_id, WORKFLOW_ID))
    sent, rate, channels = _decoded(base64.b64decode(server["clips"][0]["speaker_audio_base64"]))
    assert (rate, channels) == (44100, 1)
    assert integrated_lufs(sent, rate) == pytest.approx(-20.0, abs=0.2)
    assert stored["zonos_reference_codes"] == CODES
    assert stored["zonos_ref_name"] == "memo.wav"
    assert (stored["backend"], stored["api_url"], stored["enabled"]) == ("zonos", "http://zonos.test:1919", True)

    conv = await client.post_json("/api/conversations", json={"character_card_id": card_id})
    await client.post(
        f"/api/conversations/{conv['id']}/workflows/tts/trigger", json={"action": "clear_voice", "backend": "zonos"}
    )
    stored = normalize_profile(await get_workflow_character_state(card_id, WORKFLOW_ID))
    assert (stored["zonos_reference_codes"], stored["backend"], stored["enabled"]) == ([], "zonos", False)


async def test_a_profile_without_a_server_url_uses_the_builtin_server(client, server):
    from backend.database import get_workflow_character_state
    from backend.workflows.tts.synth import WORKFLOW_ID, synthesize

    card_id = await client.create("/api/characters", json={"name": "Cloned"})
    await client.post_json(
        f"/api/characters/{card_id}/workflows/tts/upload?backend=zonos&api_url=",
        files={"file": ("memo.wav", _wav_of(0.05 * _speech_like()), "audio/wav")},
    )
    stored = normalize_profile(await get_workflow_character_state(card_id, WORKFLOW_ID))
    await synthesize("Hello there.", stored, settings=MappingProxyType({}))
    await synthesize("Hello there.", {**stored, "api_url": "http://zonos.test:1919"}, settings=MappingProxyType({}))

    assert (stored["api_url"], stored["zonos_reference_codes"]) == ("", CODES)
    assert server["builtin"] == [True, False]
    assert server["hosts"] == ["builtin.test", "builtin.test", "builtin.test", "zonos.test"]


async def test_the_host_keeps_one_server_and_restarts_it_for_a_new_launch(monkeypatch):
    spawned = []

    class Child:
        returncode = None

        def terminate(self):
            self.returncode = 0

        kill = terminate

        async def wait(self, timeout):
            return True

        async def aclose(self):
            pass

    async def spawn(argv, sink):
        spawned.append((list(argv), Child()))
        return spawned[-1][1]

    real = httpx.AsyncClient
    answer = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    monkeypatch.setattr(zonos_host.httpx, "AsyncClient", lambda **kw: real(transport=answer, **kw))
    monkeypatch.setattr(zonos_host, "spawn", spawn)
    monkeypatch.setattr(zonos_host.binary, "find_binary", lambda: Path("zonos3-server"))
    host = ZonosServerHost(name="test", idle_timeout=3600)
    speaking = Launch("backbone.gguf", "zac.gguf", "steer.gguf")
    cloning = Launch("backbone.gguf", "zac.gguf", "steer.gguf", "encoder.gguf", "teacher.gguf", gpu=False)

    try:
        async with host.use(speaking) as first:
            pass
        async with host.use(speaking) as again:
            pass
        async with host.use(cloning) as second:
            pass
        assert first is again and second is not first
        assert [child.returncode for _, child in spawned] == [0, None]
        argv = spawned[1][0]
        assert argv[:6] == ["zonos3-server", "backbone.gguf", "--zac", "zac.gguf", "--steer", "steer.gguf"]
        assert argv[6:10] == ["--zac-encoder", "encoder.gguf", "--ssl-teacher", "teacher.gguf"]
        assert "--zac-encoder" not in spawned[0][0] and spawned[0][0][-1] == "--gpu" and argv[-1] == "--cpu"
        await host.release()
        assert (spawned[1][1].returncode, host.state) == (0, "idle")
    finally:
        await host.shutdown()
        manager._HOSTS.remove(host)


async def test_an_upload_with_the_server_down_says_to_start_it(client, monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(zonos_adapter.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(refuse), **kw))
    card_id = await client.create("/api/characters", json={"name": "Cloned"})

    response = await client.post_json(
        f"/api/characters/{card_id}/workflows/tts/upload?backend=zonos&api_url=http://zonos.test:1919",
        files={"file": ("memo.wav", _wav_of(_speech_like()), "audio/wav")},
        expected_status=503,
    )
    assert "http://zonos.test:1919" in response["detail"]


def test_a_clip_with_sound_under_the_voice_is_flagged_and_set_to_the_target_level():
    clean = prepare(_wav_of(_speech_like()), separate=False)
    noisy = prepare(_wav_of(_speech_like(bed=0.05)), separate=False)

    assert (clean.background, noisy.background, noisy.separated) == (False, True, False)
    for clip in (clean, noisy):
        signal, rate, _ = _decoded(clip.wav)
        assert integrated_lufs(signal, rate) == pytest.approx(-20.0, abs=0.2)
        assert np.abs(signal).max() <= 10 ** (-1 / 20) + 1e-4


@pytest.mark.parametrize(("cleaned", "background"), [(True, False), (False, True)])
def test_a_separated_clip_is_measured_again(monkeypatch, cleaned, background):
    voice = _speech_like()
    monkeypatch.setattr(separator, "vocals", lambda signal: voice if cleaned else signal)

    clip = prepare(_wav_of(_speech_like(bed=0.05)), separate=True)

    assert (clip.separated, clip.background) == (True, background)


def test_the_separators_spectrogram_round_trips():
    signal = np.random.default_rng(1).standard_normal((2, 30000))

    assert np.allclose(istft(stft(signal), signal.shape[1]), signal, atol=1e-9)
