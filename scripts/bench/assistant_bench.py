"""Bench the panel assistant on a real model: tool choice, arguments, retries, calls and time, per protocol.

    NANOGPT_API_KEY=... python -m scripts.bench.assistant_bench --endpoint https://nano-gpt.com/api/v1 \\
        --model google/gemma-4-31b-it --key-env NANOGPT_API_KEY --repeats 5 --workers 6 --output OUT
    python -m scripts.bench.assistant_bench --report OUT

Step 7 of docs/plans/assistant-chat-tightening.md. Every request runs in-process through the real ``run_assistant`` and the
real tools, on a fresh copy of one seeded library, so a write in one request never reaches another. The media tools keep their
real argument model and target checks, but their render is a stub that saves nothing. A handoff ends the run as it does in
the app; nothing starts in a chat.

Arms (``--arms``):

- ``two-default``: the default two-call protocol (a step call, then an argument call), with the endpoint as Orb profiles
  it and everything the client learns kept from one run to the next within a worker, as a session keeps it. On NanoGPT
  that starts as strict ``response_format`` with no tool list on the wire, until a reply demotes the model to native tools.
- ``two-native``: the same protocol with the structured-output profile demoted from the start, so every forced call goes
  as tools + tool_choice.
- ``single-native``: one call per step over every tool's schema plus ``reply``, ``tool_choice: required``. It needs native
  tools, so it is refused on an endpoint that sends none.

Each result records the wire mode the arm actually ran in, and any demotion the client learned mid-run.

Per request: the first tool chosen against the request's acceptable set, whether every argument call was usable and valid
on its first attempt, unusable replies retried, invalid arguments refused, tool refusals, model calls, tokens, wall time,
and whether the request's own outcome check passed. Results append to ``runs.jsonl`` as they finish; a rerun resumes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ARMS = {
    # name: (single_call, native tools)
    "two-default": (False, False),
    "two-native": (False, True),
    "single-native": (True, True),
}
JOB_SECONDS = 900
PROVIDER_RETRIES = 3
PROVIDER_BACKOFF_SECONDS = 30
BOOTSTRAP_SAMPLES = 4000


# --------------------------------------------------------------------------------------------------------------------------
# The library every request runs against


_LAST_REPLY = (
    "Mara sets teh lantern on the crate and leans close enough that you can smell the tar on her coat. "
    '"You\'ll recieve the ledger at the north quay, not before. Wait untill the bells stop, then come alone."'
)

_IVO = (
    "Ivo Brannick keeps the import ledgers at the Customs House and has done so for eleven years. He is thin, careful and "
    "permanently cold, and he wears fingerless gloves even in summer. Every morning he walks from his rooms above the "
    "bakery on Rope Street to the Customs House, counting the bollards as he goes. He takes small bribes and records every "
    "one of them in a second ledger he keeps in a biscuit tin. Nobody at the Customs House trusts him, which suits him: a "
    "man nobody trusts is a man nobody confides in, and so a man nobody suspects."
)

_WREN = (
    "Wren is a courier who grew up in the Harbour and knows every alley by smell. She is quick, mouthy and loyal to whoever "
    "paid last."
)

_NOTES = (
    "Act one: Mara agrees to fence the stolen customs ledger, but only if the buyer meets her at the north quay. "
    "Act two: Ivo notices the missing ledger and quietly opens his biscuit tin, deciding whether to report it or sell what he "
    "knows. Act three: the bells at the harbour stop early, the meeting is moved, and Wren has to choose who to warn. "
) * 4

_KESTREL_TOPICS = [
    ("childhood", "the salt flats north of the estuary", "a cracked brass compass", "her grandmother's boat"),
    ("apprenticeship", "the rope walk behind the chandlery", "a borrowed marlinspike", "the rigger who taught her knots"),
    ("first command", "the shoals off Port Vell", "a hand-drawn chart", "a crew of six who never paid her back"),
    ("smuggling years", "the fog banks past the lighthouse", "a false-bottomed water cask", "a revenue cutter named Patience"),
    ("the wreck", "the reef they call the Teeth", "a lantern with a green lens", "the night the mainmast split"),
    ("the long silence", "a boarding house on Kiln Lane", "a stack of unsent letters", "a landlady who asked no questions"),
    ("return to the water", "the ferry slip at Hollow Point", "a secondhand oilskin", "a skiff she rebuilt plank by plank"),
    ("present work", "the night crossings between the islands", "a ledger written in her own cipher", "Mara, who pays well"),
]


def _kestrel() -> tuple[str, str]:
    """A long card description, about 7,600 characters, that names Port Vell exactly once, and its last paragraph."""
    paragraphs = []
    for index, (era, place, thing, person) in enumerate(_KESTREL_TOPICS):
        paragraphs.append(
            f"On her {era}: Kestrel remembers {place} more clearly than any face, and she will talk about it for as long "
            f"as someone keeps her cup full. She still carries {thing}, and she will not say why, though anyone who has "
            f"sailed with her knows it has something to do with {person}. In those years she learned to read weather "
            f"from the colour of the gulls, to keep two sets of books, and to leave a harbour before the tide had fully "
            f"turned. She says the {era} taught her patience; the people who knew her then say it taught her to lie "
            f"well. Both are true, and she likes it that way. Chapter {index + 1} of her life ended, as most of them did, "
            f"with an unpaid debt, a closed door and a boat that needed more work than she could afford. Ask her about it "
            f"and she will pour herself another measure, look at the window, and change the subject to the price of rope."
        )
    paragraphs.append(
        "Today she sails alone. She charges by the crossing, never by the cargo, and she never asks what is in the crates. "
        "If you want her to take you somewhere, bring cash, bring your own lantern, and do not ask her about the Teeth."
    )
    return "\n\n".join(paragraphs), paragraphs[-1]


async def seed_library(path: Path) -> dict[str, Any]:
    """Build the fresh-install database at *path* and fill it through the app's own routes; returns the ids a request names."""
    import httpx

    import backend.database.connection as db_connection
    from backend.api.assistant import HOST
    from backend.database import add_message, get_conversation, get_settings, init_db, resolve_cast
    from backend.features.assistant.tools import switch_summary
    from backend.main import app

    db_connection.DB_PATH = str(path)
    await init_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as client:

        async def post(url: str, body: Mapping[str, Any]) -> Any:
            response = await client.post(url, json=body)
            response.raise_for_status()
            return response.json()

        async def put(url: str, body: Mapping[str, Any]) -> Any:
            response = await client.put(url, json=body)
            response.raise_for_status()
            return response.json()

        kestrel_text, kestrel_last = _kestrel()
        mara = (
            await post(
                "/api/characters",
                {
                    "name": "Mara",
                    "description": "A harbour fence who deals in other people's paperwork.",
                    "personality": "Wry, patient, and allergic to debt.",
                    "tags": ["Noir"],
                },
            )
        )["id"]
        ivo = (await post("/api/characters", {"name": "Ivo", "description": _IVO}))["id"]
        kestrel = (await post("/api/characters", {"name": "Kestrel", "description": kestrel_text}))["id"]
        chat = (await post("/api/conversations", {"character_card_id": mara}))["id"]
        await put(f"/api/conversations/{chat}", {"title": "Mara at the quay"})
        group = (
            await post(
                "/api/conversations",
                {"kind": "group", "members": [{"character_card_id": mara}, {"character_card_id": ivo}]},
            )
        )["id"]
        harbour = (await post("/api/worlds", {"name": "Harbour"}))["id"]
        await post(
            f"/api/worlds/{harbour}/entries",
            {"name": "Tide tax", "content": "Paid at dusk, in copper only.", "keywords": ["tax"]},
        )
        await post(
            f"/api/worlds/{harbour}/entries",
            {"name": "Lighthouse", "content": "Dark since the storm nine winters ago.", "keywords": ["lighthouse"]},
        )
        inland = (await post("/api/worlds", {"name": "Inland"}))["id"]
        await post(f"/api/worlds/{inland}/entries", {"name": "Salt road", "content": "Closed for the winter.", "keywords": []})
        await put(f"/api/conversations/{chat}/worlds/{harbour}", {"enabled": True})
        wren = (await post("/api/user-personas", {"name": "Wren", "description": _WREN}))["id"]
        document = (await post("/api/documents", {"title": "Plot notes"}))["id"]
        await put(f"/api/documents/{document}", {"content": _NOTES, "expected_revision": 0})

    from backend.database.connection import get_db

    async with get_db() as db:
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (chat,))
        await db.commit()
    lines = [
        ("user", 'I find Mara behind the chandlery. "You said you had something for me."'),
        ("assistant", "Mara doesn't look up from her knife. \"I said I might. Depends who's asking, and who's paying.\""),
        ("user", '"Ivo sent me. He says the ledger\'s gone missing from the Customs House."'),
        ("assistant", 'She laughs once, without warmth. "Ivo loses things the way tides lose sand. On purpose."'),
        ("user", '"Can you get it back?"'),
        ("assistant", '"I can get anything back. The question is what it costs you, and whether you can carry it."'),
        ("user", '"Name the price."'),
        ("assistant", _LAST_REPLY),
    ]
    parent = None
    ids = []
    for index, (role, text) in enumerate(lines):
        parent, _ = await add_message(chat, role, text, index, parent_id=parent, advance_leaf=True)
        ids.append(parent)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as client:
        crossing = (await client.post("/api/conversations", json={"character_card_id": kestrel})).json()["id"]
        # One inventory entry on the solo chat's kept branch, as the State tab would add it.
        (await client.put("/api/interactive-fragments/inventory", json={"enabled": True})).raise_for_status()
        added = await client.post(
            f"/api/conversations/{chat}/state", json={"fragment_id": "inventory", "op": "add", "text": "Mara: a brass compass"}
        )
        added.raise_for_status()

    # A chat longer than one page of read_messages, whose one fact sits near its start.
    async with get_db() as db:
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (crossing,))
        await db.commit()
    parent = None
    for index in range(30):
        if index == 1:
            text = '"The fare is three silver crowns, paid before we cast off," Kestrel says, holding out her palm.'
        elif index % 2:
            text = f"Kestrel trims the sail and watches the swell. ({index})"
        else:
            text = f"I watch the dark water slide past the hull. ({index})"
        role = "user" if index % 2 == 0 else "assistant"
        parent, _ = await add_message(crossing, role, text, index, parent_id=parent, advance_leaf=True)

    # The group has spoken once, so its next turn needs a speaker.
    members = (await resolve_cast(await get_conversation(group))).members
    ivo_member = next(member.member_id for member in members if member.card_id == ivo)
    await add_message(group, "assistant", "Mara eyes the clerk across the table.", 0, advance_leaf=True)
    summary = switch_summary(await get_settings(), HOST)
    return {
        "mara": mara,
        "ivo": ivo,
        "kestrel": kestrel,
        "kestrel_text": kestrel_text,
        "kestrel_last": kestrel_last,
        "chat": chat,
        "crossing": crossing,
        "group": group,
        "ivo_member": ivo_member,
        "harbour": harbour,
        "inland": inland,
        "wren": wren,
        "document": document,
        "last_reply": ids[-1],
        "messages": len(ids),
        "switches": summary,
    }


