"""Retry settings round-trip through the /api/settings stack.

Guards the wiring a new settings column silently depends on: the field must be on
``SettingsUpdate`` (or the PUT drops it as an extra field) and in
``update_settings``' allowlist (or the write is a no-op), and
``RetryPolicy.from_settings`` must read the persisted row back correctly.
"""

from __future__ import annotations

from backend.database.queries.settings import get_settings
from backend.inference.retry import RetryPolicy


async def test_retry_settings_default_off(client):
    s = (await client.get("/api/settings")).json()
    assert s["retry_enabled"] == 0
    assert s["retry_count"] == 10
    assert s["retry_delay_seconds"] == 5


async def test_retry_settings_roundtrip_and_policy(client):
    updated = (
        await client.put(
            "/api/settings",
            json={"retry_enabled": True, "retry_count": 3, "retry_delay_seconds": 2},
        )
    ).json()
    assert updated["retry_enabled"] == 1
    assert updated["retry_count"] == 3
    assert updated["retry_delay_seconds"] == 2

    policy = RetryPolicy.from_settings(await get_settings())
    assert policy.enabled is True
    assert policy.count == 3
    assert policy.delay == 2.0
