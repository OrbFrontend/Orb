"""Native Orb benchmark preparation, history insertion, and timestamped API turns."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from scripts.bench.archive import read_text, write_text


def save(path: Path, value):
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def native_cleanup(text: str) -> str:
    """The whitespace TauriTavern's save path removes (`cleanUpMessage`): trailing blanks per line and at both ends."""
    return re.sub(r"[^\S\r\n]+$", "", text, flags=re.MULTILINE).strip()


def saved_difference(final: str, saved: str) -> str | None:
    """``None`` when the saved reply is the final text, ``"cleanup"`` for the native save whitespace, else ``"content"``."""
    if final == saved:
        return None
    return "cleanup" if native_cleanup(final) == native_cleanup(saved) else "content"


def request(client, method, route, payload=None):
    response = client.request(method, route, json=payload)
    response.raise_for_status()
    return response.json()


def sse_data_line(line: str) -> str:
    """SSE removes one framing space, preserving the token's own whitespace."""
    return line[5:].removeprefix(" ")


def decode_event(name: str, raw: str):
    if name == "token":
        return raw.replace("\\n", "\n")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def prepare(base: str, fixture: dict, output: Path):
    snapshot = json.loads(Path(__file__).with_name("defaults.json").read_text())
    with httpx.Client(base_url=base, timeout=120, trust_env=False) as client:
        model_id = httpx.get("http://127.0.0.1:5000/v1/models", trust_env=False).json()["data"][0]["id"]
        settings = {**snapshot["settings"], "user_name": "User", "user_description": fixture["persona"]}
        request(client, "PUT", "/api/settings", settings)
        endpoint = request(client, "PUT", "/api/endpoints/1", snapshot["endpoint"])
        model = request(
            client,
            "PUT",
            f"/api/models/{endpoint['active_model_config_id']}",
            {**snapshot["model_config"], "model_name": model_id},
        )
        for fragment in request(client, "GET", "/api/interactive-fragments"):
            if fragment["field_type"] in snapshot["disabled_fragment_types"]:
                request(client, "PUT", f"/api/interactive-fragments/{fragment['id']}", {"enabled": False})
        applied = request(client, "GET", "/api/settings")
        for key, expected in settings.items():
            if applied.get(key) != expected:
                raise ValueError(f"settings readback differs for {key}: {applied.get(key)!r} != {expected!r}")
        endpoints = request(client, "GET", "/api/endpoints")
        read_endpoint = next(row for row in endpoints if row["id"] == 1)
        for key, expected in snapshot["endpoint"].items():
            if read_endpoint[key] != expected:
                raise ValueError(f"endpoint readback differs for {key}")
        for key, expected in snapshot["model_config"].items():
            if applied.get(key) != expected:
                raise ValueError(f"model readback differs for {key}")
        card = request(client, "POST", "/api/characters", fixture["card"])
        conversation = request(client, "POST", "/api/conversations", {"title": fixture["id"], "character_card_id": card["id"]})
        artifacts = {
            "settings": applied,
            "endpoint": read_endpoint,
            "model_config": model,
            "card": card,
            "conversation": conversation,
            "moods": request(client, "GET", "/api/fragments"),
            "fragments": request(client, "GET", "/api/interactive-fragments"),
        }
        save(output / "applied.json", artifacts)
        return conversation["id"]


async def seed_history(conversation: str, fixture: dict):
    """Call with uvicorn stopped, in the fresh Orb worktree."""
    from backend.database import add_message, get_active_path, set_active_leaf

    if await get_active_path(conversation):
        raise ValueError("refusing to insert a starting history into a nonempty conversation")
    parent = None
    for index, row in enumerate(fixture["history"]):
        parent, _ = await add_message(conversation, row["role"], row["content"], index, parent_id=parent)
    await set_active_leaf(conversation, parent)


