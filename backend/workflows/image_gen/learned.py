"""Persist provider-specific bounds learned during image renders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..toolkit import get_workflow_config, set_workflow_config, workflow_config_lock
from .config import WORKFLOW_ID
from .engine.degrade import LEARNABLE

SLOT = f"{WORKFLOW_ID}:learned"
MAX_TARGETS = 128
MAX_SIZES = 32


def target_key(source: str, provider: str, model: str) -> str:
    return " ".join((source, provider, model))


def _sizes(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in list(value.items())[-MAX_SIZES:] if isinstance(v, str)}


def _entry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    entry = {
        name: raw[name]
        for name in LEARNABLE
        if name != "sizes" and isinstance(raw.get(name), int) and not isinstance(raw.get(name), bool)
    }
    sizes = _sizes(raw.get("sizes"))
    if sizes:
        entry["sizes"] = sizes
    return entry


def _store(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): entry for key, value in raw.items() if (entry := _entry(value))}


async def recall(key: str) -> dict[str, Any]:
    return _store(await get_workflow_config(SLOT)).get(key, {})


async def remember(key: str, learned: Mapping[str, Any] | None) -> None:
    incoming = _entry(learned)
    if not incoming:
        return
    async with workflow_config_lock():
        store = _store(await get_workflow_config(SLOT))
        entry = store.get(key, {})
        merged = {**entry, **incoming}
        if "sizes" in entry or "sizes" in incoming:
            merged["sizes"] = {**entry.get("sizes", {}), **incoming.get("sizes", {})}
        merged = _entry(merged)
        if merged == entry:
            return
        store.pop(key, None)
        store[key] = merged
        store = dict(list(store.items())[-MAX_TARGETS:])
        await set_workflow_config(SLOT, store)
