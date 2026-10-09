"""Score native completion, workload qualification, failures and recorded costs.

Both applications face the same task contract: a valid direction consumed before the draft, an audit of the draft,
a re-audit after every edit batch, and the final reply saved intact. That contract decides qualification. How the
Editor ended (findings left, extra batches), reasoning-channel output and the native save cleanup are reported as
observations for every arm, because they describe outcome and cost rather than whether the task was done.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from backend.analysis.text.roleplay_segmentation import split_segment_sentences
from scripts.bench.auditor import contextual_audit
from scripts.bench.cache.orb_driver import save, saved_difference

SAMPLERS = {
    "temperature": 0.8,
    "top_k": 40,
    "top_p": 0.95,
    "min_p": 0,
    "repetition_penalty": 1,
    "max_tokens": 4096,
    "cache_prompt": True,
}
EDIT_BATCH_LIMIT = 3
LIST_FIELDS = {"moods"}
# Gemma 4 channel markers that llama.cpp can route to the reasoning field without any reasoning text.
CHANNEL_MARKERS = re.compile(r"<\|channel>|<channel\|>|\bthought\b")


def read(path):
    return json.loads(path.read_text())


def read_optional(path, default):
    try:
        return read(path)
    except (OSError, ValueError):
        return default


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utc_ns(value):
    return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9)


def stream_choices(path):
    """Yield streamed choice deltas; a malformed frame is counted, not fatal."""
    malformed = 0
    deltas = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("data: {"):
            continue
        try:
            frame = json.loads(line[6:])
        except ValueError:
            malformed += 1
            continue
        deltas.extend(choice.get("delta", {}) for choice in frame.get("choices", []))
    return deltas, malformed


def wire_facts(path):
    metadata = read(path / "metadata.json")
    body = read_optional(path / "request.bin", {})
    errors = [f"wire.{key}" for key, value in SAMPLERS.items() if body.get(key) != value]
    if "seed" in body:
        errors.append("wire.seed")
    if body.get("chat_template_kwargs", {}).get("enable_thinking") is not False:
        errors.append("wire.thinking")
    deltas, malformed = stream_choices(path / "response.bin")
    if malformed:
        errors.append("response.malformed_sse")
    reasoning = "".join(str(delta.get("reasoning_content") or delta.get("reasoning") or "") for delta in deltas)
    if not metadata.get("upstream_complete") or not metadata.get("downstream_complete") or metadata.get("status") != 200:
        errors.append("response.incomplete")
    usage = metadata.get("usage", {})
    prompt, generated = usage.get("prompt_tokens"), usage.get("completion_tokens")
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens")
    timings = metadata.get("timings", {})
    return {
        "path": str(path),
        "metadata": metadata,
        "body": body,
        "errors": errors,
        # Thinking is verified off on the wire above; any reasoning-channel text is reported, and its tokens are cost.
        "reasoning_chars": len(CHANNEL_MARKERS.sub("", reasoning).strip()),
        "channel_marker_only": bool(reasoning) and not CHANNEL_MARKERS.sub("", reasoning).strip(),
        "content": "".join(str(delta.get("content") or "") for delta in deltas),
        "prompt_tokens": prompt,
        "generated_tokens": generated,
        "cached_tokens": cached,
        "uncached_tokens": prompt - cached if prompt is not None and cached is not None else None,
        "prompt_evaluated": timings.get("prompt_n"),
        "cache_n": timings.get("cache_n"),
        "seconds": (metadata["stream_end_ns"] - metadata["ingress_ns"]) / 1e9 if metadata.get("stream_end_ns") else None,
        "forwarding_setup_ms": (metadata.get("upstream_start_ns", metadata["ingress_ns"]) - metadata["ingress_ns"]) / 1e6,
        "tools_sha256": digest(body.get("tools", [])),
        "messages_sha256": digest(body.get("messages", [])),
        "constraints": {key: body[key] for key in ("tool_choice", "response_format", "grammar", "json_schema") if key in body},
        "provider_response_id": metadata.get("id"),
    }


def message_text(content):
    """Project native text blocks to text; retain their hints in request.bin."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(block, dict) and block.get("type") == "text" for block in content):
        return "".join(block.get("text", "") for block in content)
    return None


