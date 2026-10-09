"""Blind hand labels for Bench 2: sample scored replies, label them without arm or Jev label, then compare with Jev.

    python -m scripts.bench.driven.hand_labels sample --scores OUT/turns.json --run RUN --sheet sheet.md --key key.json
    python -m scripts.bench.driven.hand_labels score --sheet sheet.md --key key.json

The sheet carries each reply under an opaque id with a `label:` line to fill in with driven, afterthought or static. The key
holds the arm, Jev's label and P(driven) per id; keep it closed until the sheet is done.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

from scripts.bench.cache.orb_driver import save
from scripts.bench.driven.jev_check import SHAPE
from scripts.bench.driven.score import DRIVEN_THRESHOLD

LABELS = list(SHAPE.criteria)
BANDS = ((0.0, 0.4), (0.4, 0.8), (0.8, 1.01))


def stratum(row: dict) -> tuple[str, int]:
    band = next(index for index, (low, high) in enumerate(BANDS) if low <= row["p_driven"] < high)
    return row["label"], band


def sample(rows: list[dict], size: int, seed: int = 20261009) -> list[dict]:
    """Round-robin over (Jev label, P(driven) band) strata, so rare labels and borderline scores are both represented."""
    rng = random.Random(seed)
    strata: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        strata.setdefault(stratum(row), []).append(row)
    for members in strata.values():
        rng.shuffle(members)
    chosen: list[dict] = []
    while len(chosen) < size and any(strata.values()):
        for key in sorted(strata):
            if strata[key] and len(chosen) < size:
                chosen.append(strata[key].pop())
    rng.shuffle(chosen)
    return chosen


def write_sheet(chosen: list[dict], run: Path, plan: dict, sheet: Path, key: Path):
    criteria = "\n".join(f"- **{label}**: {text}" for label, text in SHAPE.criteria.items())
    parts = [f"# Bench 2 hand labels\n\n{SHAPE.instructions}\n\n{criteria}\n"]
    answers = {}
    for index, row in enumerate(chosen, 1):
        item = f"h{index:02d}"
        reply = (run / "turns" / row["key"] / "final.md").read_text()
        request = plan["contexts"][row["context"]]["user_turn"]
        parts.append(f"## {item}\n\nlabel: \n\n**Request:** {request}\n\n{reply}\n")
        answers[item] = {field: row[field] for field in ("key", "context", "arm", "label", "p_driven")}
    sheet.write_text("\n---\n\n".join(parts))
    save(key, answers)


def read_sheet(sheet: Path) -> dict[str, str]:
    found = dict(re.findall(r"^## (h\d+)\s*\n+label:[ \t]*(\w*)", sheet.read_text(), flags=re.MULTILINE))
    unknown = {item: label for item, label in found.items() if label not in LABELS}
    if unknown:
        raise SystemExit(f"fill every label with one of {LABELS}: {unknown}")
    return found


def compare(hand: dict[str, str], answers: dict) -> dict:
    matrix = {human: {jev: 0 for jev in LABELS} for human in LABELS}
    for item, label in hand.items():
        matrix[label][answers[item]["label"]] += 1
    agree = sum(matrix[label][label] for label in LABELS)
    by_label = [(hand[item] == "driven", answers[item]["label"] == "driven") for item in hand]
    by_threshold = [(hand[item] == "driven", answers[item]["p_driven"] >= DRIVEN_THRESHOLD) for item in hand]
    return {
        "n": len(hand),
        "agreement": agree / len(hand),
        "rows_human_columns_jev": matrix,
        "driven_threshold": DRIVEN_THRESHOLD,
        "threshold": precision_recall(by_threshold),
        "jev_label": precision_recall(by_label),
    }


def precision_recall(pairs: list[tuple[bool, bool]]) -> dict:
    """Of (human driven, Jev driven) pairs: how many Jev calls are right, and how many human ones Jev finds."""
    hits = sum(human and jev for human, jev in pairs)
    return {
        "driven_precision": hits / max(1, sum(jev for _, jev in pairs)),
        "driven_recall": hits / max(1, sum(human for human, _ in pairs)),
        "agreement": sum(human == jev for human, jev in pairs) / len(pairs),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["sample", "score"])
    parser.add_argument("--scores", type=Path, help="score.py's turns.json")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--sheet", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--size", type=int, default=40)
    args = parser.parse_args()
    if args.action == "sample":
        rows = [row for row in json.loads(args.scores.read_text()) if row.get("label")]
        plan = json.loads((args.run / "plan.json").read_text())
        write_sheet(sample(rows, args.size), args.run, plan, args.sheet, args.key)
    else:
        print(json.dumps(compare(read_sheet(args.sheet), json.loads(args.key.read_text())), indent=2))


if __name__ == "__main__":
    main()
