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

Spark-TTS builds a voice from gender and pitch/speed attributes rather than a
fixed voice bank, and can clone a voice from a reference clip. Speed and pitch
apply to its attribute-built voices only; a cloned voice takes its prosody from
the reference clip.

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
Unicode quote pairs. Paired em-dash dialogue supports consecutive lines at prose
boundaries, including inline emphasis within each line.
Inline narrative dash asides are left out. Parenthetical asides, OOC notes,
protected formatting (code fences, bold runs and dividers), and recognized
attributed thoughts are excluded. Ordinary parentheses inside dialogue remain
spoken.

The classifier identifies a whole-message convention, not the speaker or the
role of every sentence. For unmarked chat such as `Hello. Let's get to know each
other.`, it can return `unknown` for both conventions. TTS reads that plain text
as speech, while still excluding text positively classified as narration. This
fallback requires the model's reading; the heuristic fallback alone cannot
reliably recognize plain chat. Mixed bare narration and speech cannot always be
separated reliably: speech selection follows the classified convention, without
discarding bare dialogue merely because it begins with words such as “The” or
“She”. An ambiguous unmarked narrative may also be read. TTS does not
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
| Kokoro-82M | HTTP endpoint | None | Local server with 54 voices and 9 languages |
| Spark-TTS-0.5B | HTTP endpoint | Optional | Local server; voices built from gender/pitch/speed attributes, plus zero-shot cloning. |
| Fish Speech | HTTP endpoint | Optional | Local server with voice references |
| ElevenLabs | HTTP endpoint | Required | Cloud voices and emotion tags |

### Local server backends

Kokoro-82M, Spark-TTS, and Fish Speech run as local servers. Orb ships the
client, not the server: run one yourself, then point that voice's **API URL**
at it.

Kokoro-82M (default `http://localhost:9200`) and Spark-TTS (default
`http://localhost:9300`) each expose:

- `GET /v1/voices` — returns `[{id, name, language, gender}]`
- `POST /v1/tts` — returns WAV

Their request bodies differ. Kokoro takes `{text, voice, speed, lang}`, where
`lang` is its own single-letter code (`a` for American English, `b` for
British, and so on). Spark-TTS takes `{text, voice, speed, pitch, lang}`, where
`lang` is a full locale. Fish Speech uses its own native API instead: `POST
/v1/tts` keyed by `reference_id`, and `GET /v1/references/list`.

Orb sends one request per speech block and joins the clips itself, inserting
real silence for pauses, so a server only ever synthesizes one block at a time.
`speed` and `pitch` arrive as float multipliers where `1.0` means the voice's
own level.

## Add a backend

Backends live in `backend/workflows/tts/engine/` and implement the `TTSAdapter` base class. The
router registers an adapter when its dependencies are available. Implement
`list_voices()`, `list_models()` when needed, and `synthesize()`, plus the adapter
metadata properties. `backend/workflows/tts/engine/edge_adapter.py` is a reference.