# --------------------------------------------------------------------------------------------------------------------------
# What one run produced, and the checks a request makes of it


@dataclass
class Outcome:
    events: list[dict[str, Any]]
    seed: dict[str, Any]

    @property
    def reply(self) -> str:
        return " ".join(e["text"] for e in self.events if e["type"] == "assistant_message" and e.get("final"))

    def ran(self, tool: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == "tool_result" and e["tool"] == tool and e["ok"]]

    def fields(self, kind: str, name: str) -> list[dict[str, Any]]:
        """The latest preview of each proposal on a *kind* target, for its field *name*."""
        latest: dict[str, dict[str, Any]] = {}
        for event in self.events:
            if event["type"] == "confirm":
                latest[event["proposal"]["proposal_id"]] = event["proposal"]
        return [
            {**item, "target": proposal["target"]}
            for proposal in latest.values()
            if proposal["kind"] == kind
            for item in proposal["fields"]
            if item["field"] == name
        ]

    def handoffs(self, tool: str) -> list[dict[str, Any]]:
        return [e["handoff"] for e in self.events if e["type"] == "handoff" and e["tool"] == tool]


Check = Callable[[Outcome], Awaitable[bool]]


def _says(*words: str) -> Check:
    async def check(out: Outcome) -> bool:
        text = out.reply.casefold()
        return all(word.casefold() in text for word in words)

    return check


def _matches(pattern: str) -> Check:
    async def check(out: Outcome) -> bool:
        return re.search(pattern, out.reply, re.IGNORECASE) is not None

    return check


def _all(*checks: Check) -> Check:
    async def check(out: Outcome) -> bool:
        return all([await item(out) for item in checks])

    return check


def _ran(tool: str) -> Check:
    async def check(out: Outcome) -> bool:
        return bool(out.ran(tool))

    return check


