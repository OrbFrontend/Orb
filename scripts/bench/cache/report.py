"""Rebuild the Bench 1 comparison report from scored evidence, without inference."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from statistics import median

from scripts.bench.cache.figure import SHORT, render, section
from scripts.bench.cache.orb_driver import save

ARMS = {"orb": "Orb", "tt-handoff": "TauriTavern handoff Profiles", "tt-single": "TauriTavern single Profile"}
TURN_FIELDS = [
    "block",
    "arm",
    "fixture",
    "turn",
    "status",
    "complete",
    "native_terminal_completed",
    "verified_saved_reply",
    "qualified",
    "qualification_errors",
    "observations",
    "native_wall_seconds",
    "first_prose_seconds",
    "model_seconds",
    "non_model_seconds",
    "attempt_elapsed_seconds",
    "model_calls",
    "generated_tokens",
    "reasoning_chars",
    "uncached_tokens",
    "actual_prompt_tokens",
    "first_prompt_tokens",
    "tool_calls",
    "tool_errors",
    "edit_batches",
    "patches",
    "audit_calls",
    "prose_words",
    "draft_words",
    "initial_findings",
    "final_findings",
    "repair_findings",
    "unflagged_sentences",
    "preserved_unflagged_sentences",
    "stages",
    "native_error",
    "error",
    "presentation_difference",
]
CALL_FIELDS = [
    "block",
    "arm",
    "turn",
    "stage",
    "invocation_id",
    "round",
    "attempt",
    "path",
    "seconds",
    "prompt_tokens",
    "cached_tokens",
    "uncached_tokens",
    "prompt_evaluated",
    "cache_n",
    "generated_tokens",
    "reasoning_chars",
    "forwarding_setup_ms",
    "full_history",
    "errors",
    "tools_sha256",
    "messages_sha256",
    "constraints",
    "provider_response_id",
    "association_method",
]


def rate(count, total):
    return f"{count:,}/{total:,} ({100 * count / total:.1f}%)" if total and count else (f"0/{total:,}" if total else "—")


def native_failed(row):
    return not row["complete"] if row["arm"] == "orb" else row.get("status") != "completed"


def size_of(row):
    return int(row["fixture"].removeprefix("bellwick-"))


def med(values, digits=1):
    values = [value for value in values if value is not None]
    return f"{median(values):.{digits}f}" if values else "—"


def total(rows, key):
    values = [row.get(key) for row in rows]
    return f"{sum(values):,}" if values and all(value is not None for value in values) else "unavailable"


def call_total(calls, key):
    """Sum a per-call usage figure; a call the provider cut off before reporting usage is counted, not hidden."""
    values = [call.get(key) for call in calls]
    known = sum(value for value in values if value is not None)
    missing = sum(value is None for value in values)
    return f"{known:,} (+{missing} call{'s' * (missing != 1)} without usage)" if missing else f"{known:,}"


def codes(value):
    if isinstance(value, dict):
        found = {value["code"]} if isinstance(value.get("code"), str) else set()
        return found | {code for child in value.values() for code in codes(child)}
    if isinstance(value, list):
        return {code for child in value for code in codes(child)}
    return set()


def write_csv(path, rows, fields):
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in row.items()}
            )


def phase(row):
    return "turn 1" if row["turn"] == 1 else "later turns"


def summary_rows(rows):
    """One row per arm × starting size × phase, for the report and the figure."""
    out = []
    for arm in ARMS:
        for size in sorted({size_of(row) for row in rows}):
            for name in ("turn 1", "later turns"):
                group = [row for row in rows if row["arm"] == arm and size_of(row) == size and phase(row) == name]
                if not group:
                    continue
                qualified = [row for row in group if row["qualified"]]
                out.append(
                    {
                        "arm": arm,
                        "size": size,
                        "phase": name,
                        "attempts": len(group),
                        "native_failures": sum(native_failed(row) for row in group),
                        "qualified": len(qualified),
                        "median_wall_seconds_qualified": median([r["native_wall_seconds"] for r in qualified])
                        if qualified
                        else None,
                        "median_wall_seconds_completed": median(
                            [r["native_wall_seconds"] for r in group if r["native_wall_seconds"] is not None]
                        )
                        if any(r["native_wall_seconds"] is not None for r in group)
                        else None,
                        "median_first_prose_seconds": median(
                            [r["first_prose_seconds"] for r in qualified if r["first_prose_seconds"] is not None]
                        )
                        if any(r["first_prose_seconds"] is not None for r in qualified)
                        else None,
                        "median_uncached_tokens": median(
                            [r["uncached_tokens"] for r in group if r["uncached_tokens"] is not None]
                        )
                        if any(r["uncached_tokens"] is not None for r in group)
                        else None,
                        "median_model_calls": median([r["model_calls"] for r in group]),
                        "median_generated_tokens": median([r["generated_tokens"] for r in group]),
                        "median_prompt_tokens": median(
                            [r["actual_prompt_tokens"] for r in group if r["actual_prompt_tokens"] is not None]
                        )
                        if any(r["actual_prompt_tokens"] is not None for r in group)
                        else None,
                    }
                )
    return out


WITH = {"orb": "in Orb", "tt-handoff": "with TauriTavern handoff Profiles", "tt-single": "with a single TauriTavern Profile"}
DEFECTS = [
    ("prose.unbalanced_quotes", "Unbalanced quotes"),
    ("prose.adjacent_quotes_after_comma", 'Two quoted lines joined after `,"` or a dash'),
    ("prose.capitalized_after_comma", 'A new sentence after `,"` where the tag should be (`dock," She looks`)'),
    ("prose.adjacent_quotes_after_stop", 'Two quoted lines joined after `."`, `?"` or `!"`'),
]


def ranked(counter):
    return dict(sorted(counter.items(), key=lambda pair: (-pair[1], pair[0])))


def top(counter, limit=5):
    items = [f"`{item}` {count}" for item, count in list(ranked(counter).items())[:limit]]
    rest = len(counter) - limit
    return ", ".join(items) + (f", {rest} more" if rest > 0 else "") if items else "none"


def joined(parts):
    return ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else "".join(parts)


def build(runs, output):
    rows = json.loads((runs / "turns.json").read_text())
    if not rows:
        raise ValueError("no scored turns")
    pilot = all(row["pilot"] for row in rows)
    if not pilot and any(row["pilot"] for row in rows):
        raise ValueError("pilot and reportable turns must not be pooled")
    blocks = sorted({row["block"] for row in rows})
    manifests = [
        json.loads((runs / block / "manifest.json").read_text())
        for block in blocks
        if (runs / block / "manifest.json").exists()
    ]
    identity = manifests[0] if manifests else {}
    output.mkdir(parents=True, exist_ok=True)
    (output / "turns.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n")
    write_csv(output / "turns.csv", rows, TURN_FIELDS)
    calls = []
    for row in rows:
        path = runs / row["block"] / f"turn-{row['turn']:02d}" / "scored-calls.json"
        for call in json.loads(path.read_text()) if path.exists() else []:
            call["path"] = Path(call["path"]).name
            calls.append({"block": row["block"], "arm": row["arm"], "turn": row["turn"], **call})
    write_csv(output / "calls.csv", calls, CALL_FIELDS)
    summary = summary_rows(rows)
    save(output / "summary.json", summary)
    write_csv(output / "summary.csv", summary, list(summary[0]))
    sizes = sorted({size_of(row) for row in rows})
    largest = sizes[-1]
    by_arm = {arm: [row for row in rows if row["arm"] == arm] for arm in ARMS}
    steady = {
        arm: med(
            [r["native_wall_seconds"] for r in group if r["qualified"] and phase(r) == "later turns" and size_of(r) == largest]
        )
        for arm, group in by_arm.items()
    }
    met = [f"{SHORT[arm]} {sum(r['qualified'] for r in group)}/{len(group)}" for arm, group in by_arm.items()]
    lines = [
        f"# Bench 1: Orb and TauriTavern on the same model{' (pilot)' if pilot else ''}",
        "",
        f"{len(rows)} turns of Orb and two custom TauriTavern agent configurations doing the same job on Gemma 4: write a scene "
        "direction, draft a reply from it, audit the draft with Orb's detectors, repair it under Orb's Editor stopping rule, "
        f"and save it. Starting histories of {joined([f'{size:,}' for size in sizes])} tokens, "
        f"{max(row['turn'] for row in rows)} scripted consecutive turns per block, "
        f"{len({m['repeat'] for m in manifests})} repeat blocks per size and arm. llama-server restarts cold before every "
        "block and block order rotates across repeats; later turns keep each application's own replies.",
        "",
        f"**At the {largest:,}-token start, turns 2–10 take a median "
        + joined([f"{steady[arm]} s {WITH[arm]}" for arm in ARMS])
        + f". Turns meeting the task contract: {', '.join(met)}.**",
        "",
        "There is one frozen history per size, so these are descriptive results for these configurations, not population "
        "intervals or a claim about every TauriTavern configuration.",
        "",
        *section(rows),
        "",
        "## Completion",
        "",
        "Every arm must write a valid scene direction before its draft and use it, audit the draft with the same detectors, "
        "re-audit after every edit batch, and save the final reply intact; TauriTavern's trailing-whitespace cleanup counts as "
        "intact. A missing moods field, an empty required field, a whole-file rewrite and every native failure fail the turn. "
        "An unknown mood id next to valid ones, or a list field given as one delimited string, is an observation: Orb drops "
        "unknown ids and renders either shape into Scene Guidance unchanged. Thinking is off on the wire in every request and "
        "verified there.",
        "",
    ]
    per_size = {arm: [[r for r in group if size_of(r) == size] for size in sizes] for arm, group in by_arm.items()}
    block_turns = {len(part) for parts in per_size.values() for part in parts}
    uniform = len(block_turns) == 1
    lines.extend(
        [
            f"| Configuration | Attempts | Native failures | Met the task contract | Met it by start, {' / '.join(f'{size:,}' for size in sizes)}"
            + (f" (of {block_turns.pop()} each)" if uniform else "")
            + " |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, group in by_arm.items():
        lines.append(
            f"| {ARMS[arm]} | {len(group)} | {rate(sum(native_failed(r) for r in group), len(group))} | "
            f"{rate(sum(r['qualified'] for r in group), len(group))} | "
            + " / ".join(
                f"{sum(r['qualified'] for r in part)}" + ("" if uniform else f"/{len(part)}") for part in per_size[arm]
            )
            + " |"
        )
    (output / "figure.svg").write_text(render(rows))
    lines.extend(
        [
            "",
            "## Latency and cost per turn",
            "",
            "Wall time runs from the driver's trigger to the confirmed final save: Orb's `done` after persistence, or TauriTavern's "
            "completed Run with its chat settled, less the driver's 0.5 s polling delay (the raw value is `driver_wall_seconds`). "
            "First prose is Orb's first Writer token at the client and TauriTavern's first reply text rendered in its chat. "
            "Wall and first-prose times are medians over qualified turns; uncached input, calls and tokens are medians over all "
            "attempts. Turn 1 follows a cold server start.",
            "",
            "| Configuration | Start | Turns | Qualified | Wall s | First prose s | Uncached input | Model calls | Generated tokens | Prompt tokens, max |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary:

        def show(value, digits=1):
            return "—" if value is None else f"{value:,.{digits}f}"

        lines.append(
            f"| {ARMS[row['arm']]} | {row['size']:,} | {row['phase']} | {row['qualified']}/{row['attempts']} | "
            f"{show(row['median_wall_seconds_qualified'])} | {show(row['median_first_prose_seconds'])} | "
            f"{show(row['median_uncached_tokens'], 0)} | {show(row['median_model_calls'], 0)} | "
            f"{show(row['median_generated_tokens'], 0)} | {show(row['median_prompt_tokens'], 0)} |"
        )
    lines.extend(
        [
            "",
            "### Where the time went",
            "",
            "Model seconds sum the turn's recorded model calls, recorder ingress to stream end. The rest is application work: "
            "prompt assembly, tools, the auditor, saving and, for TauriTavern, its WebView bridge.",
            "",
            "| Configuration | Median wall s | Median model s | Median non-model s | Median calls |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        group = [row for row in by_arm[arm] if row["qualified"]]
        if group:
            lines.append(
                f"| {name} | {med([r['native_wall_seconds'] for r in group])} | {med([r['model_seconds'] for r in group])} | "
                f"{med([r['non_model_seconds'] for r in group])} | {med([r['model_calls'] for r in group], 0)} |"
            )
    lines.extend(
        [
            "",
            "## Repair",
            "",
            "Repair is scored where Orb's Editor stopping rule stops: after a re-audit that is clean, leaves no flagged sentence, or "
            "did not lower the issue count, and after at most three batches. Orb enforces the rule in code; TauriTavern Profiles "
            "are told it, and editing past it stays in their time and calls. Findings are per 1,000 draft words. Preserved "
            "sentences are unflagged draft sentences that survive verbatim in the saved reply.",
            "",
            "| Configuration | Qualified turns | Clean drafts | Findings per 1,000 words: draft → at stop rule | Turns with findings left | Turns edited past the rule | Unflagged sentences preserved |",
            "| --- | ---: | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        group = [
            row
            for row in by_arm[arm]
            if row["qualified"] and row["initial_findings"] is not None and row.get("repair_findings") is not None
        ]
        if not group:
            continue
        before = 1000 * sum(r["initial_findings"] for r in group) / max(sum(r["draft_words"] for r in group), 1)
        after = 1000 * sum(r["repair_findings"] for r in group) / max(sum(r["draft_words"] for r in group), 1)
        lines.append(
            f"| {name} | {len(group)} | {sum(r['initial_findings'] == 0 for r in group)} | {before:.1f} → {after:.1f} | "
            f"{sum(bool(r['repair_findings']) for r in group)} | "
            f"{sum('editor.edited_past_stop_rule' in r.get('observations', []) for r in group)} | "
            f"{rate(sum(r['preserved_unflagged_sentences'] for r in group), sum(r['unflagged_sentences'] for r in group))} |"
        )
    damage = []
    for arm, group in by_arm.items():
        for key, label in DEFECTS:
            drafted = sum(f"{key}.draft" in row.get("observations", []) for row in group)
            edited = sum(f"{key}.editing" in row.get("observations", []) for row in group)
            if drafted or edited:
                damage.append(f"| {ARMS[arm]} | {label} | {drafted}/{len(group)} | {edited}/{len(group)} |")
    lines.extend(
        [
            "",
            "A mechanical punctuation check runs on every saved reply in every arm and attributes each defect to the Writer's "
            "draft or to editing, counting turns.",
        ]
    )
    if damage:
        lines.extend(
            ["", "| Configuration | Defect | From the draft | From editing |", "| --- | --- | ---: | ---: |", *damage, ""]
        )
        lines.append("No other defect appears in any arm.")
    else:
        lines.append("No arm has any defect.")
    orb = [row for row in by_arm["orb"] if row.get("editor_replay_matches") is not None]
    by_round = Counter(
        (round_, defect)
        for row in orb
        for round_, defects in (row.get("editing_defects_by_round") or {}).items()
        for defect in defects
    )
    if orb:
        lines[-1] += (
            f" Replaying Orb's Editor rounds with the pinned code reproduces {sum(row['editor_replay_matches'] for row in orb)} "
            f"of {len(orb)} edited Orb replies byte for byte"
            + (
                "; its editing defects came from "
                + joined(
                    [
                        f"round {round_} (`{defect}`, {count} turn{'s' * (count != 1)})"
                        for (round_, defect), count in sorted(by_round.items())
                    ]
                )
                if by_round
                else ""
            )
            + "."
        )
    findings = {}
    for arm in ARMS:
        findings[arm] = {
            "native_errors": ranked(
                Counter(code for row in by_arm[arm] for code in codes([row.get("native_error"), row.get("native_failures")]))
            ),
            "contract_failures": ranked(
                Counter(item for row in by_arm[arm] for item in set(row.get("qualification_errors", [])))
            ),
            "observations": ranked(Counter(item for row in by_arm[arm] for item in set(row.get("observations", [])))),
        }
    save(output / "findings.json", findings)
    lines.extend(
        [
            "",
            "## Failures and observations",
            "",
            "Attempts per cause, most frequent first. Causes overlap, so they do not add up to the failure rate; every count is in "
            "[findings.json](findings.json).",
            "",
            "| Configuration | Native errors | Task-contract failures | Observations (not disqualifying) |",
            "| --- | --- | --- | --- |",
            *[
                f"| {ARMS[arm]} | {top(found['native_errors'])} | {top(found['contract_failures'])} | {top(found['observations'])} |"
                for arm, found in findings.items()
            ],
            "",
            "## Work performed, including failed attempts",
            "",
            "| Configuration | Model calls | Generated tokens | Uncached input tokens | Failed tool calls | Edit batches |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        arm_calls = [call for call in calls if call["arm"] == arm]
        lines.append(
            f"| {name} | {len(arm_calls):,} | {call_total(arm_calls, 'generated_tokens')} | {call_total(arm_calls, 'uncached_tokens')} | "
            f"{total(by_arm[arm], 'tool_errors')} | {total(by_arm[arm], 'edit_batches')} |"
        )
    lines.extend(
        [
            "",
            "Orb's turn log has no failed-tool-call count. Generated tokens include tool-call JSON and any reasoning-channel output.",
            "",
            "| Configuration / stage | Calls | Model seconds | Generated tokens | Uncached input tokens |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        for stage in sorted({call["stage"] for call in calls if call["arm"] == arm}):
            group = [call for call in calls if call["arm"] == arm and call["stage"] == stage]
            values = [call.get("seconds") for call in group]
            seconds = f"{sum(values):,.1f}" if all(value is not None for value in values) else "unavailable"
            lines.append(
                f"| {name} / {stage} | {len(group):,} | {seconds} | {call_total(group, 'generated_tokens')} | {call_total(group, 'uncached_tokens')} |"
            )
    overhead = [call["forwarding_setup_ms"] for call in calls]
    cache_available = [call for call in calls if call["uncached_tokens"] is not None and call["prompt_evaluated"] is not None]
    cache_mismatch = [
        call["path"]
        for call in cache_available
        if call["uncached_tokens"] != call["prompt_evaluated"] or call["cached_tokens"] != call["cache_n"]
    ]
    save(output / "cache-crosscheck.json", {"available_calls": len(cache_available), "mismatches": cache_mismatch})
    save(output / "association-crosscheck.json", Counter(call.get("association_method", "Orb tool_choice") for call in calls))
    lines.extend(
        [
            "",
            f"The recorder added a median {median(overhead):.3f} ms of forwarding setup, {max(overhead):.3f} ms at most, over "
            f"{len(overhead):,} calls. Provider cache usage matched llama-server's timings on {len(cache_available) - len(cache_mismatch):,} "
            f"of {len(cache_available):,} calls ([cache-crosscheck.json](cache-crosscheck.json)); request-to-stage associations are in "
            "[association-crosscheck.json](association-crosscheck.json).",
        ]
    )
    notes = runs / "NOTES.md"
    if notes.exists():
        nested = re.sub(r"^(#+) ", lambda match: "#" * (len(match.group(1)) + 2) + " ", notes.read_text().strip(), flags=re.M)
        lines.extend(["", "## Disclosures", "", nested])
    harnesses = Counter(m.get("harness_commit", "unrecorded") for m in manifests)
    lines.extend(
        [
            "",
            "## Configuration",
            "",
            f"Orb `{identity.get('orb_commit', 'unrecorded')}`; TauriTavern 2.3.0 `{identity.get('tauritavern_commit', 'unrecorded')}` "
            f"(release binary SHA-256 `{identity.get('tauritavern_binary_sha256', 'unrecorded')}`); benchmark harness "
            + joined([f"`{commit}` ({count} blocks)" for commit, count in ranked(harnesses).items()])
            + (" with uncommitted changes" if any(m.get("harness_dirty", True) for m in manifests) else "")
            + ".",
            "",
            f"Gemma 4 26B-A4B QAT UD-Q4_K_XL (SHA-256 `{identity.get('model_sha256', 'unrecorded')}`) on llama.cpp "
            f"({'; '.join(identity.get('llama_server_version', [])) or 'build unrecorded'}), RTX 3090 at 270 W, Ubuntu 24.04. "
            "Server: 49,152 context, one slot, 4,096 MiB host cache, f16 KV, flash attention, no speculative decoding. "
            "Samplers in every request: temperature 0.8, top_k 40, top_p 0.95, min_p 0, repetition penalty 1, max_tokens 4096, cache_prompt, no seed.",
            "",
            "Both applications, the request recorder and the MCP auditor ran on the inference host over localhost. "
            "TauriTavern ran its release binary in a software-rendered WebKitGTK WebView under Xvfb, driven through its host Agent API with the real prompt-assembly and chat-commit bridges.",
            "",
            "TauriTavern Profiles ([tt_setup.js](../../tt_setup.js)): full chat history through a saved preset with stable content before stage instructions; "
            "plans, skills, world info and delegation off; 32 rounds and 80 calls per invocation; three model retries. "
            "Both configurations write the direction to a plain-text workspace file (`scratch/direction.md`, one field per line); the handoff Director then hands off "
            "to the Writer, which reads that file before drafting, and the Writer hands off to the Editor. "
            "Prompts carry no JSON examples, because Gemma 4 writes tool arguments in its own quoting syntax and imitated JSON quoting can leave an argument string unterminated. "
            "The auditor returns the same numbered report and per-category fixing rules that Orb's Editor reads, reworded only where Orb names sentence ids. "
            "Orb runs its seeded defaults with the Editor on (`defaults.json`).",
            "",
            "Every arm gets the same system prompt, card, persona, direction fields and mood descriptions, Orb's Director brief, the same Scene Guidance reading of the direction, "
            "the same audit report and fixing rules, and the same Editor stopping rule. Native differences kept on purpose: Orb's Director sees the previously active moods "
            "and Orb releases an ended mood with its negative prompt, while each TauriTavern turn starts from an empty workspace with no mood state; "
            "Orb's first prose is timed at the client before rendering, TauriTavern's when its WebView renders it; neither arm is told a reply length.",
            "",
            "Per-attempt data: [turns.csv](turns.csv), [turns.json](turns.json); per-call costs: [calls.csv](calls.csv); "
            "grouped medians: [summary.csv](summary.csv); figure: [figure.svg](figure.svg).",
            "",
        ]
    )
    (output / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps({"attempts": len(rows), "qualified": sum(row["qualified"] for row in rows), "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.runs, args.output)
