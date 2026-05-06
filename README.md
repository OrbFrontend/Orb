# Orb - Agentic RP Frontend

![Orb](Orb.png)
## Problem Statement

LLMs suffer from stylistic inertia in long roleplay sessions. Once a tone, pacing, or prose style is established over several turns, the model tends to perpetuate it regardless of narrative shifts. A lighthearted conversation that turns tragic will often retain the cadence and vocabulary of the earlier tone because the weight of prior context anchors the model's generation.

Static system prompts cannot solve this. The system prompt is written once and does not adapt to evolving scenes.

## Solution Overview

An **agentic middleware layer** sits between the user and the model. It intercepts each user message, runs a short analytical pass to "read the room," then dynamically assembles prompt directives that shape the model's writing before the actual roleplay generation happens.

The user never sees the agentic layer. The writer model doesn't know it's being directed. The result is a roleplay session that naturally adapts its style, tone, and pacing as the narrative evolves.

## Notable Features
1. **Clear direction for Writer**: Grounding the story + actively steering the writing style = better output
2. **Customizability**: Customizable prompt injection that's automatically used by Director model
3. **Anti-slop**: Get rid of overused words, phrases, and patterns often seen in LLM outputs
4. **Length Guard**: Actively or passively protect from length degradation as context grows
5. **Super-regenerate**: Normal regens may give samey outputs, ask for a different take
6. **Magic Rewrite**: Rewrite the target message in a user-defined direction
7. **Compress History**: Summarize chat context and move it to a new conversation
8. **Mobile-compatibility**: UI for mobile devices

## Architecture

### Three-Pass Design

The system uses a three-pass architecture, with the agent and writer optionally being the same or different models:

1. **Director Pass** - Tool-calling phase where the LLM selects moods, plot direction, and potentially rewrites user prompts
2. **Writer Pass** - Story generation phase where the LLM writes the actual roleplay response
3. **Editor Pass** - A ReAct loop - Self-audit for slop and length optimization phase. This is surgical, errors will be programmatically detected, 
the model only needs to write replacement for targeted sentences

### KV Cache Reuse Strategy

For optimal KV cache reuse, the following will remain consistent across passes:

#### 1. System Prompt
- The system prompt (character card, instructions, etc.) is identical across all passes
- Built once and reused forever
- Includes character description, scenario, example dialogue, and additional instructions

#### 2. Chat History
- The conversation history (previous messages) is identical across all passes
- Maintains exact same message content and ordering

#### 3. Tool Schemas
- The same tool definitions must be sent in each LLM call for kv cache reuse
- Tool schemas affect the model's internal representation
- Inconsistent tool schemas break KV cache alignment

## Design Principles

1. Prioritize small models - if a feature fails half of the time on Gemma-4-26B4A, it will be scrapped
2. Only use agentic functionalities when absolutely needed - we will not have useless tools like `dice_roll`
3. Scanning should be algorithmic, avoid making LLMs eyeball for errors
4. Keep agentic scope small, avoid giving the agent too much freedom of choice

## Drawbacks

1. **Speed**: Multiple passes will obviously have a longer time to final response
2. **Cost**: Neligible cost increase, which comes naturally with multiple passes, somewhat alleviated by KV cache reuse strategy

## Requirements
1. A model with solid tool/function calling capabilities (recommended: Gemma 4)
2. OpenAI-compatible LLM inference backend API that supports prompt-caching
3. Python 3.9+

## Contributing & Discussions

Read this before opening a PR: https://github.com/OrbFrontend/Orb/blob/main/CONTRIBUTING.md

Ideas, help requests, and questions go here: https://github.com/OrbFrontend/Orb/discussions

## Voice Speech generation (TTS)

Orb can speak character messages using text-to-speech. Edge TTS is included in the main Python dependencies; cloud and self-hosted backends use the existing HTTP client stack.

### Setup

```bash
# Install the main dependencies
pip install -r requirements.txt
```

Restart Orb. A **Voice** tab appears in the character editor where you can:
- Pick a TTS backend and voice
- Adjust speed and pitch
- Preview voices before saving
- Set a custom speech generation prompt per character

### How It Works

Clicking the 🔊 speaker icon on a character message triggers a three-step pipeline:

1. **Speech Scripter** — an LLM pass extracts only spoken dialogue from the message, stripping speech generation, action beats (`*like this*`), and scene descriptions
2. **TTS Synthesis** — the speakable text is sent to the configured backend (Edge TTS, OpenAI, Fish Speech, ElevenLabs)
3. **Playback** — the generated audio plays in-browser; results are cached on disk so repeated plays are instant

### Available Backends

| Backend | Install | API Key | Voices | Models | Notes |
|---------|---------|---------|--------|--------|-------|
| Microsoft Edge TTS | Included in `requirements.txt` | None (free) | Fetched live, filterable by language | — | 400+ voices, 80+ languages |
| OpenAI TTS (and compatible) | None (httpx) | Required | 10 built-in voices (alloy, echo, nova, shimmer...) | Fetched live from `/v1/models` | Works with any provider implementing `POST /v1/audio/speech` |
| Fish Speech | None (httpx) | Optional | Fetched live from `/v1/references/list` | — | Self-hosted, supports voice cloning via references. Default: `http://localhost:8080` |
| ElevenLabs | None (httpx) | Required | Fetched live from ElevenLabs API | — | 300+ cloud voices, emotion tags, highest quality |

### Adding New Backends

Each backend is a single file in `backend/tts/` implementing the `TTSAdapter` base class. The router auto-registers adapters whose dependencies are installed (try/except import). See `backend/tts/edge_adapter.py` as a reference.

Key methods to implement:
- `list_voices()` — return available voices (can be static or fetched from API)
- `list_models()` — optional, return available models (for backends with multiple models)
- `synthesize()` — convert speakable chunks into audio bytes
- `backend_name`, `supports_streaming`, `supports_emotion_tags` properties
