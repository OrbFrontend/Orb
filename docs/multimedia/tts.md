# Text-to-Speech

Orb can read character dialogue aloud. Speech settings are global, while each
character has its own voice.

## Playback controls

In **Settings**, configure:

- **Audio/TTS enabled**: turn speech playback on or off
- **Auto-speak**: play speech for each new assistant reply
- **Volume**: set playback volume

Select the speaker icon on an assistant reply to read it. Select a quoted line to
read only that line. Orb highlights the line currently playing.

## Character voices

Open a character's **Voice** tab to choose:

- Whether the voice is enabled
- Backend and connection settings
- Language and voice
- Speed and pitch
- Preview playback

In a group chat, choose a **Cast member** in the Voice panel. Each reply uses the
voice of the member who wrote it, regardless of which member is selected in the
panel.

## How speech is made

1. Orb extracts quoted dialogue locally. Narration and inner monologue are left
   out. Recognized action beats such as `*laughs*` can become pauses or emotion
   tags for compatible backends.
2. The selected backend synthesizes the dialogue.
3. The browser plays the audio. Orb caches generated audio for replay.

Orb recognizes straight and curly double quotes.

## Available backends

| Backend | Setup | API key | Notes |
|---|---|---|---|
| Microsoft Edge TTS | Included in `requirements.txt` | None | 400+ voices in 80+ languages |
| OpenAI-compatible | HTTP endpoint | Required | Uses `POST /v1/audio/speech`; voices and models depend on the provider |
| Kokoro-82M | Install `requirements-tts.txt` | None | Local model with 54 voices and 9 languages |
| Fish Speech | HTTP endpoint | Optional | Local server with voice references |
| ElevenLabs | HTTP endpoint | Required | Cloud voices and emotion tags |

## Add a backend

Backends live in `backend/tts/` and implement the `TTSAdapter` base class. The
router registers an adapter when its dependencies are available. Implement
`list_voices()`, `list_models()` when needed, and `synthesize()`, plus the adapter
metadata properties. `backend/tts/edge_adapter.py` is a reference.
