"""Director, Writer, and Editor turn pipeline."""

from __future__ import annotations

from .context import resolve_card_and_persona, resolve_judge_config
from .context_size import estimate_context_size
from .entrypoints import (
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_speak,
    handle_super_regenerate,
    handle_turn,
)
from .passes.judge import remap_anchors as remap_decision_anchors
from .predicates import agent_enabled
from .prose_rewrite import prose_rewrite_source, rerun_after_prose_rewrite, retained_draft
from .state import LorebookTurn, ModelLane, PipelineConfig, TurnState

__all__ = [
    # entrypoints -- turn entry points
    "handle_fork_edit",
    "handle_magic_rewrite",
    "handle_regenerate",
    "handle_super_regenerate",
    "handle_turn",
    "handle_speak",
    # prose_rewrite -- the turn machinery behind an on-demand rewrite of a saved reply
    "rerun_after_prose_rewrite",
    "prose_rewrite_source",
    "retained_draft",
    # predicates -- turn predicates
    "agent_enabled",
    # context -- card and persona resolution shared with the api layer
    "resolve_card_and_persona",
    # context_size -- the context meter's estimate
    "estimate_context_size",
    # decisions -- the surfaces the api layer needs from the judge pass
    "remap_decision_anchors",
    "resolve_judge_config",
    # state -- per-turn contracts
    "LorebookTurn",
    "ModelLane",
    "TurnState",
    "PipelineConfig",
]
