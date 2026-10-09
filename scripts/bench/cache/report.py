"""Rebuild the Bench 1 comparison report from scored evidence, without inference."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from statistics import median

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
    return f"{count}/{total} ({100 * count / total:.1f}%)" if total else "—"


def native_failed(row):
    return not row["complete"] if row["arm"] == "orb" else row.get("status") != "completed"


def size_of(row):
    return int(row["fixture"].removeprefix("bellwick-"))


def med(values, digits=1):
    values = [value for value in values if value is not None]
    return f"{median(values):.{digits}f}" if values else "—"


def total(rows, key):
    values = [row.get(key) for row in rows]
    return sum(values) if values and all(value is not None for value in values) else "unavailable"


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


def build(runs, output):
    rows = json.loads((runs / "turns.json").read_text())
    if not rows:
        raise ValueError("no scored turns")
    pilot = all(row["pilot"] for row in rows)
    if not pilot and any(row["pilot"] for row in rows):
        raise ValueError("pilot and reportable turns must not be pooled")
    manifests = [json.loads(path.read_text()) for path in sorted(runs.glob("*/manifest.json"))]
    identity = manifests[0] if manifests else {}
    output.mkdir(parents=True, exist_ok=True)
    save(output / "turns.json", rows)
    write_csv(output / "turns.csv", rows, TURN_FIELDS)
    calls = []
    for row in rows:
        path = runs / row["block"] / f"turn-{row['turn']:02d}" / "scored-calls.json"
        for call in json.loads(path.read_text()) if path.exists() else []:
            calls.append({"block": row["block"], "arm": row["arm"], "turn": row["turn"], **call})
    save(output / "calls.json", calls)
    write_csv(output / "calls.csv", calls, CALL_FIELDS)
    summary = summary_rows(rows)
    save(output / "summary.json", summary)
    write_csv(output / "summary.csv", summary, list(summary[0]))
    sizes = sorted({size_of(row) for row in rows})
    qualified = sum(row["qualified"] for row in rows)
    kind = "pilot" if pilot else "sweep"
    lines = [
        f"# Bench 1 comparison {kind}",
        "",
        f"{len(rows)} attempted turns across Orb and two custom TauriTavern configurations; {qualified} met the shared task contract. "
        f"Starting histories: {', '.join(f'{size:,}' for size in sizes)} nominal tokens; "
        f"{max(row['turn'] for row in rows)} consecutive scripted user turns per block; "
        f"{len({json.loads(path.read_text())['repeat'] for path in runs.glob('*/manifest.json')})} repeat block(s) per size and arm. "
        "llama-server restarted cold before every block, and block order rotated across repeats. "
        "Starting histories were shared; later turns kept each application's own replies, including the effect of failed turns.",
        "",
        "There is one frozen history per size, so these are descriptive results for this fixture and these configurations, "
        "not population intervals and not a claim about every TauriTavern configuration.",
        "",
        "## Task contract",
        "",
        "Every arm must produce a valid scene direction before its draft and use it, audit the draft with the same detectors, "
        "re-audit after every edit batch, and save the final reply intact. TauriTavern's native save cleanup (trailing whitespace) counts as intact. "
        "How editing ended (findings left, more than three batches), reasoning-channel output and save cleanup are reported below as observations for every arm; "
        "they do not disqualify a turn. Thinking is disabled on the wire in every request and verified there.",
        "",
        "## Completion",
        "",
        "| Configuration / starting history | Attempts | Native failures | Qualified |",
        "| --- | ---: | ---: | ---: |",
    ]
    for arm, name in ARMS.items():
        for size in [None, *sizes]:
            group = [row for row in rows if row["arm"] == arm and (size is None or size_of(row) == size)]
            if not group:
                continue
            label = name + (" / all" if size is None else f" / {size:,}")
            lines.append(
                f"| {label} | {len(group)} | {rate(sum(native_failed(r) for r in group), len(group))} | "
                f"{rate(sum(r['qualified'] for r in group), len(group))} |"
            )
    lines.extend(
        [
            "",
            "## Latency and cost per turn",
            "",
            "Wall time runs from the driver's trigger to Orb's `done` after persistence, or to TauriTavern's completed Run with its chat presentation settled. "
            "Medians over qualified turns; turn 1 follows a cold server start and is reported apart from turns 2–10. "
            "First visible prose is Orb's first streamed Writer token at the client, and TauriTavern's first reply text rendered in its chat message "
            "(checked against the start of the reply). Uncached input, calls and generated tokens are medians over all attempts, failures included.",
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
            "Model seconds sum each turn's recorded model calls (recorder ingress to stream end). Non-model seconds are the rest of the wall time: "
            "application work, prompt assembly, tool execution, the auditor, chat saving and, for TauriTavern, its WebView bridge and 0.5 s polling.",
            "",
            "| Configuration | Median wall s | Median model s | Median non-model s | Median calls |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        group = [row for row in rows if row["arm"] == arm and row["qualified"]]
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
            "Findings come from the shared auditor on the pre-edit draft and the saved reply. Preserved sentences are unflagged draft sentences that survive verbatim.",
            "",
            "| Configuration | Qualified turns | Clean drafts | Mean findings, draft → final | Turns with findings left | Unflagged sentences preserved |",
            "| --- | ---: | ---: | --- | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        group = [row for row in rows if row["arm"] == arm and row["qualified"] and row["initial_findings"] is not None]
        if not group:
            continue
        before = sum(r["initial_findings"] for r in group) / len(group)
        after = sum(r["final_findings"] or 0 for r in group) / len(group)
        lines.append(
            f"| {name} | {len(group)} | {sum(r['initial_findings'] == 0 for r in group)} | {before:.2f} → {after:.2f} | "
            f"{sum(bool(r['final_findings']) for r in group)} | "
            f"{rate(sum(r['preserved_unflagged_sentences'] for r in group), sum(r['unflagged_sentences'] for r in group))} |"
        )
    lines.extend(["", "## Failures and observations", "", "### Native error codes", ""])
    lines.extend(["| Configuration | Error code | Attempts with code |", "| --- | --- | ---: |"])
    native_codes = []
    for arm, name in ARMS.items():
        counts = Counter(
            code for row in rows if row["arm"] == arm for code in codes([row.get("native_error"), row.get("native_failures")])
        )
        for code, count in counts.most_common():
            lines.append(f"| {name} | `{code}` | {count} |")
            native_codes.append({"arm": arm, "code": code, "attempts": count})
        if not counts:
            lines.append(f"| {name} | None recorded | 0 |")
    for title, key in (
        ("Task-contract failures", "qualification_errors"),
        ("Observations (not disqualifying)", "observations"),
    ):
        lines.extend(["", f"### {title}", "", "| Configuration | Finding | Attempts |", "| --- | --- | ---: |"])
        for arm, name in ARMS.items():
            counts = Counter(item for row in rows if row["arm"] == arm for item in set(row.get(key, [])))
            for item, count in counts.most_common():
                lines.append(f"| {name} | `{item}` | {count} |")
            if not counts:
                lines.append(f"| {name} | None | 0 |")
    lines.extend(
        [
            "",
            "Causes overlap and do not add up to the failure rate. A missing stage after an aborted run is a finding, not a separate attempt.",
            "",
            "## Work performed, including failed attempts",
            "",
            "| Configuration | Model calls | Generated tokens | Uncached input tokens | Failed tool calls | Edit batches |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        group = [row for row in rows if row["arm"] == arm]
        arm_calls = [call for call in calls if call["arm"] == arm]
        lines.append(
            f"| {name} | {len(arm_calls)} | {total(arm_calls, 'generated_tokens')} | {total(arm_calls, 'uncached_tokens')} | "
            f"{total(group, 'tool_errors')} | {total(group, 'edit_batches')} |"
        )
    lines.extend(
        [
            "",
            "Orb's turn log does not expose a failed-tool-call denominator, so its column reads `unavailable`. Generated tokens include tool-call JSON and any reasoning-channel output.",
            "",
            "| Configuration / stage | Calls | Model seconds | Generated tokens | Uncached input tokens |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, name in ARMS.items():
        for stage in sorted({call["stage"] for call in calls if call["arm"] == arm}):
            group = [call for call in calls if call["arm"] == arm and call["stage"] == stage]
            seconds = total(group, "seconds")
            seconds = f"{seconds:.1f}" if isinstance(seconds, (float, int)) else seconds
            lines.append(
                f"| {name} / {stage} | {len(group)} | {seconds} | {total(group, 'generated_tokens')} | {total(group, 'uncached_tokens')} |"
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
    save(output / "failure-codes.json", native_codes)
    lines.extend(
        [
            "",
            f"Recorder forwarding setup: median {median(overhead):.3f} ms, maximum {max(overhead):.3f} ms over {len(overhead)} calls. "
            f"Provider cache usage matched llama-server timings in {len(cache_available) - len(cache_mismatch)} of {len(cache_available)} calls "
            "([cache-crosscheck.json](cache-crosscheck.json)); request-to-stage associations: [association-crosscheck.json](association-crosscheck.json).",
            "",
            "## Configuration",
            "",
            f"Orb `{identity.get('orb_commit', 'unrecorded')}`; TauriTavern 2.3.0 `{identity.get('tauritavern_commit', 'unrecorded')}` "
            f"(release binary SHA-256 `{identity.get('tauritavern_binary_sha256', 'unrecorded')}`); "
            f"benchmark harness `{identity.get('harness_commit', 'unrecorded')}`"
            + (" with uncommitted changes" if identity.get("harness_dirty", True) else "")
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
            "The handoff Director passes its direction as fields of the native handoff brief; the single Profile writes a plain-text direction file. "
            "Prompts carry no JSON examples, because Gemma 4 writes tool arguments in its own quoting syntax and imitated JSON quoting trapped earlier runs in an unterminated argument string. "
            "The auditor returns the same numbered report and per-category fixing rules that Orb's Editor reads, reworded only where Orb names sentence ids. "
            "Orb runs its seeded defaults with the Editor on (`defaults.json`).",
            "",
            "Per-attempt data: [turns.csv](turns.csv), [turns.json](turns.json); per-call costs: [calls.csv](calls.csv); "
            "grouped medians: [summary.csv](summary.csv).",
            "",
        ]
    )
    (output / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps({"attempts": len(rows), "qualified": qualified, "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.runs, args.output)