def run_turn(base: str, conversation: str, text: str, output: Path, binding: Path, label: dict):
    output.mkdir(parents=True, exist_ok=False)
    save(binding, label)
    with httpx.Client(base_url=base, timeout=120, trust_env=False) as before_client:
        before = request(before_client, "GET", f"/api/conversations/{conversation}/messages")
    save(output / "history.json", [{"role": row["role"], "content": row["content"]} for row in before])
    summary = {**label, "text": text, "started_ns": time.monotonic_ns(), "complete": False}
    event_name, data_lines, draft = "message", [], []
    events = []
    writer_done = False
    try:
        with httpx.Client(base_url=base, timeout=900, trust_env=False) as client:
            with client.stream("POST", f"/api/conversations/{conversation}/send", json={"content": text}) as response:
                response.raise_for_status()
                with (output / "events.jsonl").open("w") as saved:
                    for line in response.iter_lines():
                        arrival = time.monotonic_ns()
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(sse_data_line(line))
                        elif not line and data_lines:
                            raw = "\n".join(data_lines)
                            data = decode_event(event_name, raw)
                            event = {"event": event_name, "data": data, "arrival_ns": arrival}
                            saved.write(json.dumps(event, ensure_ascii=False) + "\n")
                            saved.flush()
                            events.append(event)
                            if event_name == "token" and not writer_done:
                                draft.append(data)
                                summary.setdefault("first_prose_ns", arrival)
                            elif event_name == "writer_done":
                                writer_done = True
                            elif event_name == "done":
                                summary["done_ns"] = arrival
                            event_name, data_lines = "message", []
            summary["stream_end_ns"] = time.monotonic_ns()
            rows = request(client, "GET", f"/api/conversations/{conversation}/messages")
            logs = request(client, "GET", f"/api/conversations/{conversation}/logs")
            save(output / "messages.json", rows)
            save(output / "logs.json", logs)
            # The message-list API deliberately strips writer_draft. Observe the
            # persisted row through the database's public read boundary instead.
            from backend.database import get_active_path

            persisted = asyncio.run(get_active_path(conversation))
            save(output / "database_messages.json", persisted)
            row = persisted[-1] if persisted else {}
            summary["saved_ns"] = time.monotonic_ns()
            warnings = [event for event in events if event["event"] in {"warning", "error"}]
            reasoning = [event for event in events if event["event"].startswith("reasoning") and event["data"]]
            reasoning_columns = {key: value for key, value in row.items() if key.startswith("reasoning_") and value}
            summary.update(
                {
                    "warnings": warnings,
                    "reasoning": reasoning,
                    "reasoning_columns": reasoning_columns,
                    "writer_done": writer_done,
                }
            )
            final = row.get("content", "") if row.get("role") == "assistant" else ""
            summary["complete"] = bool(
                summary.get("done_ns")
                and final
                and writer_done
                and not warnings
                and saved_difference(row.get("writer_draft") or "", final) != "content"
            )
            (output / "final.md").write_text(final)
    except Exception as exc:  # A failed attempt is evidence; record it and keep the sweep going.
        summary["error"] = f"{type(exc).__name__}: {exc}"
    (output / "draft.md").write_text("".join(draft))
    summary["finished_ns"] = time.monotonic_ns()
    save(output / "summary.json", summary)
    print(
        json.dumps(
            {
                "arm": "orb",
                "complete": summary["complete"],
                "seconds": (summary["finished_ns"] - summary["started_ns"]) / 1e9,
                "output": str(output),
            }
        ),
        flush=True,
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "seed", "turn"])
    parser.add_argument("--base", default="http://127.0.0.1:18899")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--conversation")
    parser.add_argument("--binding", type=Path)
    parser.add_argument("--turn", type=int, default=0)
    parser.add_argument("--block", default="ad-hoc-pilot")
    parser.add_argument("--reportable", action="store_true")
    args = parser.parse_args()
    fixture = json.loads(read_text(args.fixture))
    if args.action == "prepare":
        print(prepare(args.base, fixture, args.output))
    elif args.action == "seed":
        asyncio.run(seed_history(args.conversation, fixture))
    else:
        run_turn(
            args.base,
            args.conversation,
            fixture["user_script"][args.turn],
            args.output,
            args.binding,
            {"arm": "orb", "fixture": fixture["id"], "turn": args.turn + 1, "pilot": not args.reportable, "block": args.block},
        )
