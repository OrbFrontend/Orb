from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ....core import DecisionDefinition
from ....inference import DecisionQuestion
from .render import DECISION_RENDERER_VERSION

EVALUATIONS_VERSION = 2


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def raw_request_fingerprint(model: str, state: str, question: DecisionQuestion) -> str:
    return _digest([model, state, question.canonical()])


def resolution_policy_fingerprint(definition: DecisionDefinition, *, scope: str) -> str:
    return _digest(
        {
            "renderer": DECISION_RENDERER_VERSION,
            "placement": definition.placement,
            "scope": scope,
            "resolution": definition.resolution,
            "threshold": definition.threshold,
            "confidence_floor": definition.confidence_floor,
        }
    )


def envelope(evaluations: Sequence[Mapping[str, Any]], skipped: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return (
        {"version": EVALUATIONS_VERSION, "evaluations": list(evaluations), "skipped": list(skipped)}
        if evaluations or skipped
        else {}
    )


def readable(stored: Mapping[str, Any] | None) -> bool:
    version = stored.get("version") if stored else None
    return isinstance(version, int) and version <= EVALUATIONS_VERSION


def stored_evaluations(stored: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    records = stored.get("evaluations") if readable(stored) and stored else None
    return [dict(record) for record in records if isinstance(record, Mapping)] if isinstance(records, list) else []


def matching_replay(
    records: Sequence[Mapping[str, Any]], *, fragment_id: str, raw_fingerprint: str, policy_fingerprint: str
) -> dict[str, Any] | None:
    for record in records:
        if (
            record.get("fragment_id") == fragment_id
            and not record.get("anchor_invalidated")
            and record.get("raw_request_fingerprint") == raw_fingerprint
            and record.get("resolution_policy_fingerprint") == policy_fingerprint
        ):
            return dict(record)
    return None


def invalidated_anchor(records: Sequence[Mapping[str, Any]], fragment_id: str) -> bool:
    return any(record.get("fragment_id") == fragment_id and record.get("anchor_invalidated") for record in records)


def remap_anchors(stored: Mapping[str, Any] | None, id_map: Mapping[int, int]) -> dict[str, Any]:
    if not readable(stored) or stored is None:
        return {}
    evaluations = stored_evaluations(stored)
    for record in evaluations:
        if (anchor := record.get("input_branch_anchor")) is not None:
            record["input_branch_anchor"] = id_map.get(int(anchor))
            if record["input_branch_anchor"] is None:
                record["anchor_invalidated"] = True
    return {**stored, "evaluations": evaluations}
