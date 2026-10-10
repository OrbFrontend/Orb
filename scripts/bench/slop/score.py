"""Score Bench 3 on Bench 2's saved turns: Orb's own detectors and a held-out phrase list on the Writer's draft and the reply.

PYTHONPATH=. .venv/bin/python -m scripts.bench.slop.score --run RUN [--run RUN ...] --heldout PHRASES --output OUT
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import random
import re
import sqlite3
from collections import Counter
from pathlib import Path

from backend.analysis import build_targets, filter_audit_report_to_text, report_to_dict, run_audit, split_narration_sentences
from backend.analysis.detectors.slop_detector import detect_cliches
from backend.analysis.detectors.subject_fixation import HISTORY_WINDOW, confirm, nominate, presence_streaks
from backend.pipeline.passes.editor.editor import AUDIT_BASELINE_WINDOW
from backend.pipeline.passes.editor.subject_repeats import repeat_scores
from backend.pipeline.subject_tags import tag_text
from scripts.bench.cache.orb_driver import save

HELDOUT_URL = (
    "https://raw.githubusercontent.com/sam-paech/antislop-sampler/"
    "6aa2540392818600372f8974ea232c10f64fa745/slop_phrases_2025-04-07.json"
)
HELDOUT_SHA256 = "d0bc80be1c294e03ccb59464553c5d72b1c13dd7bf6d8d99e9da700c3e6fbff1"
SEVEN = (
    "banned_phrases",
    "repetitive_openers",
    "repetitive_templates",
    "contrastive_negation",
    "phrase_repetition",
    "structural_repetition",
    "anti_echo",
)
APART = ("negated_narration", "subject_fixation")
# What identifies one finding across the draft and the reply, per report section.
FINDING_KEY = {
    "banned_phrases": "phrase",
    "repetitive_openers": "opener",
    "repetitive_templates": "template",
    "contrastive_negation": "sentence",
    "phrase_repetition": "phrase",
    "structural_repetition": None,
    "anti_echo": "matched",
    "negated_narration": "span",
}
# A flagged sentence missing from the reply was rewritten when some reply sentence is at least this close to it.
REWRITE_RATIO = 0.5


def phrase_bank(run: Path) -> list:
    """The phrase bank the run's Orb read, from its own database."""
    with sqlite3.connect(f"file:{run / 'orb/backend/data/app.db'}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT variants, kind, pattern FROM phrase_bank ORDER BY id ASC").fetchall()
    return [
        {"kind": "regex", "pattern": pattern or ""}
        if kind == "regex"
        else {"kind": "literal", "variants": json.loads(variants)}
        for variants, kind, pattern in rows
    ]


def heldout_phrases(path: Path, bank: list) -> tuple[list[str], list[str]]:
    """The held-out list, one entry per normalized spelling, minus every entry the seeded bank's own matcher flags."""
    if hashlib.sha256(path.read_bytes()).hexdigest() != HELDOUT_SHA256:
        raise ValueError(f"{path} is not the pinned held-out list ({HELDOUT_URL})")
    phrases = list(dict.fromkeys(normalize(phrase) for phrase, _count in json.loads(path.read_text())))
    dropped = [phrase for phrase in phrases if detect_cliches(phrase, bank).flagged_count]
    return [phrase for phrase in phrases if phrase not in dropped], dropped


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'").replace("‘", "'"))


def heldout_hits(text: str, patterns: list[tuple[str, re.Pattern]]) -> Counter:
    flat = normalize(text)
    return Counter({phrase: n for phrase, pattern in patterns if (n := len(pattern.findall(flat)))})


def words(text: str) -> int:
    return len(text.split())


def audit(text: str, history: list[str], user: str, bank: list, toggles: dict):
    """The Editor's own audit of *text*: cross-message context, filtered to the text, and its numbered targets."""
    full = "\n\n".join([*reversed(history), text]) if history else text
    report = filter_audit_report_to_text(
        run_audit(full, bank, assistant_messages=history, structural_text=text, user_message=user, audit_toggles=toggles), text
    )
    return report, build_targets(report, text)


def finding_keys(report, text: str) -> dict[str, Counter]:
    sections = report_to_dict(report)["sections"]
    keys = {name: Counter() for name in FINDING_KEY}
    for name, items in sections.items():
        field = FINDING_KEY[name]
        keys[name].update(item[field] if field else "structure" for item in items)
    return keys


async def subject_streaks(text: str, history: list[str], history_tags: list[dict]) -> Counter:
    """The subject fixation step's flags on *text*: presence streaks, then nominees the pair comparer confirms."""
    tags = await tag_text(text)
    streaks = presence_streaks(tags, history_tags)
    present = {streak.category for streak in streaks}
    nominees = [category for category in nominate(tags, history_tags) if category not in present]
    if nominees:
        scores = await repeat_scores(text, history, nominees)
        streaks += [streak for category in nominees if (streak := confirm(category, scores[category]))]
    return Counter(streak.category for streak in streaks)