def _proposed(kind: str, name: str, test: Callable[[str, str, Outcome], bool]) -> Check:
    """Exactly one proposal for that field, and its accumulated text passes *test(before, after, outcome)*."""

    async def check(out: Outcome) -> bool:
        found = out.fields(kind, name)
        return len(found) == 1 and test(found[0]["before"], found[0]["after"], out)

    return check


def _flat(text: str) -> str:
    return " ".join(text.split())


async def _setting(key: str) -> Any:
    from backend.database import get_settings

    return (await get_settings()).get(key)


def _db(probe: Callable[[Outcome], Awaitable[bool]]) -> Check:
    return probe


async def _director_off(out: Outcome) -> bool:
    from backend.api.assistant import HOST
    from backend.database import get_settings
    from backend.features.assistant.tools import switch_summary

    value = switch_summary(await get_settings(), HOST)["director"]
    return not (value.get("stored") if isinstance(value, Mapping) else value)


async def _workflows_off(out: Outcome) -> bool:
    return not await _setting("workflows_globally_enabled")


async def _world_named(out: Outcome) -> bool:
    from backend.database import get_worlds

    return any(world["name"].strip().casefold() == "saltmarsh" for world in await get_worlds())


async def _night_market(out: Outcome) -> bool:
    from backend.database import get_lorebook_entries

    for entry in await get_lorebook_entries(out.seed["harbour"]):
        if entry["name"].strip().casefold() == "night market":
            keywords = entry.get("keywords")
            words = {str(word).casefold() for word in (json.loads(keywords) if isinstance(keywords, str) else keywords or [])}
            return {"market", "night"} <= words and "moonrise" in str(entry["content"]).casefold()
    return False


async def _persona_named(out: Outcome) -> bool:
    from backend.database import get_user_personas

    return any(persona["name"].strip().casefold() == "corin" for persona in await get_user_personas())


async def _character_named(out: Outcome) -> bool:
    from backend.database import list_character_cards

    cards = await list_character_cards()
    rows = cards["items"] if isinstance(cards, Mapping) else cards
    return any(str(card["name"]).strip().casefold() == "tamsin" for card in rows)


async def _inland_attached(out: Outcome) -> bool:
    from backend.database import get_effective_world_ids

    worlds = await get_effective_world_ids(out.seed["chat"])
    return out.seed["inland"] in worlds and out.seed["harbour"] in worlds


async def _checkpointed(out: Outcome) -> bool:
    from backend.database.connection import get_db

    async with get_db() as db, db.execute("SELECT title FROM conversations") as cursor:
        titles = [row[0] for row in await cursor.fetchall()]
    return any("before the deal" in str(title).casefold() for title in titles)


async def _lighthouse_off(out: Outcome) -> bool:
    from backend.database import get_lorebook_entries

    entries = {entry["name"]: entry for entry in await get_lorebook_entries(out.seed["harbour"])}
    return not entries["Lighthouse"]["enabled"] and bool(entries["Tide tax"]["enabled"])


async def _ivo_chat(out: Outcome) -> bool:
    from backend.database.connection import get_db

    query = "SELECT COUNT(*) FROM conversations WHERE character_card_id = ? AND kind = 'solo'"
    async with get_db() as db, db.execute(query, (out.seed["ivo"],)) as cursor:
        return (await cursor.fetchone())[0] == 1


async def _renamed(out: Outcome) -> bool:
    from backend.database import get_conversation

    conv = await get_conversation(out.seed["chat"])
    return conv is not None and conv["title"].strip().casefold() == "ledger night"


async def _length_150(out: Outcome) -> bool:
    return await _setting("length_guard_max_words") == 150


async def _phrase_banned(out: Outcome) -> bool:
    from backend.database import get_phrase_bank_rows

    wanted = "a shiver ran down her spine"
    return any(wanted in str(variant).casefold() for row in await get_phrase_bank_rows() for variant in row["variants"])


async def _mood_terse(out: Outcome) -> bool:
    from backend.database import get_mood_fragments

    moods = [row for row in await get_mood_fragments() if row["label"].strip().casefold() == "terse"]
    return len(moods) == 1 and bool(moods[0]["enabled"]) and bool(moods[0]["prompt_text"].strip())


async def _tense_off(out: Outcome) -> bool:
    from backend.database import get_mood_fragments

    moods = {row["label"]: row for row in await get_mood_fragments()}
    return not moods["Tense"]["enabled"] and bool(moods["Talkative"]["enabled"])


async def _weather_decision(out: Outcome) -> bool:
    from backend.core import parse_decision_definition
    from backend.database import get_interactive_fragments

    return any(
        row["decision_type"] == "noul" and parse_decision_definition(row) is not None
        for row in await get_interactive_fragments()
        if "weather" in str(row["decision_instructions"] or "").casefold()
    )


async def _inland_global(out: Outcome) -> bool:
    from backend.database import get_world

    world = await get_world(out.seed["inland"])
    return world is not None and bool(world["is_global"])


async def _opened_chat(out: Outcome) -> bool:
    return any(handoff.get("conversation_id") == out.seed["chat"] for handoff in out.handoffs("open_chat"))


async def _ivo_speaks(out: Outcome) -> bool:
    return any(handoff.get("speaker_member_id") == out.seed["ivo_member"] for handoff in out.handoffs("continue_chat"))


def _tagged(before: Any, after: Any, out: Outcome) -> bool:
    return sorted(after) == ["Harbour", "Noir"]


def _media(tool: str) -> Check:
    async def check(out: Outcome) -> bool:
        return any(result["ids"].get("message_id") == out.seed["last_reply"] for result in out.ran(tool))

    return check


def _handed(tool: str) -> Check:
    async def check(out: Outcome) -> bool:
        return any(handoff.get("message_id") == out.seed["last_reply"] for handoff in out.handoffs(tool))

    return check


def _either(*checks: Check) -> Check:
    async def check(out: Outcome) -> bool:
        return any([await item(out) for item in checks])

    return check


def _typos_fixed(before: str, after: str, out: Outcome) -> bool:
    return (
        all(word in after for word in ("the lantern", "receive", "until"))
        and not any(word in after for word in ("teh ", "recieve", "untill"))
        and len(after) >= len(before) - 6
    )


def _tollhouse(before: str, after: str, out: Outcome) -> bool:
    return (
        after.count("the Tollhouse") == 3
        and "Customs House" not in after
        and _flat(after.replace("Tollhouse", "Customs House")) == _flat(before)
    )


