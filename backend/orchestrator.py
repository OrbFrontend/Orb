"""
orchestrator.py — Pipeline coordinator: director → writer → editor,
plus the public entry points handle_turn() and handle_regenerate().
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, List, Optional

from . import database as db
from .llm_client import LLMClient, reasoning_cfg
from .endpoint_profiles import profile_for
from .tool_defs import TOOLS, POST_WRITER_TOOLS, build_direct_scene_tool
from .prompt_builder import (
    build_prefix,
    compute_style_injection_block,
    compute_lorebook_injection_block,
)
from .kv_tracker import _KVCacheTracker
from .macros import Macros
from .utils import extract_hyperparams
from .passes.director import _director_pass
from .passes.writer import _writer_pass, build_writer_content
from .passes.editor import editor_pass

logger = logging.getLogger(__name__)


# ── Core pipeline ─────────────────────────────────────────────────────────────


async def _run_pipeline(
    client: LLMClient,
    settings: dict,
    director: dict,
    mood_fragments: list[dict],
    director_fragments: list[dict],
    prefix: list[dict],
    user_message: str,
    attachments: Optional[List[dict]] = None,
    phrase_bank: list[list[str]] | None = None,
    lorebook_block: str = "",
    editor_audit_msgs: list[str] | None = None,
    agent_client: LLMClient | None = None,
    agent_prefix: list[dict] | None = None,
    macros: Macros | None = None,
    conversation_id: str | None = None,
) -> AsyncIterator[dict]:
    """Three-pass pipeline: director → writer → editor.

    KV cache strategy: *prefix* (system prompt + chat history) and the tool
    schema list returned by ``enabled_schemas(enabled_tools)`` are kept
    identical across all three passes so the LLM can reuse cached KV entries.
    Only ``tool_choice`` and the trailing user message differ per pass.
    ``editor_rewrite`` is included in the schema set whenever the length guard
    is enabled (mirroring how ``editor_apply_patch`` tracks ``audit_enabled``).
    """
    if macros is None:
        macros = Macros("User", "")
    if attachments is None:
        attachments = []

    user_message = macros.resolve_message(user_message)

    enabled_tools = settings.get("enabled_tools") or {}
    agent_on = bool(settings.get("enable_agent", 1))
    if not agent_on:
        enabled_tools = {}

    reasoning_passes = settings.get("reasoning_enabled_passes") or {}
    director_reasoning_on = bool(reasoning_passes.get("director", True))
    writer_reasoning_on = bool(reasoning_passes.get("writer", False))
    editor_reasoning_on = bool(reasoning_passes.get("editor", False))

    active_moods = director["active_moods"]
    _valid_progressive_ids = {df["id"] for df in director_fragments if df.get("field_type") == "progressive"}
    progressive_state: dict = {k: v for k, v in director.get("progressive_fields", {}).items() if k in _valid_progressive_ids}
    agent_raw, calls, latency = "", [], 0
    rewritten_msg: str | None = None
    extra_fields: dict = {}
    reasoning_director_text = ""
    reasoning_writer_text = ""
    reasoning_editor_text = ""
    progressive_fields: dict = {}
    effective_msg = user_message

    audit_enabled = agent_on and bool(enabled_tools.get("editor_apply_patch", False)) and phrase_bank is not None

    # Length guard
    length_guard_enabled = bool(enabled_tools.get("length_guard", False)) if agent_on else False
    # Mirror editor_rewrite into enabled_tools so enabled_schemas() includes its schema in all
    # three passes — same KV-cache consistency approach as editor_apply_patch.
    if length_guard_enabled:
        enabled_tools = {**enabled_tools, "editor_rewrite": True}
    length_guard_enforce = bool(enabled_tools.get("length_guard_enforce", False)) if agent_on else False

    length_guard = (
        {
            "enabled": length_guard_enabled,
            "max_words": int(settings.get("length_guard_max_words", 240)),
            "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
        }
        if (length_guard_enabled and agent_on)
        else None
    )

    do_edit = audit_enabled or (length_guard_enabled and agent_on)

    # When the agent runs on a separate model/endpoint, its KV cache is disjoint
    # from the writer's.  Skip tool schemas and the OOC "no tools" notice from the
    # writer call — neither is useful and both add unnecessary tokens.
    agent_is_separate = agent_client is not None
    writer_enabled_tools = {} if agent_is_separate else enabled_tools

    def _wrap(c):
        return macros.wrap_client(c)

    director_client = _wrap(agent_client or client)
    editor_client = _wrap(agent_client or client)
    writer_client = _wrap(client)
    director_prefix = agent_prefix or prefix
    editor_prefix = agent_prefix or prefix
    agent_model = settings.get("agent_model_name", settings["model_name"]) if agent_client else settings["model_name"]

    kv_tracker = _KVCacheTracker(conversation_id=conversation_id)

    # All three passes must send byte-identical tools; direct_scene is dynamic
    # so it's built once here and shared via schema_overrides.
    schema_overrides = {
        "direct_scene": build_direct_scene_tool(director_fragments),
    }

    # --- Director pass ---
    has_pre_writer_tools = any(enabled_tools.get(n, False) for n in TOOLS if n not in POST_WRITER_TOOLS)
    if agent_on and has_pre_writer_tools:
        yield {"event": "director_start"}
        async for event in _director_pass(
            director_client,
            director_prefix,
            user_message,
            settings,
            director,
            mood_fragments,
            director_fragments,
            enabled_tools,
            attachments=attachments,
            kv_tracker=kv_tracker,
            reasoning_on=director_reasoning_on,
            lorebook_block=lorebook_block,
            model=agent_model,
            progressive_state=progressive_state,
            schema_overrides=schema_overrides,
        ):
            if event["type"] == "reasoning":
                reasoning_director_text += event["delta"]
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
                    rewritten_msg,
                    extra_fields,
                ) = event["result"]
                progressive_ids = {df["id"] for df in director_fragments if df["field_type"] == "progressive"}
                progressive_fields = {k: v for k, v in extra_fields.items() if k in progressive_ids}
        if rewritten_msg:
            effective_msg = rewritten_msg
            yield {
                "event": "prompt_rewritten",
                "data": {"refined_message": rewritten_msg},
            }

    # Bail out if stop was clicked during the director pass
    if client.is_aborted or (agent_client is not None and agent_client.is_aborted):
        return

    # Style injection
    direct_scene_enabled = agent_on and bool(enabled_tools.get("direct_scene", False))
    inj_block = macros.resolve_message(
        compute_style_injection_block(
            active_moods,
            director["active_moods"],
            mood_fragments,
            director_fragments,
            direct_scene_enabled,
            extra_fields,
            progressive_state,
        )
    )

    yield {
        "event": "director_done",
        "data": {
            "active_moods": active_moods,
            "injection_block": inj_block,
            "tool_calls": calls,
            "agent_latency_ms": latency,
            "extra_fields": extra_fields,
        },
    }

    # --- Writer pass ---
    writer_content = build_writer_content(
        lorebook_block,
        inj_block,
        writer_enabled_tools,
        effective_msg,
        attachments,
        length_guard_enforce,
        length_guard,
    )
    resp_text = ""
    async for item in _writer_pass(
        writer_client,
        prefix,
        settings,
        writer_enabled_tools,
        inj_block=inj_block,
        lorebook_block=lorebook_block,
        effective_msg=effective_msg,
        attachments=attachments,
        length_guard_enforce=length_guard_enforce,
        length_guard=length_guard,
        kv_tracker=kv_tracker,
        reasoning_on=writer_reasoning_on,
        schema_overrides=schema_overrides,
    ):
        if item["type"] == "reasoning":
            reasoning_writer_text += item["delta"]
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
            "rewritten_msg": rewritten_msg,
            "effective_msg": effective_msg,
            "resp_text": resp_text,
            "inj_block": inj_block,
            "extra_fields": extra_fields,
            "progressive_fields": progressive_fields,
            "reasoning_director": reasoning_director_text,
            "reasoning_writer": reasoning_writer_text,
        },
    }

    if client.is_aborted or (agent_client is not None and agent_client.is_aborted):
        return

    # --- Editor pass ---
    if do_edit and resp_text:
        logger.info(
            "Editor pass starting (draft=%d chars, phrase_bank=%d groups)",
            len(resp_text),
            len(phrase_bank) if phrase_bank else 0,
        )
        try:
            async for event in editor_pass(
                editor_client,
                editor_prefix,
                effective_msg,
                resp_text,
                settings,
                phrase_bank or [],
                enabled_tools,
                audit_enabled,
                length_guard,
                kv_tracker=kv_tracker,
                reasoning_on=editor_reasoning_on,
                audit_context_msgs=editor_audit_msgs,
                model=agent_model,
                writer_user_msg=writer_content,
                schema_overrides=schema_overrides,
            ):
                if event["type"] == "reasoning":
                    reasoning_editor_text += event["delta"]
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
        if reasoning_editor_text:
            yield {"event": "_editor_reasoning", "data": {"reasoning_editor": reasoning_editor_text}}
    elif not do_edit:
        logger.info("Editor pass skipped (do_edit=%s)", do_edit)

    kv_tracker.log_summary()


# ═══════════════════════════════════════════════════════════════════════════════
# Shared infrastructure for handle_turn / handle_regenerate
# ═══════════════════════════════════════════════════════════════════════════════


async def _load_pipeline_context(conversation_id: str) -> dict | None:
    """Load everything the pipeline needs: settings, conversation, director,
    mood_fragments, phrase_bank, and an LLMClient.

    Returns a dict of resolved objects, or None if the conversation was not found.
    """
    settings = await db.get_settings()
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None

    director = await db.get_director_state(conversation_id)
    mood_fragments = await db.get_mood_fragments()
    # Filter out disabled mood fragments
    mood_fragments = [f for f in mood_fragments if f.get("enabled", True)]
    # Remove disabled mood fragments from active moods
    if director and director.get("active_moods"):
        enabled_ids = {f["id"] for f in mood_fragments}
        director["active_moods"] = [mood for mood in director["active_moods"] if mood in enabled_ids]
    director_fragments = await db.get_director_fragments()
    director_fragments = [df for df in director_fragments if df.get("enabled", True)]
    phrase_bank = await db.get_phrase_bank()
    lorebook_entries = await db.get_active_lorebook_entries()
    client = LLMClient(
        settings["endpoint_url"],
        api_key=settings.get("api_key", ""),
        profile=profile_for(
            settings["endpoint_url"],
            settings.get("model_name", ""),
        ),
    )

    system_prompt, char_persona, mes_example = await db.resolve_char_context(conv, settings)

    # Load active persona if set
    active_persona = None
    active_persona_id = settings.get("active_persona_id")
    if active_persona_id:
        active_persona = await db.get_user_persona(active_persona_id)

    # Resolve agent client and system prompt when separate from writer
    agent_same = settings.get("agent_same_as_writer", True)
    agent_client = None
    agent_system_prompt = None
    if not agent_same and settings.get("agent_endpoint_id"):
        agent_url = settings.get("agent_endpoint_url", settings["endpoint_url"])
        agent_api_key = settings.get("agent_api_key", settings.get("api_key", ""))
        agent_model = settings.get("agent_model_name", settings.get("model_name", ""))
        agent_client = LLMClient(
            agent_url,
            api_key=agent_api_key,
            profile=profile_for(agent_url, agent_model),
        )
        agent_system_prompt, _, _ = await db.resolve_char_context(conv, settings, shared_key="agent_shared_system_prompt")

    return {
        "settings": settings,
        "conv": conv,
        "director": director,
        "mood_fragments": mood_fragments,
        "director_fragments": director_fragments,
        "phrase_bank": phrase_bank,
        "lorebook_entries": lorebook_entries,
        "client": client,
        "system_prompt": system_prompt,
        "char_persona": char_persona,
        "mes_example": mes_example,
        "active_persona": active_persona,
        "agent_client": agent_client,
        "agent_system_prompt": agent_system_prompt,
    }


def _build_prefix_from_ctx(ctx: dict, history: list[dict], *, system_prompt: str | None = None) -> list[dict]:
    """Build the LLM prefix from a pipeline-context dict.

    When *system_prompt* is provided it overrides ``ctx["system_prompt"]``
    (used for the agent prefix when it has its own system prompt).
    """
    conv = ctx["conv"]
    active_persona = ctx.get("active_persona")
    macros = Macros.from_settings(ctx["settings"], conv["character_name"], active_persona)
    user_description = active_persona.get("description", "") if active_persona else ctx["settings"].get("user_description", "")

    return build_prefix(
        system_prompt if system_prompt is not None else ctx["system_prompt"],
        ctx["char_persona"],
        conv["character_scenario"],
        ctx["mes_example"],
        ("" if ctx["settings"].get("prevent_prompt_overrides") else conv.get("post_history_instructions", "")),
        history,
        macros,
        user_description,
    )


def _build_prefixes(ctx: dict, history: list[dict]) -> tuple[list[dict], list[dict] | None]:
    """Build (prefix, agent_prefix) from *ctx* and *history*.

    *agent_prefix* is ``None`` when no separate agent system prompt is configured.
    """
    prefix = _build_prefix_from_ctx(ctx, history)
    agent_sp = ctx.get("agent_system_prompt")
    agent_prefix = _build_prefix_from_ctx(ctx, history, system_prompt=agent_sp) if agent_sp is not None else None
    return prefix, agent_prefix


def _compute_lorebook(macros: Macros, ctx: dict, messages: list[dict]) -> str:
    """Compute the lorebook injection block for a sequence of *messages*."""
    return compute_lorebook_injection_block(
        messages,
        ctx.get("lorebook_entries", []),
        macros,
    )


async def _resolve_target_and_parent(conversation_id: str, assistant_msg_id: int) -> tuple[dict, dict] | str:
    """Validate *assistant_msg_id* and load its parent user message.

    Returns ``(target, user_msg)`` on success, or an error string on failure.
    """
    target = await db.get_message_by_id(assistant_msg_id)
    if not target or target["conversation_id"] != conversation_id or target["role"] != "assistant":
        return "Invalid target message"
    user_msg_id = target["parent_id"]
    user_msg = await db.get_message_by_id(user_msg_id) if user_msg_id else None
    if not user_msg:
        return "Parent user message not found"
    return target, user_msg


async def _prepare_regen_context(
    ctx: dict, conversation_id: str, target: dict, user_msg: dict
) -> tuple[list[dict], list[dict]]:
    """Prepare history and attachments for a regeneration pass.

    Also resets director moods to the pre-turn baseline.
    Returns ``(history, attachments)``.
    """
    parent_id: int | None = user_msg.get("parent_id")
    history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []
    moods_before = await db.get_moods_before_turn(conversation_id, target["turn_index"] - 1)
    ctx["director"]["active_moods"] = moods_before
    grandparent = next((m for m in reversed(history) if m["role"] == "assistant"), None)
    ctx["director"]["progressive_fields"] = grandparent.get("progressive_fields") or {} if grandparent else {}
    user_msg_id = target["parent_id"]
    attachments = await db.get_attachments_for_message(user_msg_id) if user_msg_id else []
    return history, attachments


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
            conversation_id,
            res["active_moods"],
            progressive_fields=res.get("progressive_fields"),
        )
    if res.get("rewritten_msg") and user_msg_id:
        await db.update_message_content(user_msg_id, res["effective_msg"])

    # Only create a message if there's actual content.
    # The writer pass can produce empty resp_text if the LLM completes
    # without generating any non‑reasoning tokens (e.g., reasoning‑only mode).
    resp_text = res.get("resp_text", "")
    if resp_text.strip():
        asst_id = await db.add_message(
            conversation_id,
            "assistant",
            resp_text,
            turn_index,
            parent_id=user_msg_id,
            progressive_fields=res.get("progressive_fields"),
        )
        await db.set_active_leaf(conversation_id, asst_id)
        return asst_id
    else:
        logger.info("Skipping assistant message persistence: resp_text is empty (reasoning‑only output)")
        return None


async def _fallback_persist(
    conversation_id: str,
    res: dict,
    settings: dict,
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
):
    """Best-effort save when the pipeline aborted before _result was consumed.

    Only saves if there's actual writer output (token events). Reasoning-only
    content (director/writer/editor reasoning) does NOT create a message node,
    because reasoning deltas are yielded as 'reasoning' SSE events and are NOT
    included in accumulated_text.
    """
    try:
        if res.get("active_moods") and settings.get("enable_agent", 1):
            await db.update_director_state(
                conversation_id,
                res["active_moods"],
                progressive_fields=res.get("progressive_fields"),
            )
        if res.get("rewritten_msg") and user_msg_id:
            await db.update_message_content(user_msg_id, res["effective_msg"])

        # Only save if there's actual writer output (token events).
        # accumulated_text only contains streamed tokens from the writer pass;
        # reasoning deltas are yielded as separate 'reasoning' events and are
        # NOT included here. This prevents creating message nodes when the
        # user stops generation during the reasoning phase (no writer output).
        if accumulated_text.strip():
            asst_id = await db.add_message(
                conversation_id,
                "assistant",
                accumulated_text,
                turn_index,
                parent_id=user_msg_id,
            )
            await db.set_active_leaf(conversation_id, asst_id)
            logger.info(
                "Fallback persistence saved incomplete assistant message (%d chars)",
                len(accumulated_text),
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
    log_saved = False
    accumulated_text = ""

    try:
        async for event in pipeline:
            etype = event["event"]
            if etype == "token":
                accumulated_text += event["data"]
                yield event
            elif etype == "_result":
                res = event["data"]
                asst_id = await _persist_result(conversation_id, res, settings, user_msg_id, turn_index)
                persisted = True
            elif etype == "_editor_reasoning":
                res["reasoning_editor"] = event["data"]["reasoning_editor"]
            elif etype == "_refined_result":
                res["resp_text"] = event["data"]["resp_text"]
                if asst_id:
                    await db.update_message_content(asst_id, res["resp_text"])
            else:
                yield event
        if extra_on_result and persisted:
            await extra_on_result(res, asst_id)
            log_saved = True
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
        elif extra_on_result and not log_saved:
            try:
                await extra_on_result(res, asst_id)
            except Exception:
                logger.exception("Failed to save conversation log after pipeline abort")

    yield {"event": "done"}


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_turn(
    conversation_id: str,
    user_message: str,
    skip_user_persist: bool = False,
    attachments: Optional[List[dict]] = None,
    client_ref: list | None = None,
) -> AsyncIterator[dict]:
    try:
        if attachments is None:
            attachments = []
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        if client_ref is not None:
            client_ref.append(ctx["client"])
            if ctx.get("agent_client"):
                client_ref.append(ctx["agent_client"])

        settings = ctx["settings"]
        messages = await db.get_messages(conversation_id)
        conv = ctx["conv"]

        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]

        # Derive progressive_fields from the grandparent message node (branch-aware)
        # rather than conversation_logs which are indexed by turn_index and can
        # return data from a different branch after a branch switch.
        grandparent = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        ctx["director"]["progressive_fields"] = grandparent.get("progressive_fields") or {} if grandparent else {}

        # Save user message BEFORE pipeline
        if not skip_user_persist:
            # Convert frontend attachment format to database format
            db_attachments = []
            for att in attachments:
                db_attachments.append(
                    {
                        "mime_type": att.get("mime", att.get("mime_type", "image/jpeg")),
                        "data_b64": att.get("b64", att.get("data_b64", "")),
                        "filename": att.get("filename"),
                        "size": att.get("size"),
                    }
                )
            user_msg_id = await db.add_message(
                conversation_id,
                "user",
                user_message,
                next_turn,
                parent_id=user_parent_id,
                attachments=db_attachments,
            )
            await db.set_active_leaf(conversation_id, user_msg_id)
            yield {"event": "user_message_created", "data": {"id": user_msg_id}}

        prefix, agent_prefix = _build_prefixes(ctx, history)
        asst_turn = next_turn + (0 if skip_user_persist else 1)

        macros = Macros.from_settings(ctx["settings"], ctx["conv"]["character_name"], ctx.get("active_persona"))

        # Compute lorebook injection — include the current user message so its
        # keywords are scanned, not just prior history.
        lorebook_block = _compute_lorebook(macros, ctx, history + [{"role": "user", "content": user_message}])

        async def _on_result(res, asst_id):
            await db.add_conversation_log(
                conversation_id,
                next_turn,
                res["agent_raw"],
                res["calls"],
                res["active_moods"],
                res["inj_block"],
                res["latency"],
                res.get("progressive_fields"),
                message_id=asst_id,
                reasoning_director=res.get("reasoning_director", ""),
                reasoning_writer=res.get("reasoning_writer", ""),
                reasoning_editor=res.get("reasoning_editor", ""),
            )

        pipeline = _run_pipeline(
            ctx["client"],
            settings,
            ctx["director"],
            ctx["mood_fragments"],
            ctx["director_fragments"],
            prefix,
            user_message,
            attachments=attachments,
            phrase_bank=ctx["phrase_bank"],
            lorebook_block=lorebook_block,
            agent_client=ctx.get("agent_client"),
            agent_prefix=agent_prefix,
            macros=macros,
            conversation_id=conversation_id,
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
    conversation_id: str,
    assistant_msg_id: int,
    client_ref: list | None = None,
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        if client_ref is not None:
            client_ref.append(ctx["client"])
            if ctx.get("agent_client"):
                client_ref.append(ctx["agent_client"])

        settings = ctx["settings"]
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        user_msg_id = target["parent_id"]
        history, attachments = await _prepare_regen_context(ctx, conversation_id, target, user_msg)
        prefix, agent_prefix = _build_prefixes(ctx, history)

        macros = Macros.from_settings(ctx["settings"], ctx["conv"]["character_name"], ctx.get("active_persona"))
        lorebook_block = _compute_lorebook(macros, ctx, history + [{"role": "user", "content": user_msg["content"]}])

        pipeline = _run_pipeline(
            ctx["client"],
            settings,
            ctx["director"],
            ctx["mood_fragments"],
            ctx["director_fragments"],
            prefix,
            user_msg["content"],
            attachments,
            ctx["phrase_bank"],
            lorebook_block=lorebook_block,
            agent_client=ctx.get("agent_client"),
            agent_prefix=agent_prefix,
            macros=macros,
            conversation_id=conversation_id,
        )

        async def _on_result(res, asst_id):
            await db.add_conversation_log(
                conversation_id,
                target["turn_index"],
                res["agent_raw"],
                res["calls"],
                res["active_moods"],
                res["inj_block"],
                res["latency"],
                res.get("progressive_fields"),
                message_id=asst_id,
                reasoning_director=res.get("reasoning_director", ""),
                reasoning_writer=res.get("reasoning_writer", ""),
                reasoning_editor=res.get("reasoning_editor", ""),
            )

        async for event in _consume_pipeline(
            pipeline,
            conversation_id,
            settings,
            user_msg_id,
            target["turn_index"],
            extra_on_result=_on_result,
        ):
            yield event

    except Exception as e:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": str(e)}


_SUPER_REGEN_MSG = "[OOC: Your response was kind of meh, rewrite it in a slightly different but still realistic direction.]"


async def handle_super_regenerate(
    conversation_id: str,
    assistant_msg_id: int,
    client_ref: list | None = None,
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        if client_ref is not None:
            client_ref.append(ctx["client"])
            if ctx.get("agent_client"):
                client_ref.append(ctx["agent_client"])

        settings = ctx["settings"]
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        user_msg_id = target["parent_id"]
        history, attachments = await _prepare_regen_context(ctx, conversation_id, target, user_msg)

        # Extend history to include the original user+assistant exchange so the
        # model sees what it wrote before being asked to go a different direction.
        extended_history = history + [
            {"role": "user", "content": user_msg["content"]},
            {"role": "assistant", "content": target["content"]},
        ]
        prefix, agent_prefix = _build_prefixes(ctx, extended_history)

        # rewrite_user_prompt must not alter the OOC steering message.
        super_regen_settings = {
            **settings,
            "enabled_tools": {
                **(settings.get("enabled_tools") or {}),
                "rewrite_user_prompt": False,
            },
        }

        macros = Macros.from_settings(ctx["settings"], ctx["conv"]["character_name"], ctx.get("active_persona"))
        lorebook_block = _compute_lorebook(
            macros,
            ctx,
            extended_history + [{"role": "user", "content": _SUPER_REGEN_MSG}],
        )

        # Collect audit context from history only — exclude target["content"] so
        # the editor doesn't flag the new draft for repeating the message it replaced.
        editor_audit_msgs = [msg["content"] for msg in reversed(history) if msg.get("role") == "assistant"][:3]

        pipeline = _run_pipeline(
            ctx["client"],
            super_regen_settings,
            ctx["director"],
            ctx["mood_fragments"],
            ctx["director_fragments"],
            prefix,
            _SUPER_REGEN_MSG,
            attachments,
            ctx["phrase_bank"],
            lorebook_block=lorebook_block,
            editor_audit_msgs=editor_audit_msgs,
            agent_client=ctx.get("agent_client"),
            agent_prefix=agent_prefix,
            macros=macros,
            conversation_id=conversation_id,
        )

        async def _on_result(res, asst_id):
            await db.add_conversation_log(
                conversation_id,
                target["turn_index"],
                res["agent_raw"],
                res["calls"],
                res["active_moods"],
                res["inj_block"],
                res["latency"],
                res.get("progressive_fields"),
                message_id=asst_id,
                reasoning_director=res.get("reasoning_director", ""),
                reasoning_writer=res.get("reasoning_writer", ""),
                reasoning_editor=res.get("reasoning_editor", ""),
            )

        # Save result as a sibling of the original: same parent_id and turn_index.
        async for event in _consume_pipeline(
            pipeline,
            conversation_id,
            settings,
            user_msg_id,
            target["turn_index"],
            extra_on_result=_on_result,
        ):
            yield event

    except Exception as e:
        logger.exception("Super-regenerate error")
        yield {"event": "error", "data": str(e)}


async def handle_magic_rewrite(
    conversation_id: str,
    assistant_msg_id: int,
    direction: str,
    client_ref: list | None = None,
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        if client_ref is not None:
            client_ref.append(ctx["client"])

        settings = ctx["settings"]
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        parent_id: int | None = user_msg.get("parent_id")
        history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []

        extended_history = history + [
            {"role": "user", "content": user_msg["content"]},
            {"role": "assistant", "content": target["content"]},
        ]
        prefix = _build_prefix_from_ctx(ctx, extended_history)

        direction_msg = f"[OOC: Rewrite the above response. Direction: {direction}]"
        msgs = prefix + [{"role": "user", "content": direction_msg}]

        hyperparams = extract_hyperparams(settings)

        writer_reasoning_on = bool((settings.get("reasoning_enabled_passes") or {}).get("writer", False))
        extra = reasoning_cfg(writer_reasoning_on)

        accumulated = ""
        async for item in ctx["client"].complete(messages=msgs, model=settings["model_name"], **extra, **hyperparams):
            if item["type"] == "done":
                break
            if item["type"] == "content":
                accumulated += item["delta"]
                yield {"event": "token", "data": item["delta"]}

        if accumulated.strip():
            await db.update_message_content(assistant_msg_id, accumulated)

    except Exception as e:
        logger.exception("Magic rewrite error")
        yield {"event": "error", "data": str(e)}

    yield {"event": "done"}