def includes_history(body, history):
    messages = body.get("messages", [])
    cursor = 0
    matched = []
    for expected in history:
        for index in range(cursor, len(messages)):
            row = messages[index]
            if row.get("role") == expected["role"] and message_text(row.get("content")) == expected["content"]:
                matched.append(index)
                cursor = index + 1
                break
        else:
            return False, matched
    return True, matched


def direction_errors(direction, applied):
    if not isinstance(direction, dict):
        return ["direction.not_object"]
    errors = []
    moods = direction.get("moods")
    allowed = {row["id"] for row in applied["moods"] if row["enabled"]}
    if not isinstance(moods, list) or any(not isinstance(mood, str) or mood not in allowed for mood in moods):
        errors.append("direction.moods")
    for field in applied["fragments"]:
        if not field["enabled"] or field["field_type"] not in {"string", "array"}:
            continue
        value = direction.get(field["id"])
        if value is None:
            if field["required"]:
                errors.append("direction.required." + field["id"])
        elif field["field_type"] == "string" and (not isinstance(value, str) or (field["required"] and not value.strip())):
            errors.append("direction.type." + field["id"])
        elif field["field_type"] == "array" and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            errors.append("direction.type." + field["id"])
    return errors


def list_fields(applied):
    return LIST_FIELDS | {field["id"] for field in applied["fragments"] if field["enabled"] and field["field_type"] == "array"}


def split_items(value):
    separator = ";" if ";" in value else ","
    return [item.strip().strip("\"'`") for item in value.split(separator) if item.strip().strip("\"'`")]


def parse_direction_text(text, applied):
    """Parse the plain `field: value` direction file; markdown emphasis and bullets are tolerated.

    List fields may be separated by semicolons or commas. An empty `moods:` line is Orb's empty list (a neutral tone);
    an absent moods line is no answer, so it fails like a missing field.
    """
    lists = list_fields(applied)
    known = lists | {field["id"] for field in applied["fragments"] if field["enabled"]}
    direction = {}
    for line in text.splitlines():
        key, colon, value = line.partition(":")
        key = key.strip().strip("-*#` ").lower().replace(" ", "_")
        if colon and key in known and key not in direction:
            value = value.strip().strip("*` ")
            direction[key] = split_items(value) if key in lists else value
    return direction or None


def prose_defects(text):
    """Mechanical punctuation defects in a saved reply, applied to every arm (no prose-quality judgement)."""
    defects = []
    if text.count('"') % 2 or text.count("\u201c") != text.count("\u201d"):
        defects.append("prose.unbalanced_quotes")
    # Two quoted segments jammed together on one line, e.g. a patch that left `," "` behind.
    if re.search(r'[,.?!\u2026]"[ \t]+"', text):
        defects.append("prose.adjacent_quotes")
    return defects


def first_prose_text_matches(observed, draft):
    """The first visible text must be the start of the reply, not a placeholder or tool JSON."""
    seen = re.sub(r"\W+", "", observed or "").lower()
    reply = re.sub(r"\W+", "", draft or "").lower()
    return bool(seen) and reply.startswith(seen[:60])


def audit_results_for(audit_root, run_id):
    rows = []
    for path in audit_root.glob("*.json"):
        result = read_optional(path, None)
        if isinstance(result, dict) and result.get("run_id") == run_id:
            rows.append((result["monotonic_ns"], path, result))
    return [(path, result) for _, path, result in sorted(rows, key=lambda row: row[0])]


def empty_tt(error, summary):
    return {
        "errors": [error],
        "observations": [],
        "draft": "",
        "direction": None,
        "tool_calls": 0,
        "tool_errors": 0,
        "edit_batches": 0,
        "patches": 0,
        "audit_calls": 0,
        "native_failures": [{"type": "driver_error", "payload": {"message": summary.get("error")}}],
        "attempt_events": [],
        "response_events": {},
        "finish_reason": None,
        "presentation_difference": None,
    }