def flagged_sentences(draft: str, targets) -> tuple[list[str], list[str]]:
    """The draft's sentences split as the detectors split them, as (flagged, unflagged): flagged overlaps an Editor target."""
    flagged, unflagged = [], []
    for sentence in split_narration_sentences(draft):
        if any(sentence in target.span or target.span in sentence for target in targets):
            flagged.append(sentence)
        else:
            unflagged.append(sentence)
    return flagged, unflagged


def fate(sentence: str, final: str, final_sentences: list[str]) -> str:
    if sentence in final:
        return "kept"
    best = max((difflib.SequenceMatcher(None, sentence, other).ratio() for other in final_sentences), default=0.0)
    return "rewritten" if best >= REWRITE_RATIO else "removed"


async def score_turn(turn: Path, bank: list, patterns, history_tags_cache: dict) -> dict:
    summary = json.loads((turn / "summary.json").read_text())
    if not summary["complete"] or summary["reasoning"] or summary["reasoning_columns"]:
        raise ValueError(f"{turn.name}: not a clean thinking-off turn")
    draft, final = (turn / "draft.md").read_text(), (turn / "final.md").read_text()
    saved = json.loads((turn / "database_messages.json").read_text())[-1]
    if saved.get("writer_draft") not in (None, final):
        raise ValueError(f"{turn.name}: messages.content differs from messages.writer_draft")
    rows = json.loads((turn / "history.json").read_text())
    history = [row["content"] for row in reversed(rows) if row["role"] == "assistant"][:AUDIT_BASELINE_WINDOW]
    subject_history = history[:HISTORY_WINDOW]
    toggles = summary["editor_audit_toggles"]
    key = tuple(subject_history)
    if key not in history_tags_cache:
        history_tags_cache[key] = [await tag_text(text) for text in subject_history]
    history_tags = history_tags_cache[key]

    before_report, targets = audit(draft, history, summary["text"], bank, toggles)
    after_report, _ = audit(final, history, summary["text"], bank, toggles)
    before, after = finding_keys(before_report, draft), finding_keys(after_report, final)
    if toggles.get("subject_fixation"):
        before["subject_fixation"] = await subject_streaks(draft, subject_history, history_tags)
        after["subject_fixation"] = await subject_streaks(final, subject_history, history_tags)
    detectors = {
        name: {
            "draft": sum(before[name].values()),
            "final": sum(after[name].values()),
            "repaired": sum((before[name] - after[name]).values()),
            "introduced": sum((after[name] - before[name]).values()),
        }
        for name in before
    }
    flagged, unflagged = flagged_sentences(draft, targets)
    final_sentences = split_narration_sentences(final)
    fates = Counter(fate(sentence, final, final_sentences) for sentence in flagged)
    held_before, held_after = heldout_hits(draft, patterns), heldout_hits(final, patterns)
    return {
        "key": turn.name,
        "context": summary["context"],
        "arm": summary["arm"],
        "edited": draft != final,
        "words_draft": words(draft),
        "words_final": words(final),
        "detectors": detectors,
        "targets": len(targets),
        "flagged_sentences": dict(fates),
        "unflagged_sentences": len(unflagged),
        "unflagged_kept": sum(1 for sentence in unflagged if sentence in final),
        "heldout_draft": dict(held_before),
        "heldout_final": dict(held_after),
    }


