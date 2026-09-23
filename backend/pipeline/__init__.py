"""Director, Writer, and Editor turn pipeline."""

from __future__ import annotations

from .context import (
    conversation_macro_seed,
    persona_macros,
    resolve_card_and_persona,
    resolve_decision_config,
)
from .entrypoints import (
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_speak,
    handle_super_regenerate,
    handle_turn,
)
from .passes.decisions import remap_anchors as remap_decision_anchors
from .predicates import agent_enabled, resolve_persona_id
from .state import LorebookTurn, ModelLane, TurnState, _PipelineConfig

__all__ = [
    # entrypoints — turn entry points
    "handle_fork_edit",
    "handle_magic_rewrite",
    "handle_regenerate",
    "handle_super_regenerate",
    "handle_turn",
    "handle_speak",
    # predicates — turn predicates
    "agent_enabled",
    "resolve_persona_id",
    # context — persona/macros resolution shared with the api layer
    "conversation_macro_seed",
    "persona_macros",
    "resolve_card_and_persona",
    # decisions — the surfaces the api layer needs from the decision stage
    "remap_decision_anchors",
    "resolve_decision_config",
    # state — per-turn contracts
    "LorebookTurn",
    "ModelLane",
    "TurnState",
    "_PipelineConfig",
]