def qualify_tt(turn, summary, applied, audit_root):
    native = turn / "native-run"
    if not (native / "events.jsonl").exists():
        return empty_tt("native.not_started", summary)
    events = []
    for line in (native / "events.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    profiles = {
        event["payload"]["invocationId"]: event["payload"]["profileId"]
        for event in events
        if event["type"] == "agent_invocation_created"
    }
    finished = {event["payload"]["callId"]: event for event in events if event["type"] == "tool_call_completed"}
    tools = []
    for event in events:
        if event["type"] == "tool_call_requested":
            payload = event["payload"]
            done = finished.get(payload["callId"])
            arguments = read_optional(native / payload["argumentsRef"], {}) if payload.get("argumentsRef") else {}
            tools.append(
                {
                    **payload,
                    "seq": event["seq"],
                    "arguments": arguments if isinstance(arguments, dict) else {},
                    "ok": bool(done and not done["payload"].get("isError")),
                    "completion": done,
                }
            )
    errors, observations = [], []
    if summary.get("status") != "completed":
        errors.append("native.not_completed")
    elif not summary["complete"]:
        errors.append("completion.native_verification_failed")
    final_file = turn / "final.md"
    presentation_file = turn / "presentation.json"
    presentation_difference = None
    if final_file.exists() and presentation_file.exists():
        chat = read(presentation_file)["chat"]
        presentation_difference = saved_difference(final_file.read_text(), chat[-1]["mes"] if chat else "")
        if presentation_difference == "content" and summary.get("status") == "completed":
            errors.append("completion.final_file_chat_mismatch")
        elif presentation_difference == "cleanup":
            observations.append("save.native_whitespace_cleanup")
    ok = [tool for tool in tools if tool["ok"]]
    drafts = [
        tool for tool in ok if tool["name"] == "workspace.write_file" and tool["arguments"].get("path") == "output/main.md"
    ]
    audits = [tool for tool in ok if tool["name"] == "audit_draft"]
    patches = [tool for tool in ok if tool["name"] == "workspace.apply_patch"]
    commits = [tool for tool in ok if tool["name"] == "workspace.commit"]
    finishes = [tool for tool in ok if tool["name"] == "workspace.finish"]
    handoffs = [tool for tool in ok if tool["name"] == "agent.handoff"]

    first_draft_seq = drafts[0]["seq"] if drafts else None
    sources = [
        tool
        for tool in ok
        if tool["name"] == "workspace.write_file"
        and str(tool["arguments"].get("path", "")).startswith("scratch/direction")
        and (first_draft_seq is None or tool["seq"] < first_draft_seq)
    ]
    # The direction in force is the last one written before the draft.
    direction = parse_direction_text(str(sources[-1]["arguments"].get("content", "")), applied) if sources else None
    direction_seq = sources[-1]["seq"] if sources else None
    if not direction:
        errors.append("direction.missing")
    else:
        errors.extend(direction_errors(direction, applied))
    initial = ""
    if not drafts:
        errors.append("draft.missing")
    else:
        initial = drafts[-1]["arguments"].get("content", "")
        if direction_seq is None or direction_seq > first_draft_seq:
            errors.append("direction.not_before_draft")
        elif summary["arm"] == "tt-handoff" and not any(
            tool["name"] == "workspace.read_file"
            and str(tool["arguments"].get("path", "")) == sources[-1]["arguments"].get("path")
            and tool["invocationId"] == drafts[0]["invocationId"]
            and direction_seq < tool["seq"] < first_draft_seq
            for tool in ok
        ):
            # A handoff receiver starts a fresh transcript; it sees the direction only by reading the file.
            errors.append("direction.not_consumed_before_draft")
        if audits:
            before_audit = [tool for tool in drafts if tool["seq"] < audits[0]["seq"]]
            if before_audit:
                initial = before_audit[-1]["arguments"].get("content", "")
            if any(tool["seq"] > audits[0]["seq"] for tool in drafts):
                errors.append("editor.whole_file_rewrite")
    audit_results = []
    stored = audit_results_for(audit_root, summary.get("handle", {}).get("runId"))
    if audits and len(stored) != len(audits):
        errors.append("audit.result_unmatched")
    for path, result in stored:
        raw = path.with_suffix(".md")
        draft_bytes = raw.read_bytes() if raw.exists() else b""
        if hashlib.sha256(draft_bytes).hexdigest() != result["draft_sha256"]:
            errors.append("audit.bytes_mismatch")
            continue
        audited = contextual_audit(
            draft_bytes.decode(),
            read(turn / "history.json"),
            read(Path(__file__).parent / "fixtures" / (summary["fixture"] + ".json"))["user_script"][summary["turn"] - 1],
        )
        if any(audited[key] != result[key] for key in ("report", "targets", "numbered_report", "audit_toggles")):
            errors.append("audit.contextual_parity")
        snapshots = turn / "audit-snapshots"
        snapshots.mkdir(exist_ok=True)
        (snapshots / raw.name).write_bytes(draft_bytes)
        save(snapshots / path.name, result)
        audit_results.append({**result, "draft": draft_bytes.decode()})
    if not audits:
        errors.append("audit.missing")
    elif audit_results:
        initial = audit_results[0]["draft"]
        if final_file.exists() and hashlib.sha256(final_file.read_bytes()).hexdigest() != audit_results[-1]["draft_sha256"]:
            errors.append("audit.final_bytes_mismatch")
    batches = set()
    for patch in patches:
        previous = [index for index, audit in enumerate(audits) if audit["seq"] < patch["seq"]]
        following = [audit for audit in audits if audit["seq"] > patch["seq"]]
        if not previous or not following:
            errors.append("audit.missing_before_or_after_edits")
        else:
            batches.add(previous[-1])
    if len(batches) > EDIT_BATCH_LIMIT:
        observations.append("editor.edit_batch_limit_exceeded")
    if not commits or not finishes or not audits or not (audits[-1]["seq"] < commits[-1]["seq"] < finishes[-1]["seq"]):
        errors.append("completion.audit_commit_finish_order")
    if patches and commits and patches[-1]["seq"] > commits[-1]["seq"]:
        errors.append("completion.edit_after_commit")
    expected = (
        ["benchmark-director", "benchmark-writer", "benchmark-editor"]
        if summary["arm"] == "tt-handoff"
        else ["benchmark-single"]
    )
    if list(profiles.values()) != expected:
        errors.append("stages.invocation_sequence")
    if summary["arm"] == "tt-handoff":
        if [tool["arguments"].get("agentId") for tool in handoffs] != expected[1:]:
            errors.append("stages.handoff_sequence")
        for tool in ok:
            stage = profiles.get(tool["invocationId"])
            editing = tool["name"] in {"audit_draft", "workspace.apply_patch", "workspace.commit", "workspace.finish"}
            drafting = tool["name"] == "workspace.write_file" and tool["arguments"].get("path") == "output/main.md"
            if (
                (stage == "benchmark-director" and (editing or drafting))
                or (stage == "benchmark-writer" and editing)
                or (stage == "benchmark-editor" and tool["name"] == "agent.handoff")
            ):
                errors.append("stages.unexpected_action")
    failures = [
        {
            **event,
            "stage": profiles.get(
                event["payload"].get("invocationId") or event["payload"].get("eventScope", {}).get("invocationId"),
                "unassociated",
            ).removeprefix("benchmark-"),
        }
        for event in events
        if event["type"] in {"tool_call_failed", "model_call_attempt_failed", "run_failed"}
        or (event["type"] == "tool_call_completed" and event["payload"].get("isError"))
    ]
    calls = [
        {
            "timestamp_ns": utc_ns(event["timestamp"]),
            "stage": profiles.get(event["payload"]["invocationId"], "unassociated").removeprefix("benchmark-"),
            **event["payload"],
        }
        for event in events
        if event["type"] == "model_call_attempt_started"
    ]
    return {
        "errors": sorted(set(errors)),
        "observations": observations,
        "draft": initial,
        "direction": direction,
        "tool_calls": len(tools),
        "tool_errors": sum(not tool["ok"] for tool in tools),
        "edit_batches": len(batches),
        "patches": len(patches),
        "audit_calls": len(audits),
        "native_failures": failures,
        "attempt_events": calls,
        "response_events": {
            event["payload"]["responseId"]: {
                **event["payload"],
                "stage": profiles.get(event["payload"]["invocationId"], "unassociated").removeprefix("benchmark-"),
            }
            for event in events
            if event["type"] == "model_response_stored" and event["payload"].get("responseId")
        },
        "finish_reason": finishes[-1]["arguments"].get("reason") if finishes else None,
        "presentation_difference": presentation_difference,
    }


