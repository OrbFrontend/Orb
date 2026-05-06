"""
backend/tts/speech_scripter.py — Speech Scripter pass.

Transforms writer output into speakable text chunks annotated with emotion
and pause markers, optimized for a specific TTS backend.

This is a standalone post-processing step, NOT a pipeline pass. It runs
after the writer finishes, called directly by the TTS API endpoint.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from ..llm_client import LLMClient
from .base import SpeakableChunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompts — one per backend capability tier
# ---------------------------------------------------------------------------

_SCRIPTER_BASE = """\
You extract ONLY spoken dialogue from roleplay text for text-to-speech.

CRITICAL RULES:
1. ONLY output text that a character says out loud (in quotes)
2. NEVER output narration, action descriptions, or scene-setting — these are \
   NOT spoken and must be discarded or converted to pauses
3. Action beats between dialogue (*she laughs*, *she sighs*, *she pauses*) → \
   convert to a pause chunk with NO text, or a sound tag if the backend supports it
4. Internal thoughts (parenthetical text) → SKIP entirely
5. Preserve dialogue exactly as written — contractions, filler, hesitations
6. Infer the speaker's emotion from context and dialogue tone
7. NEVER invent or add content

Output format: JSON array of chunks
[
  {{"text": "Hey.", "emotion": "warm", "pause_after_ms": 300}},
  {{"pause_after_ms": 500}},
  {{"text": "I was just thinking about you.", "emotion": "soft"}}
]

Valid emotions: neutral, warm, soft, playful, teasing, sad, angry, fearful, \
surprised, whispered, breathless, amused

Respond with ONLY the JSON array. No markdown, no explanation."""

_SCRIPTER_PLAIN = (
    _SCRIPTER_BASE
    + """

This TTS backend does NOT support special tags.
- Action beats with sounds (*laughs*, *sighs*, *gasps*) → pause chunk only (no text)
- Silent actions (*smiles*, *shrugs*, *nods*) → skip entirely (no chunk at all)
- Scene descriptions, narration → skip entirely

Pauses: Use pause_before_ms and pause_after_ms (milliseconds).
- Between dialogue lines in same scene: 300ms
- After an action beat: 500ms
- Scene change (new location, time skip): 800ms
"""
)

_SCRIPTER_WITH_TAGS = (
    _SCRIPTER_BASE
    + """

This TTS backend supports inline sound tags: [laugh], [sigh], [gasp], \
[moan], [groan], [chuckle], [sniffle], [cough], [clears throat].

Use these tags within dialogue text:
- *she laughs* → add [laugh] before the next dialogue: "[laugh] That's funny."
- *she sighs* → "[sigh] Fine."
- *she gasps* → "[gasp] What?"
- Silent actions (*smiles*, *nods*) → skip entirely, just add a pause

