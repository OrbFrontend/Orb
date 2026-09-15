# Text-to-Speech

Orb can read character dialogue aloud. Speech settings are global, while each
character has its own voice.

## Playback controls

In **Settings**, configure:

- **Audio/TTS enabled**: turn speech playback on or off
- **Auto-speak**: play speech for each new assistant reply
- **Volume**: set playback volume

Select the speaker icon on an assistant reply to read it. Select a spoken line to
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

1. Orb reads the message's dialogue/narration convention with the same markup
   classifier used by Format Consistency, when that Local ML model is available
   and enabled. Otherwise it uses the shared format heuristics. This works
   independently of whether the Format Consistency workflow is enabled.
2. Shared quote and emphasis segmentation selects speech. It recognizes quoted
   dialogue and bare dialogue between marked action beats, including underscore
   beats and inline emphasis. Recognized action beats such as `*laughs*` can
   become pauses or emotion tags for compatible backends.
3. The selected backend synthesizes each speech block. The browser plays the
   audio and highlights its words. Orb caches generated audio for replay.

Orb recognizes straight, curly, guillemet, CJK, fullwidth, and other supported
Unicode quote pairs. Paired em-dash dialogue at a prose boundary is supported;
inline narrative dash asides are left out. Parenthetical asides, OOC notes,
protected formatting (code fences, bold runs and dividers), and recognized
attributed thoughts are excluded. Ordinary parentheses inside dialogue remain
spoken.

The classifier identifies a whole-message convention, not the speaker or the
role of every sentence. For unmarked chat such as `Hello. Let's get to know each
other.`, it can return `unknown` for both conventions. TTS reads that plain text
as speech, while still excluding text positively classified as narration. This
fallback requires the model's reading; the heuristic fallback alone cannot
reliably recognize plain chat. Mixed bare narration and speech cannot always be
separated reliably, and an ambiguous unmarked narrative may also be read. TTS does not
infer different speakers inside one reply. Voice previews read their literal
input without dialogue extraction.

New audio attachments save the exact synthesis chunks in
`generation_metadata.speech_chunks`, and each playback block includes
`consumption_metadata.blocks[].spoken_text`. Replay uses those saved chunks
without rerunning the classifier, and highlighting uses the stored spoken text.
Older attachments retain their original extraction rules for replay; regenerating
an attachment applies the current segmentation and Local ML settings.

## Available backends

| Backend | Setup | API key | Notes |
|---|---|---|---|
| Microsoft Edge TTS | Included in `requirements.txt` | None | 400+ voices in 80+ languages |
| OpenAI-compatible | HTTP endpoint | Required | Uses `POST /v1/audio/speech`; voices and models depend on the provider |
| Kokoro-82M | Install `requirements-tts.txt` | None | Local model with 54 voices and 9 languages |
| Fish Speech | HTTP endpoint | Optional | Local server with voice references |
| ElevenLabs | HTTP endpoint | Required | Cloud voices and emotion tags |

## Add a backend

Backends live in `backend/workflows/tts/engine/` and implement the `TTSAdapter` base class. The
router registers an adapter when its dependencies are available. Implement
`list_voices()`, `list_models()` when needed, and `synthesize()`, plus the adapter
metadata properties. `backend/workflows/tts/engine/edge_adapter.py` is a reference.
