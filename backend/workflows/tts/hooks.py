"""Bind TTS generation to workflow hooks."""

from __future__ import annotations

import base64
import logging

from ..toolkit import (
    get_message_by_id,
    get_workflow_character_state,
    get_workflow_config,
    insert_workflow_attachment,
    set_workflow_character_state,
    spark_voice_reference_audio,
)
from .config import normalize_config
from .engine.router import get_adapter, list_backends
from .engine.wav import pcm_to_wav
from .synth import (
    WORKFLOW_ID,
    audio_mime_ext,
    build_generation_metadata,
    compute_seed,
    consumption_blocks,
    normalize_profile,
    synthesize,
    synthesize_blocks,
    synthesize_blocks_from_metadata,
)

logger = logging.getLogger(__name__)

PREVIEW_TEXT = "Hey, this is a voice preview. How do I sound?"


def _attachment(text: str, profile: dict, audio: bytes, mime: str, backend: str, blocks: list[dict]) -> dict:
    """Assemble the attachment payload for a freshly synthesized clip.

    ``consumption_metadata`` carries the per-block byte ranges and inter-block
    pauses the frontend reads to slice and play individual blocks.
    """
    _, ext = audio_mime_ext(backend)
    duration_ms = sum(
        max(0, int(block.get("duration_ms") or 0)) + max(0, int(block.get("pause_after_ms") or 0)) for block in blocks
    )
    return {
        "workflow_id": WORKFLOW_ID,
        "filename": f"speech.{ext}",
        "mime": mime,
        "data": audio,
        "seed": compute_seed(text, profile, blocks),
        "generation_metadata": build_generation_metadata(text, profile, blocks),
        "consumption_metadata": {"duration_ms": duration_ms, "blocks": consumption_blocks(blocks)},
    }


async def post_pipeline(ctx):
    """Synthesize the finished reply for a character whose voice profile is
    enabled. Yields one ``attach_artifact`` and, when auto-play is on, a
    pass-through event the frontend uses to start playback.

    Generation is gated solely by the per-character profile's ``enabled`` flag;
    there is no global generate switch. A synthesis failure is logged and
    swallowed -- a bad TTS backend must not fail the user's turn.
    """
    if not ctx.character_id:
        return
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID))
    if not profile.get("enabled"):
        return
    text = ctx.draft or ""
    if not text.strip():
        return
    yield {"event": "phase_status", "data": {"channel": f"workflow:{WORKFLOW_ID}", "label": "Synthesizing speech..."}}
    try:
        audio, mime, blocks = await synthesize_blocks(text, profile, settings=ctx.settings)
    except Exception:
        logger.exception("tts auto-generation failed")
        return

    att = _attachment(text, profile, audio, mime, profile.get("backend", "edge"), blocks)
    att["source"] = f"workflow:{WORKFLOW_ID}"
    yield {"type": "attach_artifact", "attachment": att}
    yield {"event": "phase_status", "data": {"channel": f"workflow:{WORKFLOW_ID}", "state": "done"}}

    if normalize_config(await get_workflow_config(WORKFLOW_ID))["auto_play"]:
        yield {"event": "tts_autoplay", "data": {}}


async def regenerate(ctx, body):
    """Re-synthesize the message under the character's CURRENT voice profile.

    Unlike reroll, this ignores the original's stored parameters and re-reads
    both the message text and the live profile, so an edit to the voice (pitch,
    rate, backend, voice id, ...) takes effect here. The route stamps
    ``workflow_id`` and ``parent_attachment_id`` and validates each entry.
    """
    message = await get_message_by_id(ctx.message_id)
    text = (message or {}).get("content") or ""
    if not text.strip():
        return []
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    try:
        audio, mime, blocks = await synthesize_blocks(text, profile, settings=ctx.settings)
    except Exception:
        logger.exception("tts regenerate failed for attachment %s", ctx.attachment_id)
        return []
    return [_attachment(text, profile, audio, mime, profile.get("backend", "edge"), blocks)]


async def reroll_gen(ctx, params, seed):
    """Re-synthesize from the stored parameters. Returns ``(bytes, consumption_metadata)``.

    The framework-supplied ``seed`` is ignored because TTS synthesis takes no
    seed input -- there is nothing for it to influence -- so reroll and
    rehydrate both reproduce from the stored parameters alone. The
    consumption_metadata is returned alongside so the byte ranges track the
    freshly synthesized clips on both the reroll (new sibling) and rehydrate
    (in-place) routes. Raises on missing parameters, surfaced by the route as
    a 500.
    """
    audio, _, blocks = await synthesize_blocks_from_metadata(params if isinstance(params, dict) else {})
    duration_ms = sum(
        max(0, int(block.get("duration_ms") or 0)) + max(0, int(block.get("pause_after_ms") or 0)) for block in blocks
    )
    return audio, {"duration_ms": duration_ms, "blocks": consumption_blocks(blocks)}


