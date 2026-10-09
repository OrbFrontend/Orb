"""Draft Bench 2 cards 21-60 with the pinned DeepSeek, one forced `write_card` call per premise, frozen to drafted_cards.json.

    PYTHONPATH=. .venv/bin/python -m scripts.bench.driven.draft_cards [--only ID ...]

The drafts are reviewed by hand before a run; `--only` redrafts the named premises and keeps the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from scripts.bench.cache.orb_driver import save
from scripts.bench.driven.run import SNAPSHOT, api_key

OUTPUT = Path(__file__).with_name("drafted_cards.json")

PREMISES = {
    "solveig": "a lighthouse keeper on a Norwegian skerry, on the evening watch with the user, a relief keeper",
    "harold": "a retired judge in his allotment shed in Leeds on a Sunday afternoon; the user rents the next plot",
    "keiko": "a ceramicist in a Kyoto studio while the kiln cools overnight; the user is her new apprentice",
    "marisol": "the owner of a 24-hour laundromat in Queens at 2 a.m., folding other people's laundry; the user is a regular",
    "dale": "a fire lookout in an Oregon tower on a clear, quiet afternoon; the user is a trainee lookout",
    "esteban": "the oldest member of a chess club in a Buenos Aires cafe; the user is a newcomer who just lost to him",
    "colette": "a Burgundy vigneronne at the long harvest lunch table; the user is a seasonal picker",
    "otis": "a bartender setting up a 1920s Chicago jazz club in the afternoon before opening; the user is the new piano player",
    "aodh": "a monk copying manuscripts in a 12th-century Irish scriptorium; the user is a visiting scribe",
    "oyunaa": "a herder in her ger on the Mongolian steppe in deep winter; the user is a guest sheltering for the night",
    "priya": "a greenhouse technician in a Mars habitat at the end of a sol; the user is a newly arrived colonist",
    "tavita": "the cook in the galley of a deep-sea research vessel weeks into a survey; the user is a visiting scientist",
    "gloria": "a branch librarian in Detroit reshelving after the library has closed; the user is a friend keeping her company",
    "folake": "a barber in a busy Lagos shop on a Saturday; the user is waiting their turn in the chair",
    "callum": "a warehouseman at an Islay whisky distillery, drawing samples from casks; the user is a visiting writer",
    "joost": "the mechanic who runs an Amsterdam bicycle repair co-op; the user volunteers there on weekends",
    "lucio": "a glassblower in a Murano workshop; the user is a visitor watching from the bench by the furnace",
    "ruth": "a bush pilot in her Alaskan hangar on a fogged-in day with no flying; the user is her passenger for the week",
    "ambrose": "a Victorian clockmaker in his London shop on a winter evening; the user is his apprentice",
    "nadine": "a drama teacher in an empty school auditorium after rehearsal; the user is a parent volunteer painting sets",
    "zahra": "the host of a riad in Marrakesh serving mint tea on the roof at dusk; the user is a guest staying the week",
    "mere": "a shearer in a New Zealand woolshed at smoko; the user is a new rouseabout",
    "takeshi": "the cook of a Tokyo sumo stable, cleaning up after the wrestlers have eaten and gone to rest; the user is a journalist allowed to watch",
    "earl": "a Texas pitmaster tending an overnight brisket; the user is a friend keeping him company by the smoker",
    "ingvild": "the station doctor at an Antarctic base during winter-over, in the lounge; the user is a fellow winterer",
    "vaclav": "a puppet maker in his Prague workshop; the user is a student learning to carve",
    "maite": "a cheesemaker at a Pyrenees dairy, turning wheels in the cave; the user is a summer helper",
    "dominic": "a tattoo artist in a Brooklyn shop between appointments; the user is his friend who keeps the books",
    "aino": "a forest ranger at a Finnish wilderness cabin after the sauna; the user is a hiker sharing the cabin",
    "hattie": "the keeper of an 1880s Montana general store on a slow afternoon; the user is a homesteader buying supplies",
    "rogelio": "a Havana mechanic restoring an old Chevrolet in his garage; the user is his nephew visiting from abroad",
    "minji": "a clerk at a Seoul convenience store on the night shift; the user eats instant noodles at the window counter",
    "beat": "the warden of a Swiss mountain hut after the day's climbers have gone to bed; the user is a guest who can't sleep",
    "oleander": "a maintenance technician in an orbital station's repair bay during a quiet shift; the user is a new crew member",
    "fiona": "the curator of a Victorian glasshouse in Edinburgh's botanic garden; the user is a volunteer watering the ferns",
    "jomo": "a ranger at a Kenyan wildlife camp at dusk; the user is a researcher on their first evening in camp",
    "thao": "a tailor at a street-side stall in Hanoi; the user is a customer waiting for a hem to be finished",
    "gerald": "the owner of a Newfoundland fish-and-chips shop in the off-season; the user is the only customer",
    "rosalind": "a beachcomber and driftwood sculptor in a Cornish cove; the user is a holidaymaker sitting on the rocks",
    "anselm": "a church organ builder voicing pipes in an empty Leipzig church; the user is his assistant",
}

INSTRUCTIONS = """Write a safe-for-work roleplay character card and the start of a chat for this premise: {name} is {premise}.

