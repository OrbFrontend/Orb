from __future__ import annotations

import json
from typing import cast

from ..connection import _build_set_clause, get_db
from ..models import SettingsRow
from ..seeds import DEFAULT_SETTINGS


async def get_settings() -> SettingsRow:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM settings WHERE id = 1"))
        if not rows:
            return cast(SettingsRow, DEFAULT_SETTINGS)
        s = dict(rows[0])
        s["enabled_tools"] = json.loads(s.get("enabled_tools") or "{}")
        s["reasoning_enabled_passes"] = json.loads(
            s.get("reasoning_enabled_passes") or '{"director":true,"writer":false,"editor":false}'
        )
        # Remove stale scripter key from reasoning_enabled_passes if present.
        s["reasoning_enabled_passes"].pop("scripter", None)
        s["inspector_open_states"] = json.loads(
            s.get("inspector_open_states")
            or '{"reasoning":true,"tool_calls":false,"injection_block":false,"context_size":true}'
        )
        s["editor_audit_toggles"] = json.loads(
            s.get("editor_audit_toggles")
            or '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,'
            '"contrastive_negation":true,"phrase_repetition":true,"structural_repetition":true}'
        )
        # Overlay endpoint_url, api_key, model_name, and hyperparameters from the
        # active endpoint's active model config so callers always get live values
        # rather than the stale flat columns.
        active_ep_id = s.get("active_endpoint_id")
        if active_ep_id:
            ep_rows = list(
                await db.execute_fetchall(
                    "SELECT id, url, api_key, active_model_config_id FROM endpoints WHERE id = ?",
                    (active_ep_id,),
                )
            )
            if ep_rows:
                ep = dict(ep_rows[0])
                mc_id = ep.get("active_model_config_id")
                if mc_id:
                    mc_rows = list(
                        await db.execute_fetchall(
                            """SELECT mc.*, e.url AS endpoint_url, e.api_key
                           FROM model_configs mc
                           JOIN endpoints e ON mc.endpoint_id = e.id
                           WHERE mc.id = ?""",
                            (mc_id,),
                        )
                    )
                    if mc_rows:
                        mc = dict(mc_rows[0])
                        s["endpoint_url"] = mc["endpoint_url"]
                        s["api_key"] = mc.get("api_key", "")
                        s["model_name"] = mc["model_name"]
                        for field in (
                            "temperature",
                            "min_p",
                            "top_k",
                            "top_p",
                            "repetition_penalty",
                            "max_tokens",
                        ):
                            if mc.get(field) is not None:
                                s[field] = mc[field]
                        if mc.get("system_prompt") is not None:
                            s["system_prompt"] = mc["system_prompt"]

        # Resolve agent endpoint cascade
        s["agent_same_as_writer"] = bool(s.get("agent_same_as_writer", 1))
        s["agent_endpoint_id"] = s.get("agent_endpoint_id")
        s["agent_shared_system_prompt"] = s.get("agent_shared_system_prompt", "")
        agent_ep_id = s.get("agent_endpoint_id")
        if not s["agent_same_as_writer"] and agent_ep_id:
            agent_ep_rows = list(
                await db.execute_fetchall(
                    "SELECT id, url, api_key, active_model_config_id, agent_active_model_config_id FROM endpoints WHERE id = ?",
                    (agent_ep_id,),
                )
            )
            if agent_ep_rows:
                agent_ep = dict(agent_ep_rows[0])
                agent_mc_id = agent_ep.get("agent_active_model_config_id")
                if agent_mc_id:
                    agent_mc_rows = list(
                        await db.execute_fetchall(
                            """SELECT mc.*, e.url AS endpoint_url, e.api_key
                           FROM model_configs mc
                           JOIN endpoints e ON mc.endpoint_id = e.id
                           WHERE mc.id = ?""",
                            (agent_mc_id,),
                        )
                    )
                    if agent_mc_rows:
                        amc = dict(agent_mc_rows[0])
                        s["agent_endpoint_url"] = amc["endpoint_url"]
                        s["agent_api_key"] = amc.get("api_key", "")
                        s["agent_model_name"] = amc["model_name"]
                        for field in (
                            "temperature",
                            "min_p",
                            "top_k",
                            "top_p",
                            "repetition_penalty",
                            "max_tokens",
                        ):
                            if amc.get(field) is not None:
                                s[f"agent_{field}"] = amc[field]
                        if amc.get("system_prompt") is not None:
                            s["agent_system_prompt"] = amc["system_prompt"]
        return cast(SettingsRow, s)


# Empty slot returns {} here; per-workflow default fallback lives in the
# registry wrapper that owns the Workflow objects, so this layer stays free
# of upward imports into the workflow package.


async def get_workflow_config(workflow_id: str) -> dict:
    """Return the workflow's slot, or {} if the slot is empty."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT json_extract(workflow_config, '$.' || ?) AS slot FROM settings WHERE id = 1",
                (workflow_id,),
            )
        )
        if not rows:
            return {}
        slot = rows[0]["slot"]
        if slot is None:
            return {}
        return json.loads(slot)


async def set_workflow_config(workflow_id: str, payload: dict) -> None:
    """Atomic per-slot write via SQLite JSON1.

    Empty dict clears the slot (json_remove); non-empty stores it (json_set).

    Caller must hold ``backend.core.locks.workflow_config_lock()`` across the
    read-then-write the payload was computed from. Direct use without the
    lock is safe for blind-replace writes -- a single ``json_set`` is
    atomic at the SQL layer -- but RMW sequences (``get_workflow_config``
    -> mutate -> ``set_workflow_config``) silently lose writes under
    contention because the read happens in a separate transaction outside
    the lock window.
    """
    async with get_db() as db:
        if not payload:
            await db.execute(
                "UPDATE settings SET workflow_config = json_remove(COALESCE(workflow_config, '{}'), '$.' || ?) WHERE id = 1",
                (workflow_id,),
            )
        else:
            await db.execute(
                "UPDATE settings "
                "SET workflow_config = json_set(COALESCE(workflow_config, '{}'), '$.' || ?, json(?)) "
                "WHERE id = 1",
                (workflow_id, json.dumps(payload)),
            )
        await db.commit()


async def update_settings(data: dict) -> SettingsRow:
    async with get_db() as db:
        allowed = [
            "endpoint_url",
            "api_key",
            "model_name",
            "temperature",
            "min_p",
            "top_k",
            "top_p",
            "repetition_penalty",
            "max_tokens",
            "shared_system_prompt",
            "system_prompt",
            "user_name",
            "user_description",
            "enabled_tools",
            "enable_agent",
            "length_guard_max_words",
            "length_guard_max_paragraphs",
            "length_guard_enabled",
            "length_guard_enforce",
            "agentic_lorebook_enabled",
            "reasoning_enabled_passes",
            "active_persona_id",
            "character_library_view",
            "character_library_sort",
            "active_endpoint_id",
            "show_editor_diff",
            "editor_audit_toggles",
            "hide_streaming_until_baked",
            "prevent_prompt_overrides",
            "agent_same_as_writer",
            "agent_endpoint_id",
            "agent_shared_system_prompt",
            "feedback_enabled",
            "inspector_open_states",
        ]
        sets, vals = _build_set_clause(
            allowed,
            data,
            json_fields={"enabled_tools", "reasoning_enabled_passes", "inspector_open_states", "editor_audit_toggles"},
        )
        if sets:
            await db.execute(
                f"UPDATE settings SET {', '.join(sets)} WHERE id = 1",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_settings()
