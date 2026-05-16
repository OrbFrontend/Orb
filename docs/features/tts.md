# Text-to-Speech (TTS)

Edge TTS is included in the main Python dependencies; cloud and self-hosted backends use the existing HTTP client stack.

## Playback Controls

TTS settings live in the **Settings** panel:

- **Audio/TTS enabled** — global toggle to enable or disable all speech playback
- **Auto-speak** — automatically play speech for new assistant messages
- **Volume** — playback volume level

The speaker icon on each assistant message triggers speech for that message. Playback state is shown on the button itself.

## Character Voice Settings

Each character keeps its own voice profile in the character editor **Voice** tab:

- Enabled/disabled
- Backend/API config
- Language and voice
- Speed and pitch
- Preview playback

## How It Works

Clicking the speaker icon on a character message, or enabling auto-speak for new assistant messages, triggers a three-step pipeline:

1. **Speech Extraction** — the regex extractor extracts spoken dialogue locally, strips inner monologue/scene text, and converts recognized action beats (`*laughs*`, `*sighs*`) into pauses or emotion tags for capable backends. Handles straight (`"..."`) and curly (`\u201c...\u201d`) double quotes.
2. **TTS Synthesis** — the speakable text is sent to the configured backend (Edge TTS, OpenAI-compatible, Fish Speech, ElevenLabs, Kokoro-82M).
3. **Playback** — the generated audio plays in-browser; results are cached on disk so repeated plays are instant.

Clicking a **quoted dialogue line** in an assistant message speaks just that line (click-to-speak). Chunks are fetched on first click and cached in the DOM. The currently spoken line gets a subtle highlight during playback (karaoke mode).

## Available Backends

| Backend | Install | API Key | Voices | Models | Notes |
|---------|---------|---------|--------|--------|-------|
| Microsoft Edge TTS | Included in `requirements.txt` | None (free) | Fetched live, filterable by language | — | 400+ voices, 80+ languages |
| OpenAI-Compatible | None (httpx) | Required | 10 built-in voices (alloy, echo, nova, shimmer...) | Fetched live from `/v1/models` | Works with any provider implementing `POST /v1/audio/speech` |
| Kokoro-82M | See `requirements-tts.txt` | None | 54 voices, 9 languages | — | Self-hosted local model. [hexgrad/kokoro](https://github.com/hexgrad/kokoro) |
| Fish Speech | None (httpx) | Optional | Fetched live from `/v1/references/list` | — | Self-hosted, supports voice cloning via references |
| ElevenLabs | None (httpx) | Required | Fetched live from ElevenLabs API | — | 300+ cloud voices, emotion tags, highest quality |

## Adding New Backends

Each backend is a single file in `backend/tts/` implementing the `TTSAdapter` base class. The router auto-registers adapters whose dependencies are installed (try/except import). See `backend/tts/edge_adapter.py` as a reference.

Key methods to implement:

- `list_voices()` — return available voices (can be static or fetched from API)
- `list_models()` — optional, return available models (for backends with multiple models)
- `synthesize()` — convert speakable chunks into audio bytes
- `backend_name`, `supports_streaming`, `supports_emotion_tags` properties
