"""Stable imports for workflow authors."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..analysis import (
    AxisStyle,
    Dialogue,
    Narration,
    build_targets,
    classify_axes,
    format_numbered_report,
    format_report,
    narration_only,
    protected_runs,
    run_audit,
    speech_input,
    speech_segments,
    spoken_lines,
)

# Shared span primitives used by markup repair, voice shaping, and classification.
from ..analysis.text.roleplay import emphasis_inner, span_role, split_ws, strip_quotes
from ..analysis.text.roleplay_segmentation import (
    extract_block_spans,
    find_emphasis_spans,
)
from ..core import (
    CardScripts,
    Macros,
    card_description,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
)
from ..core.domain_types import AgentLane, CastMember, TurnCast
from ..core.text_segmentation import (
    CLOSE_QUOTES,
    OPEN_QUOTES,
    TOGGLE_QUOTES,
    find_quote_spans,
    map_prose,
    strip_protected_markup,
)
from ..database import (
    get_active_lorebook_entries,
    get_character_avatar,
    get_character_card,
    get_conversation,
    get_director_state,
    get_group_member_scripts,
    get_interactive_fragments,
    get_message_by_id,
    get_messages,
    get_mood_fragments,
    get_phrase_bank,
    get_settings,
    get_speaker_names,
    get_user_attachments_for_message,
    get_user_persona,
    get_user_personas,
    get_workflow_attachment_by_id,
    resolve_cast,
    resolve_char_context,
)
from ..inference import local_ml as _local_ml
from ..inference import (
    separate_agent_lane_configured as _separate_agent_lane_configured,
)
from ..prompting import build_prefix as _build_prefix
from ..prompting import macro_identity as _macro_identity
from ..prompting.lorebook import (
    compute_constant_lorebook_block as _compute_constant_lorebook_block,
)
from . import spark_tts_host as _spark_tts_host
from ._forced_call import forced_tool_call
from .attachment_cache import EVICTED_MARKER, insert_workflow_attachment
from .contracts import EV_DRAFT_REPLACED, ExportedFile, ToolSpec, WorkflowEventStream
from .errors import WorkflowUserFacingError
from .registry import (
    Workflow,
    get_workflow_character_state,
    get_workflow_config,
    get_workflow_message_state,
    get_workflow_state,
    overlay_enable_tools,
    set_workflow_character_state,
    set_workflow_config,
    set_workflow_message_state,
    set_workflow_state,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AxisStyle",
    "CLOSE_QUOTES",
    "CastMember",
    "Dialogue",
    "EVICTED_MARKER",
    "EV_DRAFT_REPLACED",
    "ExportedFile",
    "Macros",
    "Narration",
    "OPEN_QUOTES",
    "TOGGLE_QUOTES",
    "ToolSpec",
    "TurnCast",
    "Workflow",
    "WorkflowEventStream",
    "WorkflowUserFacingError",
    "classify_pov",
    "classify_pov_tense",
    "classify_axes",
    "markup_axes",
    "emphasis_inner",
    "extract_block_spans",
    "find_emphasis_spans",
    "find_quote_spans",
    "map_prose",
    "span_role",
    "split_ws",
    "strip_protected_markup",
    "strip_quotes",
    "forced_tool_call",
    "build_targets",
    "format_numbered_report",
    "format_report",
    "get_character_avatar",
    "get_character_card",
    "get_conversation",
    "get_interactive_fragments",
    "get_director_state",
    "get_message_by_id",
    "get_messages",
    "get_mood_fragments",
    "get_phrase_bank",
    "get_scene_cast",
    "get_settings",
    "get_user_attachments_for_message",
    "get_user_personas",
    "get_user_persona",
    "get_workflow_attachment_by_id",
    "get_workflow_character_state",
    "get_workflow_config",
    "get_workflow_message_state",
    "get_workflow_state",
    "insert_workflow_attachment",
    "local_feature_available",
    "local_feature_ready",
    "local_model_identity",
    "narration_only",
    "overlay_enable_tools",
    "spark_voice_clean_reference_text",
    "spark_voice_clean_reference_tokens",
    "spark_voice_clean_tokens",
    "spark_voice_reference_audio",
    "spark_voice_speak",
    "protected_runs",
    "run_audit",
    "spoken_lines",
    "speech_segments",
    "speech_input",
    "build_offturn_prefix",
    "conversation_macros",
    "set_workflow_character_state",
    "set_workflow_config",
    "set_workflow_message_state",
    "set_workflow_state",
    "workflow_character_state_lock",
    "workflow_config_lock",
    "workflow_state_lock",
]


def local_feature_available(feature: str) -> tuple[bool, str]:
    """Return whether a host-provided local classifier is ready."""
    return _local_ml.available(feature)


def local_feature_ready(feature: str, settings: Mapping[str, Any]) -> bool:
    """Return whether a local-ML feature is available and enabled."""
    available, _reason = local_feature_available(feature)
    enabled = settings.get("local_ml_enabled")
    return available and (not isinstance(enabled, Mapping) or enabled.get(feature, True) is not False)


def local_model_identity(feature: str) -> str:
    """Return the pinned model identity for caches of classifier results."""
    spec = _local_ml.MODELS[feature]
    return f"{spec.repo_id}@{spec.revision}"


async def classify_pov(text: str) -> str:
    """Classify narrative point of view through the host inference service."""
    return await _local_ml.aclassify_pov(text)


async def classify_pov_tense(text: str) -> tuple[str, str]:
    """Classify narrative point of view and tense through the host inference service."""
    return await _local_ml.aclassify_pov_tense(text)


_MARKUP_FEATURE = "markup_classifier"


async def markup_axes(text: str, settings: Mapping[str, Any]) -> AxisStyle:
    """*text*'s markup convention: the local markup classifier's reading, or
    ``classify_axes`` when that model is off, missing, or failing.

    Every consumer reads convention through this one door, so markup repair, voice
    shaping, and the image camera never disagree about a message.
    """
    if not local_feature_ready(_MARKUP_FEATURE, settings):
        return classify_axes(text)
    return await _classify_markup(text) or classify_axes(text)


async def _classify_markup(text: str) -> AxisStyle | None:
    try:
        narration, dialogue = await _local_ml.aclassify_markup(text)
    except Exception as e:  # a bad wheel or a wrong head: the heuristic still answers
        logger.warning("markup classifier failed (%r); reading markup heuristically", e)
        return None
    return AxisStyle(dialogue=Dialogue(dialogue), narration=Narration(narration))


def spark_voice_clean_tokens(raw: object) -> list[int]:
    """A stored voice as 32 validated speaker tokens, or ``[]``.

    The shape rule (exactly 32 ints in ``[0, 4096)``) is a property of BiCodec's
    FSQ quantizer, so it is answered by the model slice rather than restated in
    the workflow — a hand-rolled copy that drifts is a malformed voice reaching
    the codec, which fails inside an einsum rather than at the boundary.
    """
    return _spark_tts_host.clean_tokens(raw)


def spark_voice_clean_reference_tokens(raw: object) -> list[int]:
    """A stored advanced reference's semantic tokens, validated, or ``[]``."""
    return _spark_tts_host.clean_reference_tokens(raw)