def score_orb(turn, summary, selected, applied):
    if not all((turn / name).exists() for name in ("logs.json", "database_messages.json", "events.jsonl")):
        return {
            "errors": ["native.not_completed", "orb.artifacts_missing"],
            "observations": [],
            "draft": (turn / "draft.md").read_text() if (turn / "draft.md").exists() else "",
            "direction": None,
            "tool_calls": 0,
            "tool_errors": None,
            "edit_batches": 0,
            "patches": 0,
            "audit_calls": None,
            "native_failures": [{"type": "driver_error", "payload": {"message": summary.get("error")}}],
        }
    draft = (turn / "draft.md").read_text() if (turn / "draft.md").exists() else ""
    logs = read(turn / "logs.json")
    persisted = read(turn / "database_messages.json")
    log = next((row for row in logs if persisted and row["message_id"] == persisted[-1]["id"]), {})
    tools = log.get("tool_calls", [])
    directions = [tool["arguments"] for tool in tools if tool["name"] == "direct_scene"]
    errors = direction_errors(directions[0], applied) if directions else ["direction.missing"]
    if not summary["complete"]:
        errors.append("native.not_completed")
    events = [json.loads(line) for line in (turn / "events.jsonl").read_text().splitlines()]
    if not any(event["event"] == "writer_done" and event["data"].get("editor_will_run") for event in events):
        errors.append("audit.editor_not_enabled")
    for call in selected:
        choice = call["body"].get("tool_choice")
        name = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
        call["stage"] = "director" if name == "direct_scene" else "editor" if name and name.startswith("editor_") else "writer"
    raw_draft = "".join(call["content"] for call in selected if call["stage"] == "writer")
    if raw_draft != draft:
        errors.append("observer.draft_mismatch")
    edits = [tool for tool in tools if tool["name"] == "editor_apply_patch"]
    return {
        "draft": draft,
        "direction": directions[0] if directions else None,
        "errors": errors,
        "observations": [],
        "tool_calls": len(tools),
        "tool_errors": None,
        "edit_batches": len(edits),
        "patches": sum(len(tool["arguments"].get("patches", [])) for tool in edits),
        "audit_calls": None,
        "native_failures": summary.get("warnings", []),
    }


