"""Score Bench 2 turns with Jev's `shape` question; keep every raw answer so the results rebuild without calling Jev.

PYTHONPATH=. .venv/bin/python -m scripts.bench.driven.score --run RUN --output OUT
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import random
import re
import time
from collections.abc import Sequence
from pathlib import Path

import httpx

from backend.inference.jev import ChoiceAnswer, DecisionClient, normalize_response
from scripts.bench.cache.orb_driver import save
from scripts.bench.driven.jev_check import FIXTURES, REQUEST, SHAPE, judge_client, state_for

JEV_MODEL = "typesafe/jev-1.13-20260917"
SNAPSHOT = json.loads(Path(__file__).with_name("bench.json").read_text())
LABELS = list(SHAPE.criteria)
# Pre-registered 2026-10-09 on the pilot's 10 hand labels, before the full run; the full run's hand labels test it.
DRIVEN_THRESHOLD = 0.75


def raw_key(state: str) -> str:
    return hashlib.sha256(f"{state}\x1f{SHAPE.canonical()}".encode()).hexdigest()


class Judge:
    """Jev's raw responses keyed by a hash of state and question; a cached reply is never sent to Jev again."""

    def __init__(self, cache: Path, client: DecisionClient | None, concurrency: int = 8):
        self.cache, self.client = cache, client
        self.limit = asyncio.Semaphore(concurrency)
        self.raw = {}
        if cache.exists():
            for line in cache.read_text().splitlines():
                row = json.loads(line)
                self.raw[row["key"]] = row

    async def post(self, state: str) -> dict:
        if self.client is None:
            raise ValueError("an answer is missing from the raw cache and Jev was not configured")
        body = {"model": self.client.model, "state": state, "questions": {SHAPE.key: SHAPE.payload()}}
        async with self.limit, httpx.AsyncClient(timeout=self.client.timeout) as http:
            response = await http.post(self.client.url, json=body, headers={"Authorization": f"Bearer {self.client.api_key}"})
        response.raise_for_status()
        return {
            "key": raw_key(state),
            "utc_ns": time.time_ns(),
            "state": state,
            "question": SHAPE.payload(),
            "response": response.json(),
        }

    async def ask(self, state: str) -> tuple[ChoiceAnswer, str]:
        key = raw_key(state)
        if key not in self.raw:
            row = await self.post(state)
            with self.cache.open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.raw[key] = row
        return answer_of(self.raw[key])


def answer_of(row: dict) -> tuple[ChoiceAnswer, str]:
    decision = normalize_response(row["response"], [SHAPE])
    answer = decision.answers.get(SHAPE.key)
    if not isinstance(answer, ChoiceAnswer):
        raise ValueError(f"Jev returned no choice for {row['key']}")
    if decision.returned_model != JEV_MODEL:
        raise SystemExit(f"Jev answered as {decision.returned_model!r}, not {JEV_MODEL}; stop and re-validate")
    return answer, decision.returned_model


async def preflight(judge: Judge, log: Path) -> list[dict]:
    """Always live: a cached fixture answer could not show that the judge moved."""
    rows = []
    for name, (expected, paragraphs) in FIXTURES.items():
        raw = await judge.post(state_for(REQUEST, "\n\n".join(paragraphs)))
        with log.open("a") as stream:
            stream.write(json.dumps({"fixture": name, **raw}, ensure_ascii=False) + "\n")
        answer, model = answer_of(raw)
        rows.append(
            {"fixture": name, "expected": expected, "selected": answer.selected, **answer.probabilities, "model": model}
        )
    wrong = [row["fixture"] for row in rows if row["selected"] != row["expected"]]
    if wrong:
        raise SystemExit(f"the judge moved: {wrong} changed label; stop and re-validate")
    return rows


def binding_key(binding: dict) -> str | None:
    key = binding.get("key")
    return None if key in (None, "setup") else key