def bootstrap(rows: list[dict], measure, resamples: int = 10000) -> list[float] | None:
    """95% interval of a ratio measure, resampling contexts with both arms' turns together."""
    by_context: dict[str, list[dict]] = {}
    for row in rows:
        by_context.setdefault(row["context"], []).append(row)
    groups = list(by_context.values())
    rng = random.Random(20261010)
    draws = []
    for _ in range(resamples):
        value = measure([row for group in rng.choices(groups, k=len(groups)) for row in group])
        if value is not None:
            draws.append(value)
    if len(draws) < resamples * 0.9:
        return None
    draws.sort()
    return [draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]]


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def summarize(rows: list[dict]) -> dict:
    names = list(rows[0]["detectors"])

    def total(name: str, field: str, subset=rows) -> int:
        return sum(row["detectors"][name][field] for row in subset)

    def group(subset: list[dict], members) -> dict:
        draft = sum(total(name, "draft", subset) for name in members)
        return {
            "draft": draft,
            "final": sum(total(name, "final", subset) for name in members),
            "repaired": sum(total(name, "repaired", subset) for name in members),
            "introduced": sum(total(name, "introduced", subset) for name in members),
        }

    def repair_rate(subset: list[dict], members) -> float | None:
        counts = group(subset, members)
        return ratio(counts["repaired"], counts["draft"])

    def per_kwords(subset: list[dict], field: str, members, side: str) -> float | None:
        return ratio(1000 * sum(total(name, field, subset) for name in members), sum(row[f"words_{side}"] for row in subset))

    def heldout(subset: list[dict], side: str) -> float | None:
        hits = sum(sum(row[f"heldout_{side}"].values()) for row in subset)
        return ratio(1000 * hits, sum(row[f"words_{side}"] for row in subset))

    def heldout_drop(subset: list[dict]) -> float | None:
        before, after = heldout(subset, "draft"), heldout(subset, "final")
        return None if before is None or after is None else ratio(before - after, before)

    def preservation(subset: list[dict]) -> float | None:
        return ratio(sum(row["unflagged_kept"] for row in subset), sum(row["unflagged_sentences"] for row in subset))

    def removed_share(subset: list[dict]) -> float | None:
        removed = sum(row["flagged_sentences"].get("removed", 0) for row in subset)
        changed = removed + sum(row["flagged_sentences"].get("rewritten", 0) for row in subset)
        return ratio(removed, changed)

    seven = [name for name in SEVEN if name in names]
    out = {
        "turns": len(rows),
        "contexts": len({row["context"] for row in rows}),
        "edited_turns": sum(row["edited"] for row in rows),
        "words_draft": sum(row["words_draft"] for row in rows),
        "words_final": sum(row["words_final"] for row in rows),
        "detectors": {name: {**group(rows, [name]), "repair_rate": repair_rate(rows, [name])} for name in names},
        "seven": {
            **group(rows, seven),
            "repair_rate": repair_rate(rows, seven),
            "repair_ci95": bootstrap(rows, lambda s: repair_rate(s, seven)),
            "draft_per_1k": per_kwords(rows, "draft", seven, "draft"),
            "final_per_1k": per_kwords(rows, "final", seven, "final"),
            "introduced_per_1k": per_kwords(rows, "introduced", seven, "final"),
            "introduced_ci95": bootstrap(rows, lambda s: per_kwords(s, "introduced", seven, "final")),
        },
        "heldout": {
            "draft_hits": sum(sum(row["heldout_draft"].values()) for row in rows),
            "final_hits": sum(sum(row["heldout_final"].values()) for row in rows),
            "draft_per_1k": heldout(rows, "draft"),
            "final_per_1k": heldout(rows, "final"),
            "drop": heldout_drop(rows),
            "drop_ci95": bootstrap(rows, heldout_drop),
        },
        "preservation": {
            "unflagged_sentences": sum(row["unflagged_sentences"] for row in rows),
            "kept": sum(row["unflagged_kept"] for row in rows),
            "rate": preservation(rows),
            "ci95": bootstrap(rows, preservation),
        },
        "flagged_sentences": dict(sum((Counter(row["flagged_sentences"]) for row in rows), Counter())),
        "removed_share": removed_share(rows),
    }
    for name in APART:
        if name in names:
            out[name] = {
                **group(rows, [name]),
                "repair_rate": repair_rate(rows, [name]),
                "introduced_per_1k": per_kwords(rows, "introduced", [name], "final"),
            }
    return out


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--heldout", type=Path, required=True, help=f"the pinned list, from {HELDOUT_URL}")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = {"heldout_url": HELDOUT_URL, "heldout_sha256": HELDOUT_SHA256, "runs": {}}
    for run in args.run:
        manifest = json.loads((run / "manifest.json").read_text())
        bank = phrase_bank(run)
        phrases, dropped = heldout_phrases(args.heldout, bank)
        patterns = [(phrase, re.compile(rf"(?<!\w){re.escape(normalize(phrase))}(?!\w)")) for phrase in phrases]
        cache: dict = {}
        rows = [await score_turn(turn, bank, patterns, cache) for turn in sorted((run / "turns").iterdir())]
        name = run.name
        save(args.output / f"{name}-turns.json.gz", rows)
        results["runs"][name] = {
            "run": run.name,
            "transport": manifest["transport"],
            "orb_commit": manifest["orb_commit"],
            "phrase_bank_groups": len(bank),
            "heldout_kept": len(phrases),
            "heldout_dropped": dropped,
            "pooled": summarize(rows),
            "by_arm": {arm: summarize([row for row in rows if row["arm"] == arm]) for arm in ("on", "off")},
        }
        print(json.dumps({"run": name, "seven": results["runs"][name]["pooled"]["seven"]}), flush=True)
    save(args.output / "summary.json", results)


if __name__ == "__main__":
    asyncio.run(main())