def _port_sable(before: str, after: str, out: Outcome) -> bool:
    return _flat(after) == _flat(before.replace("Port Vell", "Port Sable"))


def _last_paragraph_gone(before: str, after: str, out: Outcome) -> bool:
    return _flat(after) == _flat(before.replace(out.seed["kestrel_last"], ""))


def _shorter_reply(before: str, after: str, out: Outcome) -> bool:
    return 0 < len(after) < len(before)


@dataclass(frozen=True)
class Request:
    id: str
    kind: str
    view: Mapping[str, str]
    text: str
    first: frozenset[str]
    check: Check


def requests() -> list[Request]:
    """Forty-two fixed requests; a view's id value names a seed key."""
    chat = {"mode": "chat", "conversation_id": "chat"}
    group = {"mode": "chat", "conversation_id": "group"}
    library = {"mode": "library"}

    def r(id_: str, kind: str, view: Mapping[str, str], text: str, first: set[str], check: Check) -> Request:
        return Request(id_, kind, view, text, frozenset(first), check)

    return [
        # Reads
        r(
            "read_characters",
            "read",
            library,
            "Which characters do I have?",
            {"list_characters", "query_library"},
            _says("Mara", "Ivo", "Kestrel"),
        ),
        r(
            "read_lore",
            "read",
            library,
            "When is the tide tax paid, according to the Harbour lorebook?",
            {"list_worlds", "query_library"},
            _says("dusk"),
        ),
        r("read_last_reply", "read", chat, "What did Mara say in her last reply?", {"read_messages"}, _says("ledger")),
        r(
            "read_persona",
            "read",
            library,
            "What is my persona Wren like?",
            {"list_personas", "query_library"},
            _says("courier"),
        ),
        r(
            "read_document",
            "read",
            {"mode": "document", "document_id": "document"},
            "Summarize this document in one sentence.",
            {"read_document"},
            _all(_ran("read_document"), _says("ledger")),
        ),
        r(
            "read_settings",
            "read",
            library,
            "Is the Director turned on?",
            {"read_settings"},
            _all(_ran("read_settings"), _matches(r"\b(yes|on|enabled)\b")),
        ),
        r(
            "read_count",
            "read",
            chat,
            "How many messages are in this chat?",
            {"query_library", "read_messages", "read_context"},
            _matches(r"\b(8|eight)\b"),
        ),
        r(
            "read_chat_world",
            "read",
            chat,
            "Which world does this chat use?",
            {"read_context", "query_library", "list_active_lore_entries"},
            _says("Harbour"),
        ),
        r("read_personality", "read", chat, "What is Mara's personality?", {"read_character"}, _says("patient")),
        r("read_group", "read", group, "Who is in this group chat?", {"read_context", "query_library"}, _says("Mara", "Ivo")),
        r(
            "read_earlier",
            "read",
            {"mode": "chat", "conversation_id": "crossing"},
            "Near the start of this chat, what fare did Kestrel name?",
            {"read_messages", "query_library"},
            _says("three silver"),
        ),
        r(
            "read_active_lore",
            "read",
            chat,
            "Which lore entries are in effect in this chat?",
            {"list_active_lore_entries", "read_context"},
            _says("Tide tax", "Lighthouse"),
        ),
        r(
            "read_chat_state",
            "read",
            chat,
            "What does this chat's inventory state hold?",
            {"read_chat_state"},
            _all(_ran("read_chat_state"), _says("compass")),
        ),
        # Small writes
        r(
            "write_director_off",
            "write",
            library,
            "Turn the Director off.",
            {"set_pipeline_toggle", "read_settings"},
            _db(_director_off),
        ),
        r(
            "write_workflows_off",
            "write",
            library,
            "Switch off secondary workflows.",
            {"set_pipeline_toggle", "read_settings"},
            _db(_workflows_off),
        ),
        r(
            "write_world",
            "write",
            library,
            "Create a new world called Saltmarsh.",
            {"create_world", "list_worlds"},
            _db(_world_named),
        ),
        r(
            "write_entry",
            "write",
            library,
            'Add a lore entry to the Harbour world called Night Market: "Opens at moonrise on the east quay." Keywords: market, night.',
            {"list_worlds", "query_library"},
            _db(_night_market),
        ),
        r(
            "write_persona",
            "write",
            library,
            "Make me a persona named Corin, a retired smuggler.",
            {"create_persona", "list_personas"},
            _db(_persona_named),
        ),
        r(
            "write_character",
            "write",
            library,
            "Create a character named Tamsin, a lighthouse keeper.",
            {"create_character", "list_characters"},
            _db(_character_named),
        ),
        r(
            "write_chat_world",
            "write",
            chat,
            "Use the Inland world in this chat too.",
            {"list_worlds", "read_context", "query_library"},
            _db(_inland_attached),
        ),
        r(
            "write_checkpoint",
            "write",
            chat,
            "Save a checkpoint of this chat called Before the deal.",
            {"checkpoint_chat"},
            _db(_checkpointed),
        ),
        r(
            "write_entry_off",
            "write",
            library,
            "Turn off the Lighthouse lore entry in the Harbour world.",
            {"list_worlds", "query_library", "list_lore_entries"},
            _db(_lighthouse_off),
        ),
        r(
            "write_start_chat",
            "write",
            library,
            "Start a new chat with Ivo.",
            {"list_characters", "query_library", "start_chat"},
            _db(_ivo_chat),
        ),
        r("write_rename", "write", chat, "Rename this chat to Ledger night.", {"set_chat"}, _db(_renamed)),
        r(
            "write_length",
            "write",
            library,
            "Limit replies to 150 words.",
            {"set_length_guard", "read_settings"},
            _db(_length_150),
        ),
        r(
            "write_phrase",
            "write",
            library,
            'Add "a shiver ran down her spine" to the phrase bank.',
            {"add_banned_phrase"},
            _db(_phrase_banned),
        ),
        r(
            "write_world_global",
            "write",
            library,
            "Make the Inland world global.",
            {"list_worlds", "query_library", "set_world"},
            _db(_inland_global),
        ),
        r(
            "write_mood",
            "write",
            library,
            "Make a new mood fragment - Terse.",
            {"create_mood_fragment", "list_fragments"},
            _db(_mood_terse),
        ),
        r(
            "write_mood_off",
            "write",
            library,
            "Turn off the Tense mood.",
            {"list_fragments", "read_fragment", "set_mood_fragment"},
            _db(_tense_off),
        ),
        r(
            "write_decision",
            "write",
            library,
            "Add a yes/no decision fragment that asks whether the weather turns bad.",
            {"create_decision_fragment", "list_fragments"},
            _db(_weather_decision),
        ),
        # One edit
        r(
            "edit_mood",
            "edit",
            library,
            "Make the Tense mood's prompt text also ask for short sentences.",
            {"list_fragments", "read_fragment"},
            _proposed("mood_fragment", "prompt_text", lambda b, a, o: "short" in a.casefold() and len(a) > len(b)),
        ),
        r(
            "edit_entry_name",
            "edit",
            library,
            "Rename the Tide tax lore entry to Dusk toll.",
            {"list_worlds", "query_library"},
            _proposed("lorebook_entry", "name", lambda b, a, o: a.strip() == "Dusk toll"),
        ),
        r(
            "edit_persona",
            "edit",
            library,
            "Change my persona Wren's description so she grew up Inland instead of in the Harbour.",
            {"list_personas", "query_library"},
            _proposed("persona", "description", lambda b, a, o: "Inland" in a and "in the Harbour" not in a and "courier" in a),
        ),
        r(
            "edit_scenario",
            "edit",
            chat,
            "Set Mara's scenario to: A rain-soaked night at the fish market.",
            {"edit_character", "read_character"},
            _proposed("character", "scenario", lambda b, a, o: a.strip() == "A rain-soaked night at the fish market."),
        ),
        r(
            "edit_tags",
            "edit",
            chat,
            "Add the tag Harbour to Mara's card.",
            {"read_character", "edit_character"},
            _proposed("character", "tags", _tagged),
        ),
        r(
            "edit_word",
            "edit",
            chat,
            'In Mara\'s last reply, change "crate" to "barrel".',
            {"read_messages"},
            _proposed("message", "content", lambda b, a, o: _flat(a) == _flat(b.replace("crate", "barrel"))),
        ),
        # Three places in one field
        r(
            "edit_typos",
            "edit3",
            chat,
            "Fix the three typos in Mara's last reply.",
            {"read_messages"},
            _proposed("message", "content", _typos_fixed),
        ),
        r(
            "edit_three_names",
            "edit3",
            {"mode": "library", "character_id": "ivo"},
            'In Ivo\'s description, replace every "Customs House" with "Tollhouse".',
            {"read_character"},
            _proposed("character", "description", _tollhouse),
        ),
        # A field near 8,000 characters
        r(
            "edit_long_word",
            "edit_long",
            {"mode": "library", "character_id": "kestrel"},
            "In Kestrel's description, change Port Vell to Port Sable.",
            {"read_character", "edit_character"},
            _proposed("character", "description", _port_sable),
        ),
        r(
            "edit_long_cut",
            "edit_long",
            {"mode": "library", "character_id": "kestrel"},
            "Remove the last paragraph of Kestrel's description.",
            {"read_character"},
            _proposed("character", "description", _last_paragraph_gone),
        ),
        # Media and handoffs
        r(
            "media_image",
            "media",
            chat,
            "Make an image of Mara's last reply.",
            {"read_messages", "generate_image"},
            _media("generate_image"),
        ),
        r(
            "media_speech",
            "media",
            chat,
            "Generate speech for the last reply.",
            {"read_messages", "generate_speech"},
            _media("generate_speech"),
        ),
        r(
            "handoff_regenerate",
            "handoff",
            chat,
            "Regenerate Mara's last reply.",
            {"read_messages", "regenerate_reply"},
            _handed("regenerate_reply"),
        ),
        r(
            "handoff_rewrite",
            "handoff",
            chat,
            "Rewrite the last reply to be shorter.",
            {"read_messages", "rewrite_reply", "edit_message"},
            _either(_handed("rewrite_reply"), _proposed("message", "content", _shorter_reply)),
        ),
        r(
            "handoff_open",
            "handoff",
            library,
            "Open my chat Mara at the quay.",
            {"query_library", "list_characters", "open_chat"},
            _opened_chat,
        ),
        r(
            "handoff_continue",
            "handoff",
            group,
            "Let Ivo take the next turn in this group.",
            {"read_context", "continue_chat"},
            _ivo_speaks,
        ),
    ]