def wire(requests: Path, transport: dict) -> dict[str, dict]:
    """Per turn: every recorded model call, checked against the transport's bench.json config."""
    config = transport["model_config"]
    samplers = {key: config[key] for key in ("temperature", "top_k", "top_p", "min_p", "repetition_penalty", "max_tokens")}
    extra = json.loads(config["extra_body"] or "{}")
    turns: dict[str, dict] = {}
    for call in sorted(requests.iterdir()) if requests.exists() else []:
        metadata = json.loads((call / "metadata.json").read_text())
        key = binding_key(metadata.get("binding", {}))
        if key is None or not metadata.get("target", "").endswith("/chat/completions"):
            continue
        body = json.loads((call / "request.bin").read_bytes() or b"{}")
        errors = [f"wire.{name}" for name, value in {**samplers, **extra}.items() if body.get(name) != value]
        errors += ["wire.seed"] if "seed" in body else []
        thinking_off = body.get("chat_template_kwargs", {}).get("enable_thinking") is False
        if not thinking_off or (body.get("reasoning") or {}).get("enabled") is not False:
            errors.append("wire.thinking")
        if "model_name" in config and body.get("model") != config["model_name"]:
            errors.append("wire.model")
        if metadata.get("status") != 200 or not metadata.get("downstream_complete") or metadata.get("error"):
            errors.append("response.incomplete")
        reasoning, providers, cost = 0, set(), 0.0
        for line in (call / "response.bin").read_text(errors="replace").splitlines():
            if not line.startswith("data: {"):
                continue
            try:
                frame = json.loads(line[6:])
            except ValueError:
                errors.append("response.malformed_sse")
                continue
            if frame.get("provider"):
                providers.add(frame["provider"])
            cost += float((frame.get("usage") or {}).get("cost") or 0)
            for choice in frame.get("choices", []):
                delta = choice.get("delta", {})
                reasoning += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
        if "provider" in transport and providers != {transport["provider"]}:
            errors.append(f"wire.provider:{sorted(providers)}")
        row = turns.setdefault(key, {"calls": 0, "wire_errors": [], "reasoning_chars": 0, "cost": 0.0})
        row["calls"] += 1
        row["wire_errors"] += errors
        row["reasoning_chars"] += reasoning
        row["cost"] += cost
    return turns