def spark_voice_clean_reference_text(raw: object) -> str:
    """A stored advanced reference's transcript on one line, within the model's limit."""
    return _spark_tts_host.clean_reference_text(raw)


async def spark_voice_speak(
    text: str,
    speaker_tokens: Sequence[int],
    settings: Mapping[str, Any],
    *,
    reference_tokens: Sequence[int] = (),
    reference_text: str = "",
) -> tuple[bytes, int]:
    """Speak *text* in an enrolled voice."""
    return await _spark_tts_host.synthesize(
        text,
        speaker_tokens,
        settings,
        reference_tokens=reference_tokens,
        reference_text=reference_text,
    )


async def spark_voice_reference_audio(reference_tokens: Sequence[int], speaker_tokens: Sequence[int]) -> tuple[bytes, int]:
    """An advanced voice's reference excerpt as ``(pcm16, sample_rate)``, rebuilt from its tokens."""
    return await _spark_tts_host.reference_audio(reference_tokens, speaker_tokens)


async def get_scene_cast(conversation_id: str) -> TurnCast:
    """Return the conversation's resolved cast."""
    conv = await get_conversation(conversation_id)
    return await resolve_cast(conv) if conv is not None else TurnCast(False, ())


async def _turn_macros(
    conv: Mapping[str, Any],
    settings: Mapping[str, Any],
    card: Mapping[str, Any] | None,
    cast: TurnCast,
    *,
    seed: str | None = None,
) -> tuple[Macros, Mapping[str, Any] | None]:
    """Build identity macros and return the persona used for `{{user}}`."""
    persona_id = (
        conv.get("persona_lock_id") or (card.get("persona_lock_id") if card else None) or settings.get("active_persona_id")
    )
    persona = await get_user_persona(persona_id) if persona_id else None
    macro_char, cast_names = _macro_identity(conv, cast)
    conversation_seed = conv.get("macro_seed") or conv.get("id", "")
    macros = Macros.from_settings(
        settings,
        macro_char,
        persona,
        seed=conversation_seed if seed is None else seed,
        cast=cast_names,
        description=card_description(card),
    )
    return macros, persona