# --------------------------------------------------------------------------------------------------------------------------
# One run


def _usable_step(message: Mapping[str, Any], names: set[str]) -> str | None:
    """The tool a two-call step chose, or ``None`` when the loop would have refused the reply."""
    from backend.inference import strict_tool_arguments

    if message.get("finish_reason") == "length":
        return None
    arguments = strict_tool_arguments(message, "assistant_step")
    if arguments is None or set(arguments) != {"tool", "message"}:
        return None
    tool, text = arguments.get("tool"), arguments.get("message")
    if not isinstance(tool, str) or not isinstance(text, str) or (tool not in names and tool != "reply"):
        return None
    return tool if text.strip() or tool != "reply" else None


def _single_choice(message: Mapping[str, Any], names: set[str]) -> tuple[str | None, bool]:
    """``(tool named, usable)`` for a single-call reply; the tool is ``None`` when no one listed tool could be read."""
    from backend.inference import parse_tool_calls, strict_tool_arguments

    calls = parse_tool_calls(message)
    name = calls[0].get("name") if len(calls) == 1 else None
    if not isinstance(name, str) or (name not in names and name != "reply"):
        return None, False
    if message.get("finish_reason") == "length":
        return name, False
    arguments = strict_tool_arguments(message, name)
    if arguments is None:
        return name, False
    if name == "reply":
        text = arguments.get("message")
        return name, set(arguments) == {"message"} and isinstance(text, str) and bool(text.strip())
    return name, True


def _measure(calls: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]], names: set[str]) -> dict[str, Any]:
    """Tool choice and argument accuracy from the recorded calls and the run's events."""
    first: str | None = None
    unusable: Counter[str] = Counter()
    seen_first = False
    for call in calls:
        message, forced = call["message"], call["forced"]
        if forced == "assistant_step":
            tool, usable = _usable_step(message, names), None
            usable = tool is not None
            label = "step"
        elif forced == "required":
            tool, usable = _single_choice(message, names)
            label = tool or "step"
        else:
            from backend.inference import strict_tool_arguments

            tool = forced
            usable = message.get("finish_reason") != "length" and strict_tool_arguments(message, forced) is not None
            label = forced
        if forced in ("assistant_step", "required") and not seen_first:
            seen_first = True
            first = tool if usable else None
        if not usable:
            unusable[label] += 1
    invalid: Counter[str] = Counter()
    refused: Counter[str] = Counter()
    for event in events:
        if event["type"] == "tool_result" and not event["ok"]:
            (invalid if event["summary"].startswith("Invalid arguments") else refused)[event["tool"]] += 1
    argument_faults = sum(count for label, count in unusable.items() if label != "step") + sum(invalid.values())
    return {
        "first_tool": first,
        "unusable": dict(unusable),
        "invalid": dict(invalid),
        "refused": dict(refused),
        "args_first": argument_faults == 0,
        "retries": sum(unusable.values()),
    }


