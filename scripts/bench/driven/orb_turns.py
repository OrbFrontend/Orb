"""Bench 2 Orb side, run inside the fresh worktree: apply bench.json, create the conversations, seed them, run one arm's turn."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from scripts.bench.cache.orb_driver import request, run_turn, save

SNAPSHOT = json.loads(Path(__file__).with_name("bench.json").read_text())


def client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=120, trust_env=False)


def check(label: str, actual: dict, expected: dict):
    for key, value in expected.items():
        if actual.get(key) != value:
            raise ValueError(f"{label} readback differs for {key}: {actual.get(key)!r} != {value!r}")


def prepare(base: str, plan: dict, output: Path):
    persona = {item["persona"] for item in plan["contexts"].values()}
    if len(persona) != 1:
        raise ValueError("the persona lives in global settings, so every context must share one")
    transport = SNAPSHOT["transports"][plan["transport"]]
    model_config = dict(transport["model_config"])
    if "model_name" not in model_config:
        model_config["model_name"] = httpx.get("http://127.0.0.1:5000/v1/models", trust_env=False).json()["data"][0]["id"]
    with client(base) as http:
        settings = {**SNAPSHOT["settings"], "user_name": "User", "user_description": persona.pop()}
        request(http, "PUT", "/api/settings", settings)
        # A hosted model's key comes from the runner's environment; the readback below hides it.
        key = {"api_key": os.environ["BENCH_API_KEY"]} if "provider" in transport else {}
        endpoint = request(http, "PUT", "/api/endpoints/1", {**transport["endpoint"], **key})
        model = request(http, "PUT", f"/api/models/{endpoint['active_model_config_id']}", model_config)
        for fragment in request(http, "GET", "/api/interactive-fragments"):
            if fragment["id"] in SNAPSHOT["enabled_fragments"]:
                request(http, "PUT", f"/api/interactive-fragments/{fragment['id']}", {"enabled": True})
            elif fragment["field_type"] in SNAPSHOT["disabled_fragment_types"]:
                request(http, "PUT", f"/api/interactive-fragments/{fragment['id']}", {"enabled": False})
        applied = request(http, "GET", "/api/settings")
        check("settings", applied, settings)
        check("model", applied, model_config)
        check("endpoint", next(row for row in request(http, "GET", "/api/endpoints") if row["id"] == 1), transport["endpoint"])
        fragments = request(http, "GET", "/api/interactive-fragments")
        for fragment in fragments:
            wanted = fragment["id"] in SNAPSHOT["enabled_fragments"] or (
                fragment["field_type"] not in SNAPSHOT["disabled_fragment_types"] and bool(fragment["enabled"])
            )
            if bool(fragment["enabled"]) != wanted:
                raise ValueError(f"fragment {fragment['id']} enabled={fragment['enabled']}")
        missing = set(SNAPSHOT["enabled_fragments"]) - {fragment["id"] for fragment in fragments if fragment["enabled"]}
        if missing:
            raise ValueError(f"fragments not enabled: {sorted(missing)}")
        cards, conversations = {}, {}
        for context in plan["contexts"].values():
            if context["card_id"] not in cards:
                cards[context["card_id"]] = request(http, "POST", "/api/characters", context["card"])
        for key, turn in plan["turns"].items():
            context = plan["contexts"][turn["context"]]
            card = cards[context["card_id"]]
            conversation = request(http, "POST", "/api/conversations", {"title": key, "character_card_id": card["id"]})
            worlds = request(http, "GET", f"/api/conversations/{conversation['id']}/worlds")["world_ids"]
            if not worlds or worlds != [card["world_id"]]:
                raise ValueError(f"{key}: the card's lorebook is not the conversation's only World: {worlds}")
            conversations[key] = conversation["id"]
        save(
            output / "applied.json",
            {
                "settings": applied,
                "endpoint": endpoint,
                "model_config": model,
                "cards": cards,
                "worlds": {
                    card_id: request(http, "GET", f"/api/worlds/{card['world_id']}/entries") for card_id, card in cards.items()
                },
                "moods": request(http, "GET", "/api/fragments"),
                "fragments": fragments,
            },
        )
        save(output / "conversations.json", conversations)


async def seed(plan: dict, conversations: dict):
    """Call with uvicorn stopped, in the fresh Orb worktree."""
    from backend.database import add_message, get_active_path, set_active_leaf

    for key, cid in conversations.items():
        if await get_active_path(cid):
            raise ValueError(f"refusing to insert a starting history into nonempty conversation {key}")
        parent = None
        for index, row in enumerate(plan["contexts"][plan["turns"][key]["context"]]["history"]):
            parent, _ = await add_message(cid, row["role"], row["content"], index, parent_id=parent)
        await set_active_leaf(cid, parent)


def inventory(base: str, plan: dict, conversations: dict, output: Path):
    states = {}
    with client(base) as http:
        for key, cid in conversations.items():
            for text in plan["contexts"][plan["turns"][key]["context"]]["inventory"]:
                request(
                    http, "POST", f"/api/conversations/{cid}/state", {"fragment_id": "inventory", "op": "add", "text": text}
                )
            states[key] = request(http, "GET", f"/api/conversations/{cid}/state")
    save(output / "starting-state.json", states)


def reset(base: str, plan: dict, conversations: dict, output: Path, key: str):
    """Before a retried attempt: drop what the failed attempt added, then confirm history and state are as seeded."""
    cid = conversations[key]
    history = plan["contexts"][plan["turns"][key]["context"]]["history"]
    with client(base) as http:
        rows = request(http, "GET", f"/api/conversations/{cid}/messages")
        if len(rows) > len(history):
            request(http, "DELETE", f"/api/conversations/{cid}/messages/{rows[len(history)]['id']}")
            rows = request(http, "GET", f"/api/conversations/{cid}/messages")
        if [(row["role"], row["content"]) for row in rows] != [(row["role"], row["content"]) for row in history]:
            raise ValueError(f"{key}: the conversation is not back at its seeded history")
        state = request(http, "GET", f"/api/conversations/{cid}/state")
    starting = json.loads((output / "starting-state.json").read_text())[key]
    if inventory_texts(state) != inventory_texts(starting):
        raise ValueError(f"{key}: the inventory is not back at its starting state")


def inventory_texts(state: dict) -> list[str]:
    return sorted(
        entry["text"]
        for fragment in state["fragments"]
        if fragment["fragment_id"] == "inventory"
        for entry in fragment["entries"]
    )


def set_arm(base: str, director: bool) -> dict:
    tools = {**SNAPSHOT["settings"]["enabled_tools"], "direct_scene": director}
    with client(base) as http:
        request(http, "PUT", "/api/settings", {"enabled_tools": tools})
        applied = request(http, "GET", "/api/settings")
    check("settings", applied, {**SNAPSHOT["settings"], "enabled_tools": tools})
    return applied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "seed", "inventory", "reset", "turn"])
    parser.add_argument("--base", default="http://127.0.0.1:18899")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--key")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.action == "prepare":
        prepare(args.base, plan, args.run)
        return
    conversations = json.loads((args.run / "conversations.json").read_text())
    if args.action == "seed":
        asyncio.run(seed(plan, conversations))
    elif args.action == "inventory":
        inventory(args.base, plan, conversations, args.run)
    elif args.action == "reset":
        reset(args.base, plan, conversations, args.run, args.key)
    else:
        turn = plan["turns"][args.key]
        applied = set_arm(args.base, turn["director"])
        run_turn(
            args.base,
            conversations[args.key],
            plan["contexts"][turn["context"]]["user_turn"],
            args.run / "turns" / args.key,
            args.run / "recorder-binding.json",
            {
                **turn,
                "key": args.key,
                "pilot": plan["pilot"],
                "enabled_tools": applied["enabled_tools"],
                "editor_audit_toggles": applied["editor_audit_toggles"],
            },
        )


if __name__ == "__main__":
    main()