def words(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def share(rows: list[dict], label: str) -> float | None:
    return sum(row["label"] == label for row in rows) / len(rows) if rows else None


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bootstrap(groups: list[list[float]], resamples: int = 10000) -> list[float]:
    """95% interval of the mean, resampling whole groups."""
    rng = random.Random(20261009)
    draws = []
    for _ in range(resamples):
        values = [value for group in rng.choices(groups, k=len(groups)) for value in group]
        draws.append(sum(values) / len(values))
    draws.sort()
    return [draws[int(0.025 * resamples)], draws[int(0.975 * resamples) - 1]]


def paired_driven(rows: list[dict], contexts: dict, field: str = "driven") -> dict:
    """Director on minus off in `driven` share per context; intervals resample contexts, and cards as a sensitivity."""
    by_context: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        by_context.setdefault(row["context"], {"on": [], "off": []})[row["arm"]].append(float(row[field]))
    paired = {
        context: sum(arms["on"]) / len(arms["on"]) - sum(arms["off"]) / len(arms["off"])
        for context, arms in by_context.items()
        if arms["on"] and arms["off"]
    }
    if not paired:
        return {"contexts": 0}
    by_card: dict[str, list[float]] = {}
    for context, difference in paired.items():
        by_card.setdefault(contexts[context]["card_id"], []).append(difference)
    return {
        "contexts": len(paired),
        "cards": len(by_card),
        "difference": mean(list(paired.values())),
        "ci95_contexts": bootstrap([[value] for value in paired.values()]),
        "ci95_cards": bootstrap(list(by_card.values())),
    }


def length_bands(rows: list[dict]) -> dict:
    """Pooled tertiles of reply words, and each arm's `driven` share within them."""
    ordered = sorted(row["words"] for row in rows)
    if len(ordered) < 3:
        return {}
    edges = [ordered[len(ordered) // 3], ordered[2 * len(ordered) // 3]]
    bands = {}
    for name, low, high in (("short", 0, edges[0]), ("middle", edges[0], edges[1]), ("long", edges[1], 10**9)):
        band = [row for row in rows if low <= row["words"] < high]
        bands[name] = {
            "words": [low, high],
            **{
                arm: {"n": len(sub), "driven": mean([float(r["driven"]) for r in sub])}
                for arm in ("on", "off")
                for sub in [[r for r in band if r["arm"] == arm]]
            },
        }
    return bands


async def score(run: Path, output: Path, *, judge_on: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    plan = json.loads((run / "plan.json").read_text())
    manifest = json.loads((run / "manifest.json").read_text())
    judge = Judge(output / "jev-raw.jsonl", judge_client()) if judge_on else None
    if judge is not None:
        save(output / "jev-preflight.json", await preflight(judge, output / "jev-preflight-raw.jsonl"))
    transport = SNAPSHOT["transports"][manifest.get("transport", "gemma")]
    calls = wire(run / "requests", transport)
    rows, pending = [], []
    for key, turn in plan["turns"].items():
        folder = run / "turns" / key
        summary = json.loads((folder / "summary.json").read_text())
        final = (folder / "final.md").read_text() if (folder / "final.md").exists() else ""
        draft = (folder / "draft.md").read_text() if (folder / "draft.md").exists() else ""
        facts = calls.get(key, {"calls": 0, "wire_errors": [], "reasoning_chars": 0, "cost": 0.0})
        row = {
            "key": key,
            "context": turn["context"],
            "card": plan["contexts"][turn["context"]]["card_id"],
            "arm": turn["arm"],
            "complete": summary["complete"],
            "retried_attempts": len(list((run / "turns").glob(f"{key}.attempt-*"))),
            "warnings": [event["data"] for event in summary.get("warnings", [])],
            "error": summary.get("error"),
            "words": words(final),
            "draft_words": words(draft),
            "seconds": (summary["finished_ns"] - summary["started_ns"]) / 1e9,
            **facts,
            "rejected": None,
            "label": None,
        }
        if not summary["complete"]:
            # A partial reply saved by fallback persistence is not a reply to label; Bench 4 counts the attempt.
            row["rejected"] = "turn not clean"
        elif not facts["calls"] or any(error.startswith(("wire.", "response.")) for error in facts["wire_errors"]):
            row["rejected"] = f"request check failed: {sorted(set(facts['wire_errors'])) or 'no recorded calls'}"
        elif judge is not None:
            pending.append((row, state_for(plan["contexts"][turn["context"]]["user_turn"], final)))
        rows.append(row)
    if judge is not None:
        answers = await asyncio.gather(*(judge.ask(state) for _, state in pending))
        for (row, _), (answer, _) in zip(pending, answers):
            row["label"] = answer.selected
            row.update({f"p_{label}": answer.probabilities.get(label) for label in LABELS})
            row["driven"] = row["p_driven"] >= DRIVEN_THRESHOLD
            row["label_driven"] = row["label"] == "driven"
    scored = [row for row in rows if row["label"]]
    arms = {}
    for arm in ("on", "off"):
        attempted = [row for row in rows if row["arm"] == arm]
        sub = [row for row in scored if row["arm"] == arm]
        arms[arm] = {
            "attempted": len(attempted),
            "clean": sum(row["complete"] for row in attempted),
            "retried": sum(row["retried_attempts"] > 0 for row in attempted),
            "scored": len(sub),
            "driven": mean([float(row["driven"]) for row in sub]),
            "labels": {label: share(sub, label) for label in LABELS},
            "mean_p_driven": mean([row["p_driven"] for row in sub]),
            "mean_words": mean([row["words"] for row in sub]),
            "mean_calls": mean([row["calls"] for row in attempted]),
            "reasoning_chars": sum(row["reasoning_chars"] for row in attempted),
            "cost": sum(row["cost"] for row in attempted),
        }
    summary = {
        "run": str(run),
        "transport": manifest.get("transport", "gemma"),
        "jev_model": JEV_MODEL if judge is not None else None,
        "driven_threshold": DRIVEN_THRESHOLD,
        "arms": arms,
        "paired_driven": paired_driven(scored, plan["contexts"]),
        "paired_label_driven": paired_driven(scored, plan["contexts"], "label_driven"),
        "length_bands": length_bands(scored),
    }
    save(output / "turns.json", rows)
    with (output / "turns.csv").open("w", newline="") as stream:
        fields = list(dict.fromkeys(key for row in rows for key in row if key != "warnings"))
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    save(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-judge", action="store_true", help="check the turns and calls only; no Jev calls")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(score(args.run, args.output, judge_on=not args.skip_judge)), indent=2))


if __name__ == "__main__":
    main()