# What the as-profiled arm has learned so far in this worker: (structured output ignored, forced choice ignored).
_SESSION: tuple[set[tuple[str, str]], set[tuple[str, str]]] = (set(), set())


def _worker_init(root: str) -> None:
    import logging

    import backend.database.connection as db_connection

    logging.basicConfig(level=logging.ERROR)
    path = Path(root) / f"worker-{os.getpid()}.db"
    db_connection.DB_PATH = str(path)


def run_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """One request on one arm, on a fresh copy of the seeded library. Runs in a worker process."""
    import backend.database.connection as db_connection

    path = Path(db_connection.DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(job["template"], path)
    return asyncio.run(_job(job))


async def _job(job: Mapping[str, Any]) -> dict[str, Any]:
    from backend.api import deps
    from backend.api.assistant import HOST, MediaRun, build_tools
    from backend.api.routes.assistant import resolve_view
    from backend.api.schemas import AssistantViewContext
    from backend.core import locks
    from backend.database import get_settings
    from backend.features.assistant import AssistantUnavailable, ToolOutput, run_assistant
    from backend.features.assistant.tools import reply_target
    from backend.inference import AbortToken, LLMClient, endpoint_profiles

    for held in (deps._conversation_stream_locks, deps._active_streams, locks._workflow_state_locks):
        held.clear()
    single, native = ARMS[job["arm"]]
    url, model = job["endpoint"], job["model"]
    # Demotions are process-wide and learned from replies. The as-profiled arm keeps its own from run to run, as a session
    # does; the native arms start each run from the profile plus their forced demotion.
    structured_ignored, choice_ignored = endpoint_profiles._STRUCTURED_OUTPUT_IGNORED, endpoint_profiles._FORCED_CHOICE_IGNORED
    structured_ignored.clear()
    choice_ignored.clear()
    if not native:
        structured_ignored.update(_SESSION[0])
        choice_ignored.update(_SESSION[1])
    if native:
        endpoint_profiles.note_structured_output_ignored(url, model)
    seed = job["seed"]
    request = next(item for item in requests() if item.id == job["request"])

    class Recorded(LLMClient):
        calls: list[dict[str, Any]]

        async def complete(self, messages, model, tools=None, tool_choice=None, **params):  # type: ignore[override]
            started = time.monotonic()
            message: dict[str, Any] = {}
            usage = None
            error = ""
            try:
                async for event in super().complete(messages, model, tools, tool_choice, **params):
                    if event["type"] == "done":
                        message = dict(event.get("message") or {})
                        usage = event.get("usage")
                    yield event
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:300]
                raise
            finally:
                # A call that failed is recorded too, with no message, so it counts as an unusable reply.
                forced = (tool_choice.get("function") or {}).get("name") if isinstance(tool_choice, dict) else str(tool_choice)
                self.calls.append(
                    {
                        "forced": forced,
                        "seconds": round(time.monotonic() - started, 2),
                        "message": {
                            "content": str(message.get("content") or "")[:4000],
                            "tool_calls": message.get("tool_calls"),
                            "finish_reason": message.get("finish_reason"),
                        },
                        "usage": usage,
                        "error": error,
                    }
                )

    abort = AbortToken()
    client = Recorded(url, api_key=os.environ[job["key_env"]], abort_token=abort, completion_mode="chat")
    client.calls = []
    probe = [{"role": "user", "content": "x"}]
    mode = (
        "native"
        if client.sends_tool_schemas(probe, model)
        else "structured"
        if endpoint_profiles.supports_structured_tool_calls(url, model)
        else "text"
    )
    record: dict[str, Any] = {key: job[key] for key in ("arm", "request", "repeat")}
    record.update(kind=request.kind, mode=mode, model=model)
    if single and mode != "native":
        return {**record, "outcome": "unsupported", "detail": f"single-call needs native tools; this arm runs {mode}"}

    context = {key: seed[value] if key.endswith("_id") else value for key, value in request.view.items()}
    view = await resolve_view(AssistantViewContext(**context))
    settings = await get_settings()
    progress: asyncio.Queue[str] = asyncio.Queue()
    media = MediaRun(job=f"assistant:bench-{os.getpid()}", abort=abort, report=progress.put_nowait)
    tools = []
    for tool in build_tools(settings, view, media=media):
        if tool.reports_progress:
            # The render is not what is measured: keep the real argument model and target checks, and save nothing.
            async def stub(args: Any, name: str = tool.name) -> ToolOutput:
                conv, message = await reply_target(
                    args.conversation_id, args.message_id, view, HOST, "made into media", idle=False
                )
                ids = {"conversation_id": conv["id"], "message_id": message["id"], "attachment_ids": [1]}
                data = {"saved": True, "attachment_ids": [1]}
                return ToolOutput(data, f"Saved media on message {message['id']}.", ids, committed=({"kind": "media", **ids},))

            tool = replace(tool, run=stub)
        tools.append(tool)
    names = {tool.name for tool in tools}

    events: list[dict[str, Any]] = []
    outcome, detail = "", ""
    started = time.monotonic()
    try:
        async with asyncio.timeout(JOB_SECONDS):
            async for event in run_assistant(
                client,
                model,
                settings=settings,
                tools=tools,
                request=request.text,
                view=view,
                progress=progress,
                single_call=single,
            ):
                events.append(event)
                if event["type"] == "done":
                    outcome = event["reason"]
    except AssistantUnavailable as exc:
        outcome, detail = "unavailable", str(exc)
    except TimeoutError:
        outcome, detail = "timeout", f"over {JOB_SECONDS}s"
    except Exception as exc:  # noqa: BLE001 -- a provider failure is recorded and retried, never counted against a protocol
        detail = f"{type(exc).__name__}: {exc}"[:500]
        # NanoGPT's answer when the model made no call under a forced choice: the model's failure, which the app would show.
        outcome = "no_tool_call" if "no tool call despite" in detail else "provider_error"
    wall = time.monotonic() - started
    passed = outcome not in ("unavailable", "timeout", "provider_error", "no_tool_call") and await request.check(
        Outcome(events, seed)
    )
    measured = _measure(client.calls, events, names)
    tokens = [call["usage"] or {} for call in client.calls]
    if not native:
        _SESSION[0].update(endpoint_profiles._STRUCTURED_OUTPUT_IGNORED)
        _SESSION[1].update(endpoint_profiles._FORCED_CHOICE_IGNORED)
    learned = sorted(
        f"{kind}:{pair[1]}"
        for kind, pairs in (
            ("structured_ignored", endpoint_profiles._STRUCTURED_OUTPUT_IGNORED),
            ("forced_choice_ignored", endpoint_profiles._FORCED_CHOICE_IGNORED),
        )
        for pair in pairs
        if not (native and kind == "structured_ignored")
    )
    return {
        **record,
        "outcome": outcome,
        "detail": detail,
        "success": bool(passed),
        "right_first": measured["first_tool"] in request.first,
        **measured,
        "model_calls": len(client.calls),
        "tool_runs": sum(1 for event in events if event["type"] == "tool_result"),
        "prompt_tokens": sum(int(usage.get("prompt_tokens") or 0) for usage in tokens),
        "completion_tokens": sum(int(usage.get("completion_tokens") or 0) for usage in tokens),
        "wall_s": round(wall, 2),
        "learned": learned,
        "events": [event for event in events if event["type"] != "progress"],
        "calls": client.calls,
    }