def score_turn(turn, requests, applied, audit_root):
    summary = read(turn / "summary.json")
    history_available = (turn / "history.json").exists()
    history = read(turn / "history.json") if history_available else []
    text = summary.get("text")
    if text is None:
        fixture = read(Path(__file__).parent / "fixtures" / (summary["fixture"] + ".json"))
        text = fixture["user_script"][summary["turn"] - 1]
    selected = [
        call
        for call in requests
        if call["metadata"].get("binding", {}).get("block") == summary["block"]
        and call["metadata"].get("binding", {}).get("turn") == summary["turn"]
        and summary["started_ns"] <= call["metadata"]["ingress_ns"] <= summary["finished_ns"]
    ]
    first_prose_seconds = None
    if summary["arm"] == "orb":
        native = score_orb(turn, summary, selected, applied)
        draft = native.pop("draft")
        wall = (summary["done_ns"] - summary["started_ns"]) / 1e9 if summary.get("done_ns") else None
        if summary.get("first_prose_ns"):
            first_prose_seconds = (summary["first_prose_ns"] - summary["started_ns"]) / 1e9
    else:
        native = qualify_tt(turn, summary, applied, audit_root)
        draft = native.pop("draft")
        observed = read_optional(turn / "observed-run.json", {})
        wall = (summary["settled_ns"] - summary["started_ns"]) / 1e9 if summary.get("settled_ns") else None
        native["backend_created_to_terminal_seconds"] = (
            (utc_ns(observed["terminalAt"]) - utc_ns(observed["createdAt"])) / 1e9 if observed.get("terminalAt") else None
        )
        native["poll_observation_delay_seconds"] = (
            (summary["terminal_observed_utc_ns"] - utc_ns(observed["terminalAt"])) / 1e9
            if summary.get("terminal_observed_utc_ns") and observed.get("terminalAt")
            else None
        )
        snapshots = summary.get("prose_snapshots") or ([summary["first_prose"]] if summary.get("first_prose") else [])
        prose = next((shot for shot in snapshots if first_prose_text_matches(shot.get("text"), draft)), None)
        if prose and summary.get("started_utc_ns"):
            first_prose_seconds = (prose["utc_ms"] * 1_000_000 - summary["started_utc_ns"]) / 1e9
        elif snapshots:
            native["observations"].append("first_prose.no_reply_snapshot")
        for call in selected:
            response = native["response_events"].get(call["provider_response_id"])
            preceding = [
                event for event in native["attempt_events"] if event["timestamp_ns"] <= call["metadata"]["utc_unix_ns"]
            ]
            if preceding:
                event = preceding[-1]
                call["stage"] = event["stage"]
                call["invocation_id"], call["round"], call["attempt"] = event["invocationId"], event["round"], event["attempt"]
                call["association_method"] = "persisted_attempt_clock"
                if response:
                    if response["invocationId"] != call["invocation_id"] or response["round"] != call["round"]:
                        native["errors"].append("recording.response_association_mismatch")
                    call["association_method"] = "response_id_and_persisted_attempt_clock"
            else:
                call["stage"] = "unassociated"
                native["errors"].append("recording.unassociated_request")
    errors = list(native["errors"])
    observations = list(native["observations"])
    if not history_available:
        errors.append("history.capture_unavailable")
    for call in selected:
        errors.extend(call["errors"])
        full, matched = includes_history(call["body"], history)
        current_user_present = any(
            row.get("role") == "user" and text in (message_text(row.get("content")) or "")
            for row in call["body"].get("messages", [])[matched[-1] + 1 if matched else 0 :]
        )
        full = full and current_user_present
        call["full_history"] = full
        call["history_indices"] = matched
        if not full:
            errors.append("history.missing_or_changed")
    if not selected:
        errors.append("recording.no_model_calls")
    if summary["arm"] == "tt-handoff" and len({call["tools_sha256"] for call in selected}) > 1:
        errors.append("tools.changed_between_stages")
    if any(call["reasoning_chars"] for call in selected):
        observations.append("response.reasoning_text")
    elif any(call["channel_marker_only"] for call in selected):
        observations.append("response.channel_marker")
    final = (turn / "final.md").read_text() if (turn / "final.md").exists() else ""
    before = contextual_audit(draft, history, text) if draft else None
    after = contextual_audit(final, history, text) if final else None
    if after and after["total_issues"]:
        observations.append("editor.findings_remaining")
    # Attribute each defect to its mechanism: already in the draft (the drafting path) or introduced by editing.
    drafted = set(prose_defects(draft))
    observations.extend(f"{defect}.{'draft' if defect in drafted else 'editing'}" for defect in prose_defects(final))
    unflagged, cursor = [], 0
    for sentence in split_segment_sentences(draft):
        start = draft.find(sentence, cursor)
        end = start + len(sentence)
        cursor = end
        if start >= 0 and before and not any(start < target["end"] and end > target["start"] for target in before["targets"]):
            unflagged.append(sentence)
    available = Counter(split_segment_sentences(final))
    preserved = 0
    for sentence in unflagged:
        if available[sentence] > 0:
            preserved += 1
            available[sentence] -= 1
    stages = {}
    for stage in sorted({call["stage"] for call in selected}):
        calls = [call for call in selected if call["stage"] == stage]
        stages[stage] = {
            "calls": len(calls),
            "model_seconds": sum(call["seconds"] or 0 for call in calls),
            "generated_tokens": sum(call["generated_tokens"] or 0 for call in calls),
            "uncached_tokens": sum(call["uncached_tokens"] for call in calls)
            if all(call["uncached_tokens"] is not None for call in calls)
            else None,
        }
    model_seconds = sum(call["seconds"] or 0 for call in selected)
    result = {
        **summary,
        **native,
        "native_terminal_completed": summary["complete"] if summary["arm"] == "orb" else summary.get("status") == "completed",
        "verified_saved_reply": summary["complete"],
        "qualified": not errors,
        "qualification_errors": sorted(set(errors)),
        "observations": sorted(set(observations)),
        "native_wall_seconds": wall,
        "first_prose_seconds": first_prose_seconds,
        "model_seconds": model_seconds,
        "non_model_seconds": wall - model_seconds if wall is not None else None,
        "attempt_elapsed_seconds": (summary["finished_ns"] - summary["started_ns"]) / 1e9,
        "stages": stages,
        "model_calls": len(selected),
        "generated_tokens": sum(call["generated_tokens"] or 0 for call in selected),
        "reasoning_chars": sum(call["reasoning_chars"] for call in selected),
        "uncached_tokens": sum(call["uncached_tokens"] for call in selected)
        if all(call["uncached_tokens"] is not None for call in selected)
        else None,
        "actual_prompt_tokens": max((call["prompt_tokens"] or 0 for call in selected), default=None),
        "first_prompt_tokens": selected[0]["prompt_tokens"] if selected else None,
        "prose_words": len(final.split()),
        "draft_words": len(draft.split()),
        "initial_findings": before["total_issues"] if before else None,
        "final_findings": after["total_issues"] if after else None,
        "unflagged_sentences": len(unflagged),
        "preserved_unflagged_sentences": preserved,
    }
    result.pop("draft", None)
    result.pop("response_events", None)
    save(turn / "qualification.json", result)
    save(
        turn / "scored-calls.json",
        [{key: value for key, value in call.items() if key not in {"body", "metadata", "content"}} for call in selected],
    )
    if draft:
        (turn / "pre-edit.md").write_text(draft)
        save(turn / "initial-audit.json", before)
    if after:
        save(turn / "final-audit.json", after)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--applied", type=Path, required=True)
    parser.add_argument("--audits", type=Path, required=True)
    args = parser.parse_args()
    calls = [
        wire_facts(path.parent) for path in sorted(args.requests.glob("*/metadata.json")) if read(path).get("method") == "POST"
    ]
    applied = read(args.applied)
    scored = [score_turn(path.parent, calls, applied, args.audits) for path in sorted(args.runs.glob("*/turn-*/summary.json"))]
    save(args.runs / "turns.json", scored)
    print(
        json.dumps(
            {
                "attempts": len(scored),
                "qualified": sum(row["qualified"] for row in scored),
                "native_terminal_completed": sum(row["native_terminal_completed"] for row in scored),
                "verified_saved_replies": sum(row["verified_saved_reply"] for row in scored),
                "errors": Counter(error for row in scored for error in row["qualification_errors"]),
                "observations": Counter(item for row in scored for item in row["observations"]),
            }
        )
    )


if __name__ == "__main__":
    main()
