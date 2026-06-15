"""Pipeline layer — the Director→Writer→Editor turn engine.

Sits above ``workflows`` in the one-way order
(``api → {pipeline, features} → workflows → {inference, analysis} → {core, database}``).
``orchestrator`` drives a turn; ``state`` holds the per-turn contracts; ``passes/``
holds the individual passes.

The facade re-exports the public turn entry points and the per-turn contract
types. Private symbols (``_run_pipeline``, ``_consume_pipeline``,
``_iterate_pre_pipeline_hooks``, …) are reached via ``pipeline.orchestrator``
directly.
"""

from __future__ import annotations

from .orchestrator import (
    agent_enabled,
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_super_regenerate,
    handle_turn,
    resolve_persona_id,
)
from .state import ModelLane, TurnState, _PipelineConfig

__all__ = [
    # orchestrator — turn entry points
    "agent_enabled",
    "handle_fork_edit",
    "handle_magic_rewrite",
    "handle_regenerate",
    "handle_super_regenerate",
    "handle_turn",
    "resolve_persona_id",
    # state — per-turn contracts
    "ModelLane",
    "TurnState",
    "_PipelineConfig",
]