# --------------------------------------------------------------------------------------------------------------------------
# Plan, run, resume


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout.strip()


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run(args: argparse.Namespace) -> None:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    if args.key_env not in os.environ:
        sys.exit(f"Set {args.key_env} to the endpoint's API key.")
    arms = [arm.strip() for arm in args.arms.split(",")]
    unknown = [arm for arm in arms if arm not in ARMS]
    if unknown:
        sys.exit(f"Unknown arms: {unknown}; choose from {sorted(ARMS)}")
    chosen = [item for item in requests() if not args.only or item.id in args.only.split(",")]

    template = out / "seed.db"
    seed_path = out / "seed.json"
    if not template.exists():
        template.unlink(missing_ok=True)
        seed = asyncio.run(seed_library(template))
        seed_path.write_text(json.dumps(seed, indent=1))
    seed = json.loads(seed_path.read_text())
    meta = {
        "endpoint": args.endpoint,
        "model": args.model,
        "arms": arms,
        "repeats": args.repeats,
        "requests": [item.id for item in chosen],
        "commit": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain", "--", "backend")),
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out / f"meta-{datetime.now(UTC):%Y%m%dT%H%M%S}.json").write_text(json.dumps(meta, indent=1))

    results_path = out / "runs.jsonl"
    done = {
        (r["arm"], r["request"], r["repeat"])
        for r in _load(results_path)
        if r["outcome"] not in ("provider_error", "harness_error")
    }
    jobs = [
        {
            "arm": arm,
            "request": item.id,
            "repeat": repeat,
            "endpoint": args.endpoint,
            "model": args.model,
            "key_env": args.key_env,
            "template": str(template),
            "seed": seed,
        }
        for repeat in range(1, args.repeats + 1)
        for item in chosen
        for arm in arms
        if (arm, item.id, repeat) not in done
    ]
    # Interleave arms and requests, so provider drift over the run never lines up with one arm.
    random.Random(args.shuffle_seed).shuffle(jobs)
    print(f"{len(jobs)} runs to do ({len(done)} already recorded) with {args.workers} workers", flush=True)
    total = len(jobs)
    attempts: Counter[tuple[str, str, int]] = Counter()
    with (
        tempfile.TemporaryDirectory(prefix="assistant-bench-") as root,
        ProcessPoolExecutor(args.workers, initializer=_worker_init, initargs=(root,)) as pool,
        results_path.open("a") as sink,
    ):
        pending = {pool.submit(run_job, job): job for job in jobs}
        finished = 0
        while pending:
            for future in as_completed(list(pending)):
                job = pending.pop(future)
                key = (job["arm"], job["request"], job["repeat"])
                attempts[key] += 1
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 -- a harness fault is recorded and left unscored, not retried
                    result = {
                        **{k: job[k] for k in ("arm", "request", "repeat")},
                        "outcome": "harness_error",
                        "detail": repr(exc)[:500],
                    }
                result["attempt"] = attempts[key]
                sink.write(json.dumps(result, ensure_ascii=False) + "\n")
                sink.flush()
                finished += 1
                mark = "ok " if result.get("success") else "-- "
                print(
                    f"[{finished}/{total}] {mark}{job['arm']:14} {job['request']:20} r{job['repeat']} "
                    f"{result['outcome']:14} calls={result.get('model_calls', '?')} {result.get('wall_s', '?')}s "
                    f"{result.get('detail', '')[:120]}",
                    flush=True,
                )
                if result["outcome"] == "provider_error" and attempts[key] < PROVIDER_RETRIES:
                    time.sleep(PROVIDER_BACKOFF_SECONDS)
                    pending[pool.submit(run_job, job)] = job
                    total += 1
                break
    report(out)


# --------------------------------------------------------------------------------------------------------------------------
# Report


def _rate(values: Sequence[bool]) -> str:
    return f"{100 * sum(values) / len(values):.1f}%" if values else "–"


def _paired(rows: Sequence[Mapping[str, Any]], a: str, b: str, metric: str) -> tuple[float, float, float] | None:
    """Mean of (b − a) over requests, each request's rate averaged over its repeats, with a bootstrap 95% interval."""
    by: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by[row["request"]][row["arm"]].append(bool(row[metric]))
    diffs = [statistics.fmean(arms[b]) - statistics.fmean(arms[a]) for arms in by.values() if arms.get(a) and arms.get(b)]
    if not diffs:
        return None
    rng = random.Random(0)
    boot = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(BOOTSTRAP_SAMPLES))
    return statistics.fmean(diffs), boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1]


