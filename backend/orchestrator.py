"""
orchestrator.py — Pipeline coordinator: director → writer → editor,
plus the public entry points handle_turn() and handle_regenerate().
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from . import database as db
from .llm_client import LLMClient
from .tool_defs import TOOLS, POST_WRITER_TOOLS
from .prompt_builder import build_prefix, compute_style_injection_block
from .kv_tracker import _KVCacheTracker
from .passes.director import _agent_pass
from .passes.writer import _writer_pass
from .passes.refine import editor_pass

logger = logging.getLogger(__name__)


# ── Core pipeline ─────────────────────────────────────────────────────────────


async def _run_pipeline(
    client: LLMClient,
    settings: dict,
    director: dict,
    fragments: list[dict],
    prefix: list[dict],
    user_message: str,
    phrase_bank: list[list[str]] | None = None,
) -> AsyncIterator[dict]:
    """Three-pass pipeline: director → writer → editor.

    KV cache strategy: *prefix* (system prompt + chat history) and the tool
    schema list returned by ``enabled_schemas(enabled_tools)`` are kept
    identical across all three passes so the LLM can reuse cached KV entries.
    Only ``tool_choice`` and the trailing user message differ per pass.
    ``editor_rewrite`` is included in the schema set whenever the length guard
    is enabled (mirroring how ``editor_apply_patch`` tracks ``audit_enabled``).
    """
    enabled_tools = settings.get("enabled_tools") or {}
    agent_on = bool(settings.get("enable_agent", 1))
    if not agent_on:
        enabled_tools = {}

    reasoning_passes = settings.get("reasoning_enabled_passes") or {}
    director_reasoning_on = bool(reasoning_passes.get("director", True))
    writer_reasoning_on = bool(reasoning_passes.get("writer", True))
    editor_reasoning_on = bool(reasoning_passes.get("editor", True))

    active_moods = director["active_moods"]
    agent_raw, calls, latency = "", [], 0
    (
        refined_msg,
        next_event,
        writing_direction,
        detected_repetitions,
        plot_summary,
        user_intent,
    ) = (None, None, None, None, None, None)
    keywords = director.get("keywords", [])
    effective_msg = user_message

    audit_enabled = (
        agent_on
        and bool(enabled_tools.get("editor_apply_patch", False))
        and phrase_bank is not None
    )

    # Length guard
    length_guard_enabled = (
        bool(enabled_tools.get("length_guard", False)) if agent_on else False
    )
    # Mirror editor_rewrite into enabled_tools so enabled_schemas() includes its schema in all
    # three passes — same KV-cache consistency approach as editor_apply_patch.
    if length_guard_enabled:
        enabled_tools = {**enabled_tools, "editor_rewrite": True}
    length_guard_enforce = (
        bool(enabled_tools.get("length_guard_enforce", False)) if agent_on else False
    )

    length_guard = (
        {
            "enabled": length_guard_enabled,
            "max_words": int(settings.get("length_guard_max_words", 240)),
            "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
        }
        if (length_guard_enabled and agent_on)
        else None
    )

    do_refine = audit_enabled or (length_guard_enabled and agent_on)

    prefix_chars = sum(len(m.get("content") or "") for m in prefix)
    kv_tracker = _KVCacheTracker(prefix_chars)

    # --- Director pass ---
    has_pre_writer_tools = any(
        enabled_tools.get(n, False) for n in TOOLS if n not in POST_WRITER_TOOLS
    )
    if agent_on and has_pre_writer_tools:
        yield {"event": "director_start"}
        async for event in _agent_pass(
            client,
            prefix,
            user_message,
            settings,
            director,
            fragments,
            enabled_tools,
            kv_tracker=kv_tracker,
            reasoning_on=director_reasoning_on,
        ):
            if event["type"] == "reasoning":
                yield {
                    "event": "reasoning",
                    "data": {"pass": "director", "delta": event["delta"]},
                }
            elif event["type"] == "done":
                (
                    active_moods,
                    agent_raw,
                    calls,
                    latency,
                    refined_msg,
                    next_event,
                    writing_direction,
                    detected_repetitions,
                    plot_summary,
                    keywords,
                    user_intent,
                ) = event["result"]
        if refined_msg:
            effective_msg = refined_msg
            yield {
                "event": "prompt_rewritten",
                "data": {"refined_message": refined_msg},
            }

    # Style injection
    direct_scene_enabled = agent_on and bool(enabled_tools.get("direct_scene", False))
    inj_block = compute_style_injection_block(
        active_moods,
        director["active_moods"],
        fragments,
        direct_scene_enabled,
        next_event,
        writing_direction,
        detected_repetitions,
        plot_summary,
        keywords,
        user_intent,
    )

    yield {
        "event": "director_done",
        "data": {
            "active_moods": active_moods,
            "injection_block": inj_block,
            "tool_calls": calls,
            "agent_latency_ms": latency,
            "next_event": next_event,
            "writing_direction": writing_direction,
            "detected_repetitions": detected_repetitions,
            "plot_summary": plot_summary,
            "keywords": keywords,
            "user_intent": user_intent,
        },
    }

    # --- Writer pass ---
    resp_text = ""
    async for item in _writer_pass(
        client,
        prefix,
        settings,
        enabled_tools,
        inj_block=inj_block,
        effective_msg=effective_msg,
        length_guard_enforce=length_guard_enforce,
        length_guard=length_guard,
        kv_tracker=kv_tracker,
        reasoning_on=writer_reasoning_on,
    ):
        if item["type"] == "reasoning":
            yield {
                "event": "reasoning",
                "data": {"pass": "writer", "delta": item["delta"]},
            }
        else:
            resp_text += item["delta"]
            yield {"event": "token", "data": item["delta"]}

    yield {
        "event": "_result",
        "data": {
            "active_moods": active_moods,
            "agent_raw": agent_raw,
            "calls": calls,
            "latency": latency,
            "refined_msg": refined_msg,
            "effective_msg": effective_msg,
            "resp_text": resp_text,
            "inj_block": inj_block,
            "next_event": next_event,
            "writing_direction": writing_direction,
            "detected_repetitions": detected_repetitions,
            "plot_summary": plot_summary,
            "keywords": keywords,
        },
    }

    # --- Editor pass ---
    if do_refine and resp_text:
        logger.info(
            "Editor pass starting (draft=%d chars, phrase_bank=%d groups)",
            len(resp_text),
            len(phrase_bank) if phrase_bank else 0,
        )
        try:
            async for event in editor_pass(
                client,
                prefix,
                effective_msg,
                resp_text,
                settings,
                phrase_bank or [],
                audit_enabled,
                length_guard,
                enabled_tools,
                kv_tracker=kv_tracker,
                reasoning_on=editor_reasoning_on,
            ):
                if event["type"] == "reasoning":
                    yield {
                        "event": "reasoning",
                        "data": {"pass": "editor", "delta": event["delta"]},
                    }
                elif event["type"] == "done":
                    refined_draft = event["draft"]
                    if refined_draft and refined_draft != resp_text:
                        resp_text = refined_draft
                        yield {
                            "event": "writer_rewrite",
                            "data": {"refined_text": resp_text},
                        }
                        yield {
                            "event": "_refined_result",
                            "data": {"resp_text": resp_text},
                        }
                    if event.get("tool_calls"):
                        yield {
                            "event": "editor_done",
                            "data": {"tool_calls": event["tool_calls"]},
                        }
        except Exception as e:
            logger.error("editor pass failed, keeping original: %s", e, exc_info=True)
    elif not do_refine:
        logger.info("Editor pass skipped (do_refine=%s)", do_refine)

    kv_tracker.log_summary()


# ═══════════════════════════════════════════════════════════════════════════════
# Shared infrastructure for handle_turn / handle_regenerate
# ═══════════════════════════════════════════════════════════════════════════════


async def _load_pipeline_context(conversation_id: str) -> dict | None:
    """Load everything the pipeline needs: settings, conversation, director,
    fragments, phrase_bank, and an LLMClient.

    Returns a dict of resolved objects, or None if the conversation was not found.
    """
    settings = await db.get_settings()
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None

    director = await db.get_director_state(conversation_id)
    fragments = await db.get_fragments()
    # Filter out disabled fragments
    fragments = [f for f in fragments if f.get("enabled", True)]
    # Remove disabled fragments from active moods
    if director and director.get("active_moods"):
        enabled_ids = {f["id"] for f in fragments}
        director["active_moods"] = [
            mood for mood in director["active_moods"] if mood in enabled_ids
        ]
    phrase_bank = await db.get_phrase_bank()
    client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

    system_prompt, char_persona, mes_example = await db.resolve_char_context(
        conv, settings
    )

    return {
        "settings": settings,
        "conv": conv,
        "director": director,
        "fragments": fragments,
        "phrase_bank": phrase_bank,
        "client": client,
        "system_prompt": system_prompt,
        "char_persona": char_persona,
        "mes_example": mes_example,
    }


def _build_prefix_from_ctx(ctx: dict, history: list[dict]) -> list[dict]:
    """Build the LLM prefix from a pipeline-context dict."""
    conv = ctx["conv"]
    settings = ctx["settings"]
    return build_prefix(
        ctx["system_prompt"],
        conv["character_name"],
        ctx["char_persona"],
        conv["character_scenario"],
        ctx["mes_example"],
        conv.get("post_history_instructions", ""),
        history,
        settings.get("user_name", "User"),
        settings.get("user_description", ""),
    )


async def _persist_result(
    conversation_id: str,
    res: dict,
    settings: dict,
    user_msg_id: int | None,
    turn_index: int,
) -> int | None:
    """Persist the assistant message after _result.  Returns the new assistant message id."""
    if settings.get("enable_agent", 1):
        await db.update_director_state(
            conversation_id, res["active_moods"], res.get("keywords")
        )
    if res.get("refined_msg") and user_msg_id:
        await db.update_message_content(user_msg_id, res["effective_msg"])

    asst_id = await db.add_message(
        conversation_id,
        "assistant",
        res["resp_text"],
        turn_index,
        parent_id=user_msg_id,
    )
    await db.set_active_leaf(conversation_id, asst_id)
    return asst_id


async def _fallback_persist(
    conversation_id: str,
    res: dict,
    settings: dict,
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
):
    """Best-effort save when the pipeline aborted before _result was consumed."""
    try:
        if res.get("active_moods") and settings.get("enable_agent", 1):
            await db.update_director_state(
                conversation_id, res["active_moods"], res.get("keywords")
            )
        if res.get("refined_msg") and user_msg_id:
            await db.update_message_content(user_msg_id, res["effective_msg"])
        resp_text = res.get("resp_text", "") or accumulated_text
        if resp_text.strip():
            asst_id = await db.add_message(
                conversation_id,
                "assistant",
                resp_text,
                turn_index,
                parent_id=user_msg_id,
            )
            await db.set_active_leaf(conversation_id, asst_id)
            logger.info(
                "Fallback persistence saved incomplete assistant message (%d chars)",
                len(resp_text),
            )
    except Exception:
        logger.exception("Fallback persistence failed")


async def _shielded_fallback(
    conversation_id: str,
    res: dict,
    settings: dict,
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
):
    """Run _fallback_persist inside asyncio.shield, with a retry on CancelledError."""
    try:
        await asyncio.shield(
            _fallback_persist(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
            )
        )
    except asyncio.CancelledError:
        try:
            await _fallback_persist(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
            )
        except Exception:
            logger.exception("Fallback persistence retry failed")


async def _consume_pipeline(
    pipeline: AsyncIterator[dict],
    conversation_id: str,
    settings: dict,
    user_msg_id: int | None,
    turn_index: int,
    *,
    extra_on_result=None,
) -> AsyncIterator[dict]:
    """Shared event-consumption loop used by both handle_turn and handle_regenerate.

    *extra_on_result* is an optional async callback ``(res, asst_id) -> None``
    that runs right after the assistant message is persisted (for handle_turn's
    conversation-log write, for example).
    """
    res: dict = {}
    asst_id = None
    persisted = False
    accumulated_text = ""

    try:
        async for event in pipeline:
            etype = event["event"]
            if etype == "token":
                accumulated_text += event["data"]
                yield event
            elif etype == "_result":
                res = event["data"]
                asst_id = await _persist_result(
                    conversation_id, res, settings, user_msg_id, turn_index
                )
                persisted = True
                if extra_on_result:
                    await extra_on_result(res, asst_id)
            elif etype == "_refined_result":
                res["resp_text"] = event["data"]["resp_text"]
                if asst_id:
                    await db.update_message_content(asst_id, res["resp_text"])
            else:
                yield event
    finally:
        if not persisted:
            await _shielded_fallback(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
            )

    yield {"event": "done"}


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_turn(
    conversation_id: str, user_message: str, skip_user_persist: bool = False
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        settings = ctx["settings"]
        messages = await db.get_messages(conversation_id)
        conv = ctx["conv"]

        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]

        # Save user message BEFORE pipeline
        if not skip_user_persist:
            user_msg_id = await db.add_message(
                conversation_id,
                "user",
                user_message,
                next_turn,
                parent_id=user_parent_id,
            )
            await db.set_active_leaf(conversation_id, user_msg_id)

        prefix = _build_prefix_from_ctx(ctx, history)
        asst_turn = next_turn + (0 if skip_user_persist else 1)

        async def _on_result(res, asst_id):
            await db.add_conversation_log(
                conversation_id,
                next_turn,
                res["agent_raw"],
                res["calls"],
                res["active_moods"],
                res["inj_block"],
                res["latency"],
            )

        pipeline = _run_pipeline(
            ctx["client"],
            settings,
            ctx["director"],
            ctx["fragments"],
            prefix,
            user_message,
            ctx["phrase_bank"],
        )
        async for event in _consume_pipeline(
            pipeline,
            conversation_id,
            settings,
            user_msg_id,
            asst_turn,
            extra_on_result=_on_result,
        ):
            yield event

    except Exception as e:
        logger.exception("Pipeline error")
        yield {"event": "error", "data": str(e)}


async def handle_regenerate(
    conversation_id: str, assistant_msg_id: int
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        settings = ctx["settings"]
        target = await db.get_message_by_id(assistant_msg_id)
        if (
            not target
            or target["conversation_id"] != conversation_id
            or target["role"] != "assistant"
        ):
            yield {"event": "error", "data": "Invalid target message"}
            return

        user_msg_id = target["parent_id"]
        user_msg = await db.get_message_by_id(user_msg_id) if user_msg_id else None
        if not user_msg:
            yield {"event": "error", "data": "Parent user message not found"}
            return

        history = (
            await db._get_path_to_leaf(conversation_id, user_msg.get("parent_id"))
            if user_msg.get("parent_id")
            else []
        )
        prefix = _build_prefix_from_ctx(ctx, history)

        pipeline = _run_pipeline(
            ctx["client"],
            settings,
            ctx["director"],
            ctx["fragments"],
            prefix,
            user_msg["content"],
            ctx["phrase_bank"],
        )
        async for event in _consume_pipeline(
            pipeline, conversation_id, settings, user_msg_id, target["turn_index"]
        ):
            yield event

    except Exception as e:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": str(e)}
