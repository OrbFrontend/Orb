from __future__ import annotations

from typing import cast

from ..connection import _build_set_clause, get_db
from ..models import EndpointRow, ModelConfigRow


async def get_endpoints() -> list[EndpointRow]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, url, api_key, active_model_config_id, agent_active_model_config_id, completion_mode, proxy FROM endpoints ORDER BY id ASC"
            )
        )
        return [cast(EndpointRow, dict(r)) for r in rows]


async def get_endpoint(endpoint_id: int) -> EndpointRow | None:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, url, api_key, active_model_config_id, agent_active_model_config_id, completion_mode, proxy FROM endpoints WHERE id = ?",
                (endpoint_id,),
            )
        )
        return cast(EndpointRow, dict(rows[0])) if rows else None


async def create_endpoint(url: str, api_key: str = "") -> EndpointRow:
    async with get_db() as db:
        cur = await db.execute("INSERT INTO endpoints (url, api_key) VALUES (?, ?)", (url, api_key))
        endpoint_id = cur.lastrowid
        cur_w = await db.execute(
            "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, 'default', '', 0.8, 0.0, 40, 0.95, 1.0, 4096, 'writer')",
            (endpoint_id,),
        )
        cur_a = await db.execute(
            "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, 'default', '', 0.8, 0.0, 40, 0.95, 1.0, 4096, 'agent')",
            (endpoint_id,),
        )
        await db.execute(
            "UPDATE endpoints SET active_model_config_id = ?, agent_active_model_config_id = ? WHERE id = ?",
            (cur_w.lastrowid, cur_a.lastrowid, endpoint_id),
        )
        await db.commit()
        rows = list(
            await db.execute_fetchall(
                "SELECT id, url, api_key, active_model_config_id, agent_active_model_config_id, completion_mode, proxy FROM endpoints WHERE id = ?",
                (endpoint_id,),
            )
        )
        return cast(EndpointRow, dict(rows[0]))


async def update_endpoint(endpoint_id: int, data: dict) -> EndpointRow | None:
    async with get_db() as db:
        allowed = [
            "url",
            "api_key",
            "active_model_config_id",
            "agent_active_model_config_id",
            "completion_mode",
            "proxy",
        ]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            vals.append(endpoint_id)
            await db.execute(
                f"UPDATE endpoints SET {', '.join(sets)} WHERE id = ?",  # nosec B608
                vals,
            )
            await db.commit()
        rows = list(
            await db.execute_fetchall(
                "SELECT id, url, api_key, active_model_config_id, agent_active_model_config_id, completion_mode, proxy FROM endpoints WHERE id = ?",
                (endpoint_id,),
            )
        )
        return cast(EndpointRow, dict(rows[0])) if rows else None


async def delete_endpoint(endpoint_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_model_configs(endpoint_id: int) -> list[ModelConfigRow]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM model_configs WHERE endpoint_id = ? ORDER BY id ASC",
                (endpoint_id,),
            )
        )
        return [cast(ModelConfigRow, dict(r)) for r in rows]


async def create_model_config(endpoint_id: int, data: dict) -> ModelConfigRow:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role, reasoning_effort, reasoning_effort_param, reasoning_effort_value, extra_headers, extra_body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                endpoint_id,
                data.get("model_name", "default"),
                data.get("system_prompt", ""),
                data.get("temperature", 0.8),
                data.get("min_p", 0.0),
                data.get("top_k", 40),
                data.get("top_p", 0.95),
                data.get("repetition_penalty", 1.0),
                data.get("max_tokens", 4096),
                data.get("role", "writer"),
                data.get("reasoning_effort", ""),
                data.get("reasoning_effort_param", ""),
                data.get("reasoning_effort_value", ""),
                data.get("extra_headers", ""),
                data.get("extra_body", ""),
            ),
        )
        await db.commit()
        rows = list(await db.execute_fetchall("SELECT * FROM model_configs WHERE id = ?", (cur.lastrowid,)))
        return cast(ModelConfigRow, dict(rows[0]))


async def update_model_config(config_id: int, data: dict) -> ModelConfigRow | None:
    async with get_db() as db:
        allowed = [
            "model_name",
            "system_prompt",
            "temperature",
            "min_p",
            "top_k",
            "top_p",
            "repetition_penalty",
            "max_tokens",
            "reasoning_effort",
            "reasoning_effort_param",
            "reasoning_effort_value",
            "extra_headers",
            "extra_body",
        ]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            vals.append(config_id)
            await db.execute(
                f"UPDATE model_configs SET {', '.join(sets)} WHERE id = ?",  # nosec B608
                vals,
            )
            await db.commit()
        rows = list(await db.execute_fetchall("SELECT * FROM model_configs WHERE id = ?", (config_id,)))
        return cast(ModelConfigRow, dict(rows[0])) if rows else None


async def delete_model_config(config_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM model_configs WHERE id = ?", (config_id,))
        await db.commit()
        return cur.rowcount > 0