Pauses: Use pause_before_ms and pause_after_ms (milliseconds).
- Short pause: 200ms
- Medium pause: 400ms
- Long pause: 700ms
"""
)

# Map backend names to prompt variants
_PROMPT_MAP = {
    # Backends WITHOUT emotion tags
    "edge": _SCRIPTER_PLAIN,
    "kokoro": _SCRIPTER_PLAIN,
    "piper": _SCRIPTER_PLAIN,
    # Backends WITH emotion tags
    "fish": _SCRIPTER_WITH_TAGS,
    "qwen3": _SCRIPTER_WITH_TAGS,
    "elevenlabs": _SCRIPTER_WITH_TAGS,
}


def _get_system_prompt(backend_type: str, custom_prompt: str = "") -> str:
    """Select the right speech scripter prompt for the TTS backend."""
    prompt = _PROMPT_MAP.get(backend_type, _SCRIPTER_PLAIN)
    if custom_prompt:
        prompt += f"\n\nAdditional instructions from user:\n{custom_prompt}"
    return prompt


def _build_mood_context(moods: list[str]) -> str:
    """Add mood context from the Director to the scripter prompt."""
    if not moods:
        return ""
    return f"\n\nCurrent scene moods: {', '.join(moods)}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_speech_scripter(
    client: LLMClient,
    model: str,
    writer_text: str,
    backend_type: str = "edge",
    moods: Optional[list[str]] = None,
    custom_prompt: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> list[SpeakableChunk]:
    """Transform writer output into speakable chunks via LLM.

    Falls back to plain text passthrough (strip markdown only) if the LLM
    fails or returns invalid JSON.
    """
    system_prompt = _get_system_prompt(backend_type, custom_prompt)
    mood_ctx = _build_mood_context(moods or [])

    messages = [
        {"role": "system", "content": system_prompt + mood_ctx},
        {"role": "user", "content": writer_text},
    ]

    try:
        # Collect streamed response
        full_content = ""
        async for event in client.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            if event.get("type") == "done":
                msg = event.get("message", {})
                full_content = msg.get("content", "")
            elif event.get("type") == "content":
                full_content += event.get("delta", "")

        chunks = _parse_chunks(full_content)
        if chunks:
            logger.info(
                "Speech Scripter: transformed %d chars → %d chunks (backend=%s)",
                len(writer_text),
                len(chunks),
                backend_type,
            )
            return chunks

        logger.warning(
            "Speech Scripter: LLM returned no parseable chunks (content_len=%d), falling back. Raw: %.200s",
            len(full_content),
            full_content,
        )
        return _fallback_passthrough(writer_text)

    except Exception:
        logger.exception(
            "Speech Scripter: LLM call failed, falling back to passthrough"
        )
        return _fallback_passthrough(writer_text)


def _parse_chunks(raw: str) -> list[SpeakableChunk]:
    """Parse speech scripter LLM output into SpeakableChunks.

    Handles common LLM formatting issues:
    - Wrapping in ```json ... ```
    - Trailing text after the JSON array
    - Extra whitespace
    """
    text = raw.strip()

    # Extract JSON from markdown code blocks
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # Find the JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1:
        return []

    if end == -1 or end <= start:
        # No closing bracket — try to recover truncated array
        truncated = text[start:]
        last_brace = truncated.rfind("}")
        if last_brace > 0:
            try:
                data = json.loads(truncated[: last_brace + 1] + "]")
            except json.JSONDecodeError:
                return []
        else:
            return []
    else:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            # Truncated array — find last complete object and close it
            truncated = text[start : end + 1]
            last_brace = truncated.rfind("}")
            if last_brace > 0:
                try:
                    data = json.loads(truncated[: last_brace + 1] + "]")
                except json.JSONDecodeError:
                    return []
            else:
                return []

    if not isinstance(data, list):
        return []

    chunks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Pause-only chunk (no text) — used for action beat pauses
        if "text" not in item:
            pause = int(item.get("pause_after_ms", 0))
            if pause > 0:
                chunks.append(
                    SpeakableChunk(text="", emotion="neutral", pause_after_ms=pause)
                )
            continue
        emotion = item.get("emotion", "neutral")
        if emotion not in SpeakableChunk.EMOTIONS:
            emotion = "neutral"
        chunks.append(
            SpeakableChunk(
                text=item["text"],
                emotion=emotion,
                pause_before_ms=int(item.get("pause_before_ms", 0)),
                pause_after_ms=int(item.get("pause_after_ms", 0)),
            )
        )

    return chunks


# Fallback import for backward compatibility — new code should use
# backend.tts.regex_extractor.regex_extract instead.
def _fallback_passthrough(writer_text: str) -> list[SpeakableChunk]:
    """Extract only quoted dialogue, strip everything else.

    DEPRECATED: Use regex_extract from backend.tts.regex_extractor instead.
    Kept for backward compat with existing tests.
    """
    if not writer_text.strip():
        return []

    chunks = []
    for match in re.finditer(r'"([^"]+)"', writer_text):
        text = match.group(1)
        if text:
            chunks.append(
                SpeakableChunk(text=text, emotion="neutral", pause_before_ms=300)
            )

    if not chunks:
        return []

    chunks[0].pause_before_ms = 0
    return chunks