async def on_demand(ctx, body):
    action = body.get("action") if isinstance(body, dict) else None
    if action == "create":
        return await _create(ctx, body)
    if action == "get_profile":
        return await _get_profile(ctx)
    if action == "set_profile":
        return await _set_profile(ctx, body)
    return {"error": f"unknown action: {action!r}"}


async def _create(ctx, body) -> dict:
    mid = body.get("message_id")
    if not isinstance(mid, int) or isinstance(mid, bool):
        return {"error": "message_id (int) required"}
    msg = await get_message_by_id(mid)
    if msg is None or msg.get("conversation_id") != ctx.conversation_id:
        return {"error": "message not found in this conversation"}
    if msg.get("role") != "assistant":
        return {"error": "speech can only be generated for assistant messages"}
    text = msg.get("content") or ""
    if not text.strip():
        return {"error": "message has no text"}
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    try:
        audio, mime, blocks = await synthesize_blocks(text, profile, settings=ctx.settings)
    except Exception:
        logger.exception("tts create failed for message %s", mid)
        return {"error": "synthesis failed"}
    new_id, rejected = await insert_workflow_attachment(
        mid, _attachment(text, profile, audio, mime, profile.get("backend", "edge"), blocks)
    )
    if new_id is None:
        return {"error": "attachment rejected", "reason": (rejected or {}).get("reason")}
    return {"attachment_id": new_id}


async def _get_profile(ctx) -> dict:
    if not ctx.character_id:
        return {"profile": None, "character_id": None}
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID))
    return {"profile": profile, "character_id": ctx.character_id}


async def _set_profile(ctx, body) -> dict:
    if not ctx.character_id:
        return {"error": "no active character"}
    raw = body.get("profile")
    if not isinstance(raw, dict):
        return {"error": "profile (dict) required"}
    profile = normalize_profile(raw)
    await set_workflow_character_state(ctx.character_id, WORKFLOW_ID, profile)
    return {"ok": True, "profile": profile}


# These back the config panel's Backend / Voice / Model selectors and the
# Preview button. They answer from the static backend registry or by probing the
# TTS backend named in the form's unsaved profile, with no conversation in scope,
# and report their own failures in-band -- the caller degrades (empty list,
# status text) rather than treating a probe failure as an HTTP error.


async def query(ctx, body):
    action = body.get("action") if isinstance(body, dict) else None
    if action == "list_backends":
        return {"backends": list_backends()}
    if action == "list_voices":
        return await _list_voices(body)
    if action == "list_models":
        return await _list_models(body)
    if action == "preview":
        return await _preview(body)
    if action == "reference_audio":
        return await _reference_audio(body)
    return {"error": f"unknown action: {action!r}"}


async def _list_voices(body) -> dict:
    backend = body.get("backend") or "edge"
    try:
        adapter = get_adapter(backend)
        voices = await adapter.list_voices(
            language=body.get("language") or "",
            api_url=body.get("api_url") or "",
            api_key=(body.get("api_key") or None),
            model=body.get("model") or "",
        )
    except Exception:
        logger.exception("tts list_voices failed for backend %r", backend)
        return {"voices": [], "error": "could not load voices"}
    return {"voices": voices}


async def _list_models(body) -> dict:
    backend = body.get("backend") or "edge"
    try:
        adapter = get_adapter(backend)
        models = await adapter.list_models(
            api_url=body.get("api_url") or "",
            api_key=(body.get("api_key") or None),
        )
    except Exception:
        logger.exception("tts list_models failed for backend %r", backend)
        return {"models": [], "error": "could not load models"}
    return {"models": models}


async def _preview(body) -> dict:
    profile = normalize_profile(body)
    text = body.get("text") or PREVIEW_TEXT
    try:
        audio, mime = await synthesize(text, profile)
    except ValueError as exc:
        # An adapter raises ValueError for the refusals a user can act on: no
        # voice enrolled yet, a model that is not downloaded, a missing API
        # key. That message IS the fix, so it reaches the panel's status line;
        # "preview synthesis failed" would send the user looking for a bug that
        # is not there. Anything else IS a bug and stays generic below.
        logger.info("tts preview refused: %s", exc)
        return {"error": str(exc) or "preview synthesis failed"}
    except Exception:
        logger.exception("tts preview failed")
        return {"error": "preview synthesis failed"}
    return {"audio_b64": base64.b64encode(audio).decode("ascii"), "mime": mime}


async def _reference_audio(body) -> dict:
    """The form's advanced reference excerpt as audio, to check its transcript by ear."""
    profile = normalize_profile(body)
    if not profile["reference_tokens"] or not profile["speaker_tokens"]:
        return {"error": "This voice has no reference excerpt yet."}
    try:
        pcm, rate = await spark_voice_reference_audio(profile["reference_tokens"], profile["speaker_tokens"])
    except ValueError as exc:  # the codec is missing or switched off: the message is the fix
        return {"error": str(exc) or "could not play the excerpt"}
    except Exception:
        logger.exception("tts reference excerpt failed")
        return {"error": "could not play the excerpt"}
    return {"audio_b64": base64.b64encode(pcm_to_wav(pcm, rate)).decode("ascii"), "mime": "audio/wav"}
