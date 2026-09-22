from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ....core import OUTCOME_KEYS, DecisionDefinition
from .render import DECISION_RENDERER_VERSION

EVALUATIONS_VERSION = 2


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def raw_request_fingerprint(
    *,
    model: str,
    state: str,
    instructions: str,
    criteria: Mapping[str, str] | Sequence[str],
    question_type: str,
    facet_key: str = "",
    branch_key: str = "",
) -> str:
    return _digest(
        {
            "model": model,
            "state": state,
            "type": question_type,
            "instructions": instructions,
            "criteria": (
                [[key, criteria[key]] for key in OUTCOME_KEYS if key in criteria]
                if question_type == "noul" and isinstance(criteria, Mapping)
                else list(criteria.items())
                if isinstance(criteria, Mapping)
                else list(criteria)
            ),
            "facet_key": facet_key,
            "branch_key": branch_key,
        }
    )


def resolution_policy_fingerprint(definition: DecisionDefinition, *, scope: str) -> str:
    return _digest(
        {
            "renderer": DECISION_RENDERER_VERSION,
            "placement": definition.placement,
            "scope": scope,
            "resolution": definition.resolution,
            "threshold": definition.threshold,
            "default": definition.default_outcome,
            "confidence_floor": definition.confidence_floor,
            "facets": [
                {
                    "key": facet.key,
                    "type": facet.decision_type,
                    "resolution": facet.resolution,
                    "confidence_floor": facet.confidence_floor,
                }
                for facet in definition.facets
            ],
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
    out = {
        "version": stored.get("version", EVALUATIONS_VERSION),
        "evaluations": [],
        "skipped": [dict(row) for row in stored.get("skipped", []) if isinstance(row, Mapping)],
    }
    for record in stored_evaluations(stored):
        if (anchor := record.get("input_branch_anchor")) is not None:
            mapped = id_map.get(int(anchor))
            record["input_branch_anchor"] = mapped
            if mapped is None:
                record["anchor_invalidated"] = True
        out["evaluations"].append(record)
    return out
