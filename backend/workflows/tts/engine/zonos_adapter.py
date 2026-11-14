"""ZONOS3 TTS adapter: cloned speaker embeddings with per-line steering controls."""

from __future__ import annotations

import base64
import logging
import math
import struct
import uuid

import httpx

from ...toolkit import WorkflowInputError, WorkflowUnavailableError
from .base import SpeakableChunk, SynthesisResult, TTSAdapter
from .wav import pcm_duration_ms, pcm_to_wav, stitch_pcm, strip_header
from .zonos_delivery import delivery_controls

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://localhost:1919"
TARGET_LUFS = -20.0

_SYNTH_TIMEOUT = 300.0
_ENROLL_TIMEOUT = 120.0
_COLLAPSE_WORDS = 4
_COLLAPSE_MS = 600
_MAX_EMBEDDING_DIM = 1024

# Orb language prefix -> server text-normalization language; checked in order, so `en-gb` precedes `en`.
_LANGUAGES = (
    ("en-gb", "en_gb"),
    ("en", "en_us"),
    ("fr", "fr_fr"),
    ("de", "de"),
    ("es", "es"),
    ("it", "it"),
    ("pt", "pt_br"),
    ("ja", "ja"),
    ("zh", "cmn"),
    ("cmn", "cmn"),
    ("ko", "ko"),
)


def server_language(language: str) -> tuple[str, bool]:
    """The server's language code for an Orb language, and whether to normalize text in it."""
    code = (language or "en").strip().lower().replace("_", "-")
    for prefix, server in _LANGUAGES:
        if code == prefix or code.startswith(prefix + "-"):
            return server, True
    return "en_us", False


def clean_embedding(raw: object) -> list[float]:
    """A stored speaker embedding as finite floats, or ``[]`` when it is not one."""
    if not isinstance(raw, list) or not 0 < len(raw) <= _MAX_EMBEDDING_DIM:
        return []
    values = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            return []
        values.append(float(value))
    return values


def embedding_npy(embedding: list[float]) -> bytes:
    """*embedding* as a float32 ``.npy`` file, the form the server reads."""
    header = f"{{'descr': '<f4', 'fortran_order': False, 'shape': ({len(embedding)},), }}"
    header += " " * (63 - (10 + len(header)) % 64) + "\n"
    return (
        b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header.encode() + struct.pack(f"<{len(embedding)}f", *embedding)
    )


def _base_url(api_url: str) -> str:
    return (api_url or DEFAULT_API_URL).rstrip("/")


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _refusal(response: httpx.Response) -> str | None:
    """What a 4xx says the server refused; ``None`` for any other status."""
    if not 400 <= response.status_code < 500:
        return None
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return str(detail or response.status_code)


def _raise_for_status(response: httpx.Response) -> None:
    """A 4xx names what the server refused, which the user can act on; anything else stays an HTTP error."""
    refusal = _refusal(response)
    if refusal is not None:
        raise ValueError(f"ZONOS server refused the request: {refusal}")
    response.raise_for_status()


async def enroll_clip(data: bytes, filename: str = "", api_url: str = "", api_key: str | None = None) -> list[float]:
    """Encode an uploaded clip into a speaker embedding on the ZONOS server.

    Raises ``WorkflowInputError`` for a clip the server refuses and ``WorkflowUnavailableError`` when it cannot be reached.
    """
    base = _base_url(api_url)
    headers = {**_headers(api_key), "X-TTS-Session-ID": uuid.uuid4().hex}
    body = {"speaker_audio_base64": base64.b64encode(data).decode("ascii"), "speaker_audio_name": filename or None}
    try:
        async with httpx.AsyncClient(timeout=_ENROLL_TIMEOUT) as client:
            created = await client.post(f"{base}/v2/speakers", json=body, headers=headers)
            if (refusal := _refusal(created)) is not None:
                raise WorkflowInputError(f"ZONOS could not read this clip: {refusal}")
            created.raise_for_status()
            speaker_id = created.json()["id"]
            fetched = await client.get(f"{base}/v2/speakers/{speaker_id}/embedding", headers=headers)
            fetched.raise_for_status()
            embedding = clean_embedding(fetched.json().get("embedding"))
    except httpx.TransportError as exc:
        raise WorkflowUnavailableError(f"Start the ZONOS3 server at {base} first.") from exc
    if not embedding:
        raise WorkflowUnavailableError("The ZONOS server returned no voice for this clip.")
    return embedding


class ZonosAdapter(TTSAdapter):
    """TTS adapter for a ZONOS3 server's native ``/v2`` API."""

    async def synthesize(
        self,
        chunks: list[SpeakableChunk],
        voice_id: str,
        language: str = "en-US",
        rate: float = 1.0,
        pitch: float = 1.0,
        api_url: str = "",
        api_key: str | None = None,
        zonos_embedding: list[float] | None = None,
        emotion_delivery: bool = True,
        delivery_strength: float = 1.0,
        seed: int | None = None,
        **kwargs,
    ) -> SynthesisResult:
        """Synthesize each chunk with its line's controls and join the clips into one WAV."""
        embedding = clean_embedding(zonos_embedding)
        if not embedding:
            raise ValueError("This character has no ZONOS voice yet — upload a reference clip first.")
        lang, normalize = server_language(language)
        url = f"{_base_url(api_url)}/v2/generate"
        base: dict = {
            "speaker_embedding_base64": base64.b64encode(embedding_npy(embedding)).decode("ascii"),
            "speaker_mode": "embedding",
            "language": lang,
            "text_normalization": normalize,
            "condition_values": {"loudness": TARGET_LUFS},
            "stream": False,
            "format": "wav",
        }
        if seed is not None:
            base["seed"] = seed

        async with httpx.AsyncClient(timeout=_SYNTH_TIMEOUT) as client:

            async def generate(text: str, controls: dict[str, float]) -> tuple[bytes, int]:
                body = {**base, "text": text, **({"controls": controls} if controls else {})}
                response = await client.post(url, json=body, headers=_headers(api_key))
                _raise_for_status(response)
                return strip_header(response.content)

            async def speak(chunk: SpeakableChunk) -> tuple[bytes, int]:
                controls = delivery_controls(chunk.emotion, delivery_strength) if emotion_delivery else {}
                pcm, clip_rate = await generate(chunk.text, controls)
                if controls and len(chunk.text.split()) >= _COLLAPSE_WORDS and pcm_duration_ms(pcm, clip_rate) < _COLLAPSE_MS:
                    logger.info(
                        "ZONOS: %s collapsed a %d-word line; speaking it without controls", controls, len(chunk.text.split())
                    )
                    pcm, clip_rate = await generate(chunk.text, {})
                return pcm, clip_rate

            raw_pcm, sample_rate = await stitch_pcm(chunks, speak, fallback_rate=44100)
        if not raw_pcm:
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")

        logger.info(
            "ZONOS: %d chunks -> %d bytes (emotion=%s)",
            sum(bool(chunk.text.strip()) for chunk in chunks),
            len(raw_pcm),
            ",".join(chunk.emotion for chunk in chunks),
        )
        return SynthesisResult(
            audio_bytes=pcm_to_wav(raw_pcm, sample_rate),
            content_type="audio/wav",
            duration_ms=pcm_duration_ms(raw_pcm, sample_rate),
        )

    async def list_voices(self, language: str = "", **kwargs) -> list[dict]:
        return []

    @property
    def backend_name(self) -> str:
        return "ZONOS3"
