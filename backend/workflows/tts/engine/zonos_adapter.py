"""ZONOS3 TTS adapter: voices cloned from a clip's reference codes, with per-line steering controls."""

from __future__ import annotations

import base64
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator

import httpx

from ...toolkit import Settings, WorkflowInputError, WorkflowUnavailableError, get_settings, zonos_voice_server
from .base import SpeakableChunk, SynthesisResult, TTSAdapter
from .wav import pcm_duration_ms, pcm_to_wav, stitch_pcm, strip_header
from .zonos_delivery import VOICE_CONTROLS, delivery_controls

logger = logging.getLogger(__name__)

#: Frames the end-of-audio token is held back, so a line keeps its decay (a patched server's ``eoa_hold_frames``).
EOA_HOLD_FRAMES = 6
#: Codec silence before each line; the server default of 3 lets reference voices start with a loud first syllable.
SILENCE_PREFILL_FRAMES = 10

_SYNTH_TIMEOUT = 300.0
_ENROLL_TIMEOUT = 120.0
_COLLAPSE_WORDS = 4
_COLLAPSE_MS = 600
_CODEBOOKS = 10
_CODEBOOK_SIZE = 4096
_MAX_REFERENCE_FRAMES = 900
# Servers that refused eoa_hold_frames: they get requests without it.
_NO_HOLD: set[str] = set()

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


def clean_reference_codes(raw: object) -> list[list[int]]:
    """Stored reference codes as ``[frames][10]`` codec ids, or ``[]`` when they are not that."""
    if not isinstance(raw, list) or not 0 < len(raw) <= _MAX_REFERENCE_FRAMES:
        return []
    rows = []
    for row in raw:
        if not isinstance(row, list) or len(row) != _CODEBOOKS:
            return []
        if not all(type(code) is int and 0 <= code < _CODEBOOK_SIZE for code in row):
            return []
        rows.append(list(row))
    return rows


@contextlib.asynccontextmanager
async def _server(api_url: str, settings: Settings | None, *, enroll: bool = False) -> AsyncIterator[str]:
    """The base URL to call: *api_url* when one is set, else the built-in server, kept running for the block."""
    external = (api_url or "").strip().rstrip("/")
    if external:
        yield external
        return
    async with zonos_voice_server(settings if settings is not None else await get_settings(), enroll=enroll) as base:
        yield base


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


async def enroll_clip(
    data: bytes, filename: str = "", api_url: str = "", api_key: str | None = None, settings: Settings | None = None
) -> list[list[int]]:
    """Encode a prepared clip into reference codes, on the server at *api_url* or the built-in one when it is empty.

    Raises ``WorkflowInputError`` for a clip the server refuses and ``WorkflowUnavailableError`` when it cannot be reached or
    cannot return codes.
    """
    headers = {**_headers(api_key), "X-TTS-Session-ID": uuid.uuid4().hex}
    body = {"speaker_audio_base64": base64.b64encode(data).decode("ascii"), "speaker_audio_name": filename or None}
    async with _server(api_url, settings, enroll=True) as base:
        try:
            async with httpx.AsyncClient(timeout=_ENROLL_TIMEOUT) as client:
                created = await client.post(f"{base}/v2/speakers", json=body, headers=headers)
                if (refusal := _refusal(created)) is not None:
                    raise WorkflowInputError(f"ZONOS could not read this clip: {refusal}")
                created.raise_for_status()
                speaker_id = created.json()["id"]
                fetched = await client.get(f"{base}/v2/speakers/{speaker_id}/reference_codes", headers=headers)
                if fetched.status_code == 404:
                    raise WorkflowUnavailableError(
                        f"The ZONOS3 server at {base} cannot return reference codes. Update it to a build with "
                        "/v2/speakers/{id}/reference_codes."
                    )
                fetched.raise_for_status()
                codes = clean_reference_codes(fetched.json().get("codes"))
        except httpx.TransportError as exc:
            raise WorkflowUnavailableError(f"Start the ZONOS3 server at {base} first.") from exc
    if not codes:
        raise WorkflowUnavailableError("The ZONOS server returned no voice for this clip.")
    return codes


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
        zonos_reference_codes: list[list[int]] | None = None,
        emotion_delivery: bool = True,
        delivery_strength: float = 1.0,
        seed: int | None = None,
        **kwargs,
    ) -> SynthesisResult:
        """Synthesize each chunk with its line's controls and join the clips into one WAV."""
        codes = clean_reference_codes(zonos_reference_codes)
        if not codes:
            raise ValueError("This character has no ZONOS voice yet — upload a reference clip first.")
        lang, normalize = server_language(language)
        base: dict = {
            "speaker_reference_codes": codes,
            "language": lang,
            "text_normalization": normalize,
            "silence_prefill_frames": SILENCE_PREFILL_FRAMES,
            "stream": False,
            "format": "wav",
        }
        if seed is not None:
            base["seed"] = seed

        async with _server(api_url, kwargs.get("settings")) as server:
            url = f"{server}/v2/generate"
            async with httpx.AsyncClient(timeout=_SYNTH_TIMEOUT) as client:

                async def generate(text: str, controls: dict[str, float]) -> tuple[bytes, int]:
                    body = {**base, "text": text, **({"controls": controls} if controls else {})}
                    if server not in _NO_HOLD:
                        response = await client.post(
                            url, json={**body, "eoa_hold_frames": EOA_HOLD_FRAMES}, headers=_headers(api_key)
                        )
                        if "eoa_hold_frames" not in (_refusal(response) or ""):
                            _raise_for_status(response)
                            return strip_header(response.content)
                        logger.info("ZONOS: %s does not take eoa_hold_frames; line endings may be clipped", server)
                        _NO_HOLD.add(server)
                    response = await client.post(url, json=body, headers=_headers(api_key))
                    _raise_for_status(response)
                    return strip_header(response.content)

                async def speak(chunk: SpeakableChunk) -> tuple[bytes, int]:
                    emotional = delivery_controls(chunk.emotion, delivery_strength) if emotion_delivery else {}
                    controls = emotional or dict(VOICE_CONTROLS)
                    pcm, clip_rate = await generate(chunk.text, controls)
                    if (
                        controls
                        and len(chunk.text.split()) >= _COLLAPSE_WORDS
                        and pcm_duration_ms(pcm, clip_rate) < _COLLAPSE_MS
                    ):
                        logger.info(
                            "ZONOS: %s collapsed a %d-word line; speaking it without controls",
                            controls,
                            len(chunk.text.split()),
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
