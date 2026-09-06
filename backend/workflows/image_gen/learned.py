"""Remember what a provider's refusals revealed about one render target.

Bounds only, in the shape `engine/degrade.LEARNABLE` names: how large a seed the
target takes, and what size it rendered when asked for another. Never a capability --
see the note on `Rung.learned` for why the two are not the same kind of fact.

Nothing here is keyed by a model in the codebase. Every key is written at runtime from
what a provider said about a model nobody enumerated, which is the point: the
alternative is a table that has to stay right about catalogues that grow without us.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..toolkit import get_workflow_config, set_workflow_config, workflow_config_lock
from .config import WORKFLOW_ID
from .engine.degrade import LEARNABLE

# A slot of its own, beside the workflow's config rather than inside it. The settings
# panel PUTs `image_gen` as a *full replacement*, so anything learned that lived in
# there would be erased whenever the user saved a setting -- including the save that
# changes the model, which is exactly when a head start is worth the most.
SLOT = f"{WORKFLOW_ID}:learned"

# Bounded like every other stored map in this workflow, and evicted oldest-first. A
# catalogue can hold hundreds of models and a user may try many; what must not happen
# is a settings column that grows forever because nobody bounded it.
MAX_TARGETS = 128
# Enough for a generous menu of sizes per target, in case a provider lists many.
MAX_SIZES = 32


def target_key(source: str, provider: str, model: str) -> str:
    """What a learned bound is filed under: the backend, the connection and the model.

    All three, because none alone identifies the thing that refused. Two connections
    can name the same provider preset, and the same model id behind two providers is
    two different deployments that need not agree about anything.
    """
    return " ".join((source, provider, model))


def _sizes(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in list(value.items())[-MAX_SIZES:] if isinstance(v, str)}


def _entry(raw: Any) -> dict[str, Any]:
    """One stored entry, reduced to the keys `LEARNABLE` allows.

    An allowlist rather than a filter: this is read back into live requests, and a key
    that arrives here by any route other than a rung must not reach one.
    """
    if not isinstance(raw, Mapping):
        return {}
    entry: dict[str, Any] = {}
    for name in LEARNABLE:
        value = raw.get(name)
        if name == "sizes":
            sizes = _sizes(value)
            if sizes:
                entry["sizes"] = sizes
        elif isinstance(value, int) and not isinstance(value, bool):
            entry[name] = value
    return entry


def _store(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): entry for key, value in raw.items() if (entry := _entry(value))}


async def recall(key: str) -> dict[str, Any]:
    """Everything already learned about one target. `{}` when it is new, which is the
    ordinary case and costs the caller one refusal to fix."""
    return _store(await get_workflow_config(SLOT)).get(key, {})


async def remember(key: str, learned: Mapping[str, Any] | None) -> None:
    """Merge what a render just learned about `key` into the store.

    Read-modify-write under the config lock, per `set_workflow_config`: without it two
    renders finishing together silently lose one of the two findings.

    Merged rather than replaced, because one render only ever learns what its own
    refusals happened to cover -- a render refused over the seed says nothing about the
    size, and must not erase what an earlier one found out about it.
    """
    incoming = _entry(learned)
    if not incoming:
        return
    async with workflow_config_lock():
        store = _store(await get_workflow_config(SLOT))
        entry = store.pop(key, {})
        merged = {**entry, **incoming}
        if "sizes" in entry or "sizes" in incoming:
            merged["sizes"] = {**entry.get("sizes", {}), **incoming.get("sizes", {})}
        merged = _entry(merged)
        if merged == entry:
            store[key] = entry
            return
        # Re-inserted at the end and trimmed from the front, so the targets that fall
        # out are the ones nothing has rendered on in the longest time.
        store[key] = merged
        for stale in list(store)[:-MAX_TARGETS]:
            del store[stale]
        await set_workflow_config(SLOT, store)