The chat is a calm, complete scene. Nothing may already be on its way or scheduled: no approaching vehicles, people, animals or
weather, no expected arrivals or departures, no deadlines, alarms or plans for later. Whatever happens next is left
entirely open.

The character's messages narrate in the past tense, the character in the third person and the user as "you". The
user's own lines are written by the user, as "I". The opener and the reply are each
three to five paragraphs, about 200-300 words, with some dialogue. The user line is one plain sentence in the first
person, present tense, that asks or does something small. The final user turn is one plain sentence in the first person,
present tense, describing a small passive action such as eating, sitting down, watching or listening. It asks nothing and
says nothing about quiet, silence or not speaking.

The description is three or four sentences in the third person, never "you", and its last sentence
starts with "The user is". The scenario is one sentence naming the
time and place. The lorebook has four entries about places, people and things in this world, each with three to five
lowercase keywords. The inventory is exactly two items, one carried by the character and one by the user, each written
as "<first name or User>: <item>"."""

TOOL = {
    "type": "function",
    "function": {
        "name": "write_card",
        "description": "Write the character card and the start of the chat.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "personality": {"type": "string", "description": "A comma-separated list of traits."},
                "scenario": {"type": "string"},
                "book_name": {"type": "string", "description": "The lorebook's name, usually the place."},
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "keys": {"type": "array", "items": {"type": "string"}},
                            "content": {"type": "string"},
                        },
                        "required": ["name", "keys", "content"],
                    },
                },
                "inventory": {"type": "array", "items": {"type": "string"}},
                "opener": {"type": "string", "description": "The character's opening message."},
                "user": {"type": "string", "description": "The user's first message."},
                "reply": {"type": "string", "description": "The character's reply."},
                "final": {"type": "string", "description": "The user's open, passive final turn."},
            },
            "required": [
                "name",
                "description",
                "personality",
                "scenario",
                "book_name",
                "entries",
                "inventory",
                "opener",
                "user",
                "reply",
                "final",
            ],
        },
    },
}


def clean(value):
    """Turn an escaped newline the model wrote as two characters back into a newline."""
    if isinstance(value, str):
        return value.replace("\\n", "\n")
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    return value


def problems(card: dict) -> list[str]:
    """Shape checks the corpus tests also need; anything else is for the hand review."""
    found = []
    if len(card.get("entries") or []) < 3 or not all(entry.get("keys") for entry in card.get("entries") or []):
        found.append("entries")
    if len(card.get("inventory") or []) != 2:
        found.append("inventory")
    for field in ("opener", "reply"):
        if len((card.get(field) or "").split()) < 120:
            found.append(f"{field} too short")
    description = card.get("description") or ""
    if re.search(r"\byou(r|rs)?\b", description, re.I) or "The user is" not in description:
        found.append("description")
    if any('\\"' in str(value) for value in card.values()):
        found.append("escaped quotes")
    final = card.get("final") or ""
    if "?" in final or not final.startswith("I "):
        found.append("final turn")
    return found


async def draft(http: httpx.AsyncClient, key: str, premise_id: str, premise: str) -> dict:
    config = SNAPSHOT["transports"]["deepseek"]["model_config"]
    body = {
        "model": config["model_name"],
        "messages": [{"role": "user", "content": INSTRUCTIONS.format(name=premise_id.title(), premise=premise)}],
        "tools": [TOOL],
        "tool_choice": {"type": "function", "function": {"name": "write_card"}},
        "temperature": 0.7,
        "max_tokens": 4096,
        "reasoning": {"enabled": False},
        **json.loads(config["extra_body"]),
    }
    response = await http.post(
        "https://openrouter.ai/api/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}
    )
    response.raise_for_status()
    data = response.json()
    card = clean(json.loads(data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]))
    return {"premise_id": premise_id, "premise": premise, "provider": data.get("provider"), "card": card}


async def main_async(only: list[str]):
    previous = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {"cards": []}
    kept = [row for row in previous["cards"] if only and row["premise_id"] not in only]
    todo = {key: value for key, value in PREMISES.items() if not only or key in only}
    key = api_key(SNAPSHOT["transports"]["deepseek"]["upstream"])
    limit = asyncio.Semaphore(10)

    async def one(premise_id: str, premise: str) -> dict:
        async with limit:
            return await draft(http, key, premise_id, premise)

    async with httpx.AsyncClient(timeout=300) as http:
        drafted = await asyncio.gather(*(one(premise_id, premise) for premise_id, premise in todo.items()))
    rows = sorted([*kept, *drafted], key=lambda row: list(PREMISES).index(row["premise_id"]))
    for row in rows:
        row["problems"] = problems(row["card"])
    model = SNAPSHOT["transports"]["deepseek"]["model_config"]["model_name"]
    save(OUTPUT, {"model": model, "drafted_utc_ns": time.time_ns(), "cards": rows})
    for row in rows:
        if row["problems"]:
            print(row["premise_id"], row["problems"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=[])
    asyncio.run(main_async(parser.parse_args().only))


if __name__ == "__main__":
    main()
