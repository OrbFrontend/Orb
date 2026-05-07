# Text-to-Speech (TTS)

Edge TTS is included in the main Python dependencies; cloud and self-hosted backends use the existing HTTP client stack.

## Voice Sidepanel

The **Voice** sidepanel controls global playback behavior:

- Volume
- Auto-speak new assistant messages
- Algorithmic speech extraction status
- Now-playing progress
- Extracted speech debug preview

Regex extraction is the only extraction path in this PR. It does not call an LLM and preserves action beats/nonverbal tags when the selected backend supports them.

The extracted speech debug preview shows the latest text sent into the TTS backend. It is collapsed by default so long messages do not take over the panel.

## Character Voice Settings

Each character keeps its own voice profile in the character editor **Voice** tab:

- Enabled/disabled
- Backend/API config
- Language and voice
- Speed and pitch
- Preview playback
- Optional character-specific speech instructions

Character-specific speech instructions are reserved for future expressive extraction and backend-specific pronunciation/style hints. The current algorithmic extractor does not call an LLM.

## How It Works

Clicking the speaker icon on a character message, or enabling auto-speak for new assistant messages, triggers a three-step pipeline:

1. **Speech Extraction** — the regex extractor extracts spoken dialogue locally, strips inner monologue/scene text, and converts recognized action beats (`*laughs*`, `*sighs*`) into pauses or emotion tags for capable backends.
2. **TTS Synthesis** — the speakable text is sent to the configured backend (Edge TTS, OpenAI, Fish Speech, ElevenLabs).
3. **Playback** — the generated audio plays in-browser; results are cached on disk so repeated plays are instant.

## Available Backends

| Backend | Install | API Key | Voices | Models | Notes |
|---------|---------|---------|--------|--------|-------|
| Microsoft Edge TTS | Included in `requirements.txt` | None (free) | Fetched live, filterable by language | — | 400+ voices, 80+ languages |
| OpenAI TTS (and compatible) | None (httpx) | Required | 10 built-in voices (alloy, echo, nova, shimmer...) | Fetched live from `/v1/models` | Works with any provider implementing `POST /v1/audio/speech` |
| Fish Speech | None (httpx) | Optional | Fetched live from `/v1/references/list` | — | Self-hosted, supports voice cloning via references. Default: `http://localhost:8080` |
| ElevenLabs | None (httpx) | Required | Fetched live from ElevenLabs API | — | 300+ cloud voices, emotion tags, highest quality |

## Adding New Backends

Each backend is a single file in `backend/tts/` implementing the `TTSAdapter` base class. The router auto-registers adapters whose dependencies are installed (try/except import). See `backend/tts/edge_adapter.py` as a reference.

Key methods to implement:

- `list_voices()` — return available voices (can be static or fetched from API)
- `list_models()` — optional, return available models (for backends with multiple models)
- `synthesize()` — convert speakable chunks into audio bytes
- `backend_name`, `supports_streaming`, `supports_emotion_tags` properties
