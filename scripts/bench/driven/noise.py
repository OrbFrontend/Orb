"""Bench 2 judge noise: re-ask Jev once, live, on a sample of scored replies and count label and threshold flips.

    PYTHONPATH=. .venv/bin/python -m scripts.bench.driven.noise --scores OUT/turns.json --run RUN [--scores ... --run ...] \
        --output OUT

The re-asks append to OUT/jev-reask-raw.jsonl and are never read back as answers, so a rerun asks again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scripts.bench.cache.orb_driver import save
from scripts.bench.driven.hand_labels import sample, scored_rows
from scripts.bench.driven.jev_check import judge_client, state_for
from scripts.bench.driven.score import DRIVEN_THRESHOLD, LABELS, Judge, answer_of, mean, preflight


async def reask(rows: list[dict], output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    judge = Judge(output / "jev-reask-raw.jsonl", judge_client())
    save(output / "jev-reask-preflight.json", await preflight(judge, output / "jev-reask-preflight-raw.jsonl"))

    async def again(row: dict) -> dict:
        run = Path(row["run"])
        request = json.loads((run / "plan.json").read_text())["contexts"][row["context"]]["user_turn"]
        raw = await judge.post(state_for(request, (run / "turns" / row["key"] / "final.md").read_text()))
        with judge.cache.open("a") as stream:
            stream.write(json.dumps({"model": row["model"], "turn": row["key"], **raw}, ensure_ascii=False) + "\n")
        answer, _ = answer_of(raw)
        return {
            "model": row["model"],
            "key": row["key"],
            "label": row["label"],
            "label_again": answer.selected,
            "p_driven": row["p_driven"],
            "p_driven_again": answer.probabilities.get("driven", 0.0),
            **{f"p_{label}_again": answer.probabilities.get(label) for label in LABELS},
        }

    pairs = await asyncio.gather(*(again(row) for row in rows))
    moves = [abs(pair["p_driven_again"] - pair["p_driven"]) for pair in pairs]
    result = {
        "n": len(pairs),
        "driven_threshold": DRIVEN_THRESHOLD,
        "label_flips": sum(pair["label"] != pair["label_again"] for pair in pairs),
        "threshold_flips": sum(
            (pair["p_driven"] >= DRIVEN_THRESHOLD) != (pair["p_driven_again"] >= DRIVEN_THRESHOLD) for pair in pairs
        ),
        "mean_abs_p_driven_move": mean(moves),
        "max_abs_p_driven_move": max(moves, default=None),
        "pairs": pairs,
    }
    save(output / "noise.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", type=Path, action="append", default=[], help="score.py's turns.json")
    parser.add_argument("--run", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, default=20)
    args = parser.parse_args()
    rows = sample(scored_rows(args.scores, args.run), args.size, seed=20261010)
    result = asyncio.run(reask(rows, args.output))
    print(json.dumps({key: value for key, value in result.items() if key != "pairs"}, indent=2))


if __name__ == "__main__":
    main()
