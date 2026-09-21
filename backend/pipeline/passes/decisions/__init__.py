"""The decision stage: ask one question about the scene, then guide with the answer.

A **decision** is an interactive fragment that asks one question, resolves the
answer against its own policy, and supplies its own authored guidance. The first
release runs yes/no ``noul`` questions before the Director, by threshold or by a
weighted roll; the Director plans around the resolved outcome and the Writer
receives the same guidance.

What lives where:

* :mod:`.render` -- the frozen per-stage snapshot and the small, explicit macro
  set the classifier input may use;
* :mod:`.resolve` -- probability to outcome, and the vocabulary for why there
  wasn't one;
* :mod:`.cooldown` -- decision cooldown state, deliberately separate from the
  Director's;
* :mod:`.records` -- the persisted evaluation shape, its two fingerprints, and
  replay matching;
* :mod:`.guidance` -- the one model-facing block, carrying authored words and
  nothing about dice;
* :mod:`.stage` -- eligibility, batching, budgets, and the published result.

The provider call itself is ``inference/jev.py``. This package never speaks HTTP
and never writes to the database: the pipeline's persistence layer commits the
records atomically with the reply that they produced.
"""

from __future__ import annotations

from .authoring import SAMPLE_SNAPSHOT, connection_test, definition_problems, preview
from .cooldown import advance as advance_decision_cooldowns
from .cooldown import blocked as resting_decisions
from .cooldown import branch_baseline as decision_cooldown_baseline
from .guidance import decision_guidance_block
from .records import (
    EVALUATIONS_VERSION,
    envelope,
    raw_request_fingerprint,
    remap_anchors,
    resolution_policy_fingerprint,
    stored_evaluations,
)
from .render import (
    DECISION_RENDERER_VERSION,
    STATE_MACROS,
    TEXT_MACROS,
    DecisionSnapshot,
    build_snapshot,
    macro_errors,
    macros_used,
    render,
    template_errors,
)
from .resolve import FallbackReason, SkipReason, resolve_roll, resolve_threshold
from .stage import (
    MAX_DECISIONS_PER_CARD,
    MAX_DECISIONS_PER_EXCHANGE,
    REQUEST_TIMEOUT_SECONDS,
    STAGE_BUDGET_SECONDS,
    DecisionCandidate,
    DecisionConfig,
    DecisionsResult,
    DecisionsTurn,
    run_decisions,
    stage_has_work,
)

__all__ = [
    "DECISION_RENDERER_VERSION",
    "EVALUATIONS_VERSION",
    "MAX_DECISIONS_PER_CARD",
    "MAX_DECISIONS_PER_EXCHANGE",
    "REQUEST_TIMEOUT_SECONDS",
    "SAMPLE_SNAPSHOT",
    "STAGE_BUDGET_SECONDS",
    "STATE_MACROS",
    "TEXT_MACROS",
    "DecisionCandidate",
    "DecisionConfig",
    "DecisionSnapshot",
    "DecisionsResult",
    "DecisionsTurn",
    "FallbackReason",
    "SkipReason",
    "advance_decision_cooldowns",
    "build_snapshot",
    "connection_test",
    "decision_cooldown_baseline",
    "decision_guidance_block",
    "definition_problems",
    "envelope",
    "macro_errors",
    "macros_used",
    "preview",
    "raw_request_fingerprint",
    "remap_anchors",
    "render",
    "resolution_policy_fingerprint",
    "resolve_roll",
    "resolve_threshold",
    "resting_decisions",
    "run_decisions",
    "stage_has_work",
    "stored_evaluations",
    "template_errors",
]