async def conversation_macros(
    conversation_id: str,
    settings: Mapping[str, Any],
    *,
    seed: str | None = None,
) -> Macros:
    """Build the macros for workflow-owned text in a conversation."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        return Macros(user=str(settings.get("user_name") or "User"), char="", seed=seed or "")
    card_id = conv.get("character_card_id")
    card = await get_character_card(card_id) if card_id else None
    macros, _ = await _turn_macros(conv, settings, card, await resolve_cast(conv), seed=seed)
    return macros


async def build_offturn_prefix(
    conversation_id: str,
    history,
    settings,
    *,
    lane: AgentLane = "writer",
) -> list[Any]:
    """Build the character and persona prefix for an off-turn call."""
    if lane not in ("writer", "agent"):
        raise ValueError(f"unknown off-turn model lane {lane!r}")
    conv = await get_conversation(conversation_id)
    if conv is None:
        return []
    card_id = conv.get("character_card_id")
    card = await get_character_card(card_id) if card_id else None
    # A group names no single character: the scene's title is {{char}}, the cast
    # section stands in for the card, and each replayed reply is attributed to
    # the member who wrote it. Resolved through the same reader the turn uses,
    # against the *neutral* base (no speaker) — which is the base the Director
    # runs on in every mode, Classic card swap included.
    turn_cast = await resolve_cast(conv)
    system_prompt, char_persona, mes_example = await resolve_char_context(conv, settings, card=card)
    dual_agent = lane == "agent" and _separate_agent_lane_configured(settings)
    if dual_agent:
        system_prompt, _, _ = await resolve_char_context(
            conv,
            settings,
            card=card,
            shared_key="agent_shared_system_prompt",
        )
    macros, persona = await _turn_macros(conv, settings, card, turn_cast)
    speaker_names = await get_speaker_names(conversation_id) if turn_cast.grouped else {}
    speaker_scripts = await get_group_member_scripts(conversation_id) if turn_cast.grouped else {}
    user_description = persona.get("description", "") if persona else settings.get("user_description", "")
    return _build_prefix(
        system_prompt,
        char_persona,
        conv.get("character_scenario", ""),
        mes_example,
        "" if settings.get("prevent_prompt_overrides") else conv.get("post_history_instructions", ""),
        history,
        macros,
        user_description,
        constant_lorebook_block=_compute_constant_lorebook_block(await get_active_lorebook_entries(), macros),
        cast=turn_cast,
        speaker_names=speaker_names,
        scripts=CardScripts.from_extensions(card.get("extensions") if card else None),
        speaker_scripts=speaker_scripts,
    )