def report(out: Path) -> None:
    rows_all = _load(out / "runs.jsonl")
    # The last attempt of each run counts; provider failures that never recovered are listed, not scored.
    latest: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows_all:
        latest[(row["arm"], row["request"], row["repeat"])] = row
    unscoring = ("provider_error", "harness_error", "unsupported")
    unscored = [row for row in latest.values() if row["outcome"] in unscoring]
    rows = [row for row in latest.values() if row["outcome"] not in unscoring]
    accepted = {item.id: item.first for item in requests()}
    for row in rows:
        # Scored against the request list as it stands, so a corrected acceptable set rescores recorded runs.
        row["right_first"] = row["first_tool"] in accepted[row["request"]]
        row["accurate"] = row["right_first"] and row["args_first"]
    arms = [arm for arm in ARMS if any(row["arm"] == arm for row in rows)]
    seed = json.loads((out / "seed.json").read_text()) if (out / "seed.json").exists() else {}
    metas = sorted(out.glob("meta-*.json"))
    meta = json.loads(metas[-1].read_text()) if metas else {}

    lines = [
        "# Assistant bench",
        "",
        f"Model `{meta.get('model', '?')}` at `{meta.get('endpoint', '?')}`, commit `{meta.get('commit', '?')[:10]}`"
        f"{' with uncommitted backend changes' if meta.get('dirty') else ''}. "
        f"{len(rows)} scored runs; {len(unscored)} unscored (provider or harness error, or an arm the endpoint cannot run).",
        "",
        "Accurate: the first tool chosen was in the request's acceptable set, and every argument call was usable and valid on "
        "its first attempt. Success: the request's own outcome check passed.",
        "",
        "| Arm | Mode | Runs | Accurate | Right tool first | Args first try | Success | Hit a run limit | Retries / run | Invalid args / run "
        "| Model calls / run | Prompt tokens / run | Median wall s |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in arms:
        mine = [row for row in rows if row["arm"] == arm]
        modes = ",".join(sorted({row["mode"] for row in mine}))
        lines.append(
            f"| {arm} | {modes} | {len(mine)} | {_rate([r['accurate'] for r in mine])} | {_rate([r['right_first'] for r in mine])} "
            f"| {_rate([r['args_first'] for r in mine])} | {_rate([r['success'] for r in mine])} | {_rate([r['outcome'] == 'limit' for r in mine])} "
            f"| {statistics.fmean(r['retries'] for r in mine):.2f} | {statistics.fmean(sum(r['invalid'].values()) for r in mine):.2f} "
            f"| {statistics.fmean(r['model_calls'] for r in mine):.2f} | {statistics.fmean(r['prompt_tokens'] for r in mine):,.0f} "
            f"| {statistics.median(r['wall_s'] for r in mine):.1f} |"
        )
    if "single-native" in arms:
        lines += [
            "",
            "## Single-call against two-call",
            "",
            "Paired over requests (each request's rate over its repeats); bootstrap 95% interval.",
            "",
        ]
        lines += ["| Against | Metric | Single − two-call | 95% interval |", "| --- | --- | ---: | --- |"]
        for other in (arm for arm in arms if arm != "single-native"):
            for metric in ("accurate", "success"):
                paired = _paired(rows, other, "single-native", metric)
                if paired:
                    mean, low, high = paired
                    lines.append(f"| {other} | {metric} | {100 * mean:+.1f} pts | {100 * low:+.1f} to {100 * high:+.1f} |")

    lines += ["", "## By kind of request", "", "| Kind | " + " | ".join(f"{arm} accurate / success" for arm in arms) + " |"]
    lines.append("| --- |" + " ---: |" * len(arms))
    for kind in dict.fromkeys(
        row["kind"] for row in sorted(rows, key=lambda r: [x.id for x in requests()].index(r["request"]))
    ):
        cells = []
        for arm in arms:
            mine = [row for row in rows if row["arm"] == arm and row["kind"] == kind]
            cells.append(f"{_rate([r['accurate'] for r in mine])} / {_rate([r['success'] for r in mine])}")
        lines.append(f"| {kind} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## By request (successes / runs)",
        "",
        "| Request | " + " | ".join(arms) + " |",
        "| --- |" + " ---: |" * len(arms),
    ]
    for item in requests():
        cells = []
        for arm in arms:
            mine = [row for row in rows if row["arm"] == arm and row["request"] == item.id]
            if not mine:
                cells.append("–")
                continue
            outcomes = Counter(row["outcome"] for row in mine if not row["success"])
            note = f" ({', '.join(f'{k} {v}' for k, v in outcomes.items())})" if outcomes else ""
            cells.append(f"{sum(r['success'] for r in mine)}/{len(mine)}{note}")
        if any(cell != "–" for cell in cells):
            lines.append(f"| {item.id} | " + " | ".join(cells) + " |")

    lines += ["", "## Failures by tool", ""]
    for arm in arms:
        mine = [row for row in rows if row["arm"] == arm]
        wrong = Counter(str(row["first_tool"]) for row in mine if not row["right_first"])
        unusable: Counter[str] = Counter()
        invalid: Counter[str] = Counter()
        refused: Counter[str] = Counter()
        for row in mine:
            unusable.update(row["unusable"])
            invalid.update(row["invalid"])
            refused.update(row["refused"])
        lines.append(f"**{arm}**")
        lines.append("")
        for label, counter in (
            ("Wrong or unreadable first choice", wrong),
            ("Unusable replies (retried)", unusable),
            ("Invalid arguments (refused)", invalid),
            ("Tool refusals (target, find text, limits)", refused),
        ):
            listed = ", ".join(f"`{name}` {count}" for name, count in counter.most_common()) or "none"
            lines.append(f"- {label}: {listed}")
        learned = Counter(item for row in mine for item in row.get("learned", []))
        if learned:
            lines.append(f"- Demotions learned mid-run: {', '.join(f'{k} {v}' for k, v in learned.items())}")
        lines.append("")
    if unscored:
        lines += ["## Unscored", ""]
        for row in sorted(unscored, key=lambda r: (r["arm"], r["request"], r["repeat"])):
            lines.append(f"- {row['arm']} {row['request']} r{row['repeat']}: {row['outcome']} {row.get('detail', '')[:200]}")
        lines.append("")
    if seed:
        lines += [
            "## Library",
            "",
            f"Kestrel's description: {len(seed['kestrel_text']):,} characters. Switches at start: "
            f"`{json.dumps(seed['switches'])[:400]}`",
            "",
        ]
    (out / "REPORT.md").write_text("\n".join(lines))
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", default="https://nano-gpt.com/api/v1")
    parser.add_argument("--model", default="google/gemma-4-31b-it")
    parser.add_argument("--key-env", default="NANOGPT_API_KEY", help="Environment variable that holds the API key.")
    parser.add_argument("--arms", default="two-default,single-native")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="", help="Comma-separated request ids; default all.")
    parser.add_argument("--shuffle-seed", type=int, default=7)
    parser.add_argument("--output", default="")
    parser.add_argument("--report", default="", help="Rebuild REPORT.md from an output folder without running anything.")
    args = parser.parse_args()
    if args.report:
        report(Path(args.report))
    elif args.output:
        run(args)
    else:
        parser.error("--output or --report is required")


if __name__ == "__main__":
    main()
