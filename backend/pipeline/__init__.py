"""Pipeline layer — the Director→Writer→Editor turn engine.

Each turn is handled by a chain of single-purpose modules:

* ``predicates`` — dependency-free helpers (the package's leaf, like ``core/``)
* ``state`` — per-turn dataclasses shared across all passes
* ``config`` — resolves flags, lanes, and the tool blob for a turn
* ``context`` — loads conversation data and builds LLM prefixes
* ``workflow_bridge`` — runs secondary-workflow hooks before and after the passes
* ``orchestrator`` — sequences the three passes and collects the result
* ``persistence`` — saves the assistant message and all turn side-effects
* ``entrypoints`` — the five public ``handle_*`` functions called by routes
* ``passes/`` — the director, writer, and editor passes
"""

from __future__ import annotations

from .context import conversation_macro_seed, persona_macros, resolve_card_and_persona
from .entrypoints import (
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_super_regenerate,
    handle_turn,
)
from .predicates import agent_enabled, resolve_persona_id
from .state import LorebookTurn, ModelLane, TurnState, _PipelineConfig

__all__ = [
    # entrypoints — turn entry points
    "handle_fork_edit",
    "handle_magic_rewrite",
    "handle_regenerate",
    "handle_super_regenerate",
    "handle_turn",
    # predicates — turn predicates
    "agent_enabled",
    "resolve_persona_id",
    # context — persona/macros resolution shared with the api layer
    "conversation_macro_seed",
    "persona_macros",
    "resolve_card_and_persona",
    # state — per-turn contracts
    "LorebookTurn",
    "ModelLane",
    "TurnState",
    "_PipelineConfig",
]
