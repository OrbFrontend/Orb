"""Evaluation records, their two fingerprints, and replay matching.

The two fingerprints answer two different questions, and keeping them apart is
what lets an author edit guidance for free:

* the **raw-request fingerprint** covers classifier input -- the model, the
  rendered state, and the rendered question with its criteria. If it matches, the
  same request would be sent, so the stored answer is still that request's
  answer.
* the **resolution-policy fingerprint** covers how an answer becomes an outcome
  -- placement, scope, renderer contract version, threshold or roll mode, the
  threshold value, and the fallback outcome. If it matches, the stored outcome is
  still what that answer resolves to.

Labels and authored output text are in neither. They are not classifier inputs
and they do not change a resolution, so editing them changes the prompt on the
next regeneration without another call and without a reroll.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ....core import OUTCOME_KEYS, DecisionDefinition
from .render import DECISION_RENDERER_VERSION

#: The stored envelope's version. Read before anything else, so a record written
#: by a later Orb is left alone rather than half-understood.
EVALUATIONS_VERSION = 1


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def raw_request_fingerprint(
    *, model: str, state: str, instructions: str, criteria: Mapping[str, str], question_type: str
) -> str:
    """Identify the classifier request this evaluation would send.

    Exactly the bytes that would go out, in a canonical order -- and no
    whitespace or case normalization, because the classifier reads the prose and
    two spellings of a question are two questions.
    """
    return _digest(
        {
            "model": model,
            "state": state,
            "type": question_type,
            "instructions": instructions,
            "criteria": [[key, criteria[key]] for key in OUTCOME_KEYS if key in criteria],
        }
    )


def resolution_policy_fingerprint(definition: DecisionDefinition, *, scope: str) -> str:
    """Identify how an answer becomes an outcome for this definition."""
    return _digest(
        {
            "renderer": DECISION_RENDERER_VERSION,
            "placement": definition.placement,
            "scope": scope,
            "resolution": definition.resolution,
            "threshold": definition.threshold,
            "default": definition.default_outcome,
        }
    )


def envelope(evaluations: Sequence[Mapping[str, Any]], skipped: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The versioned record stored on a reply.

    ``{}`` when the exchange evaluated and skipped nothing, so a conversation
    without decisions stores no envelope at all rather than a version stamp on
    every message.
    """
    if not evaluations and not skipped:
        return {}
    return {"version": EVALUATIONS_VERSION, "evaluations": list(evaluations), "skipped": list(skipped)}


def readable(stored: Mapping[str, Any] | None) -> bool:
    """Whether this Orb understands *stored*'s version.

    A record from a newer Orb is not replayed: an unknown version might resolve
    differently, and reusing an outcome we cannot fully interpret is worse than
    creating a fresh occurrence.
    """
    if not stored:
        return False
    version = stored.get("version")
    return isinstance(version, int) and version <= EVALUATIONS_VERSION


def stored_evaluations(stored: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The evaluation list inside a readable envelope, else empty."""
    if not readable(stored):
        return []
    records = (stored or {}).get("evaluations")
    return [dict(record) for record in records if isinstance(record, Mapping)] if isinstance(records, list) else []


def matching_replay(
    records: Sequence[Mapping[str, Any]], *, fragment_id: str, raw_fingerprint: str, policy_fingerprint: str
) -> dict[str, Any] | None:
    """The target reply's own record for this decision, if it still applies.

    All three have to match: the same fragment, the same request, and the same
    resolution policy. A fallback record matches like any other -- regeneration
    alone does not retry a failed classification, because a user pressing
    regenerate asked for a different reply, not for a second attempt at the
    provider that just failed.
    """
    for record in records:
        if record.get("fragment_id") != fragment_id or record.get("anchor_invalidated"):
            # An anchor that could not be remapped through a branch copy is not a
            # match to weigh: the record describes input read from a message this
            # branch does not have. Re-asking is right; claiming the old answer
            # still describes this branch is not.
            continue
        if (
            record.get("raw_request_fingerprint") == raw_fingerprint
            and record.get("resolution_policy_fingerprint") == policy_fingerprint
        ):
            return dict(record)
    return None


def invalidated_anchor(records: Sequence[Mapping[str, Any]], fragment_id: str) -> bool:
    """Whether this fragment had a record whose branch anchor was lost.

    The stage stamps the reason onto the fresh occurrence it creates instead, so
    the Inspector can say "this was re-asked because the copy lost its anchor"
    rather than leaving an unexplained new occurrence on an unchanged reply.
    """
    return any(record.get("fragment_id") == fragment_id and record.get("anchor_invalidated") for record in records)


def remap_anchors(stored: Mapping[str, Any] | None, id_map: Mapping[int, int]) -> dict[str, Any]:
    """Re-point a copied reply's evaluation anchors at the copied messages.

    Checkpoints and branch copies re-insert a path under new ids, so an anchor
    that still named the source message would make a later regeneration
    reconstruct state from another conversation's history. An anchor with no
    entry in *id_map* becomes an explicit invalidation -- the next regeneration
    creates a new occurrence and says why -- rather than resolving to unrelated
    history.
    """
    if not readable(stored) or stored is None:
        return {}
    out: dict[str, Any] = {
        "version": stored.get("version", EVALUATIONS_VERSION),
        "evaluations": [],
        "skipped": [dict(row) for row in stored.get("skipped", []) if isinstance(row, Mapping)],
    }
    for record in stored_evaluations(stored):
        anchor = record.get("input_branch_anchor")
        if anchor is not None:
            mapped = id_map.get(int(anchor))
            record["input_branch_anchor"] = mapped
            if mapped is None:
                record["anchor_invalidated"] = True
        out["evaluations"].append(record)
    return out


__all__ = [
    "EVALUATIONS_VERSION",
    "envelope",
    "invalidated_anchor",
    "matching_replay",
    "raw_request_fingerprint",
    "readable",
    "remap_anchors",
    "resolution_policy_fingerprint",
    "stored_evaluations",
]
