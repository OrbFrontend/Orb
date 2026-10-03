"""Phrase Bank suggestions end to end: mining a fixture library, then accept and dismiss."""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend.database import count_model_replies, replace_slop_suggestions
from backend.features.slop_suggestions import miner

FILLER = (
    "She walks to the window and looks outside. The rain taps against the glass. "
    "He sets the cup down on the table. A car passes on the street below. "
    "She folds her arms and waits. The clock on the wall ticks."
)
TIC = " *A beat.*"
STAMP = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _small_library(monkeypatch):
    """A fixture library is far below the real minimums; the method is the same."""
    monkeypatch.setattr(miner, "MIN_CHARACTERS", 10)
    monkeypatch.setattr(miner, "MIN_CARD_NARRATION", 50)


def _seed(path: str, *, tic_in_replies: int, tic_elsewhere: int, plain: int = 3) -> None:
    """Characters whose model replies use the tic, whose user messages and turn-0
    greetings use it instead, and who never use it. Cards hold only the filler."""
    conn = sqlite3.connect(path)
    try:
        for index in range(10):
            conn.execute(
                "INSERT INTO character_cards (id, name, first_mes, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (f"card-{index}", f"Author{index}", f"{FILLER}\n\n{FILLER}", STAMP, STAMP),
            )
        kinds = ["reply"] * tic_in_replies + ["elsewhere"] * tic_elsewhere + ["plain"] * plain
        for index, kind in enumerate(kinds):
            conv = f"conv-{index}"
            conn.execute(
                "INSERT INTO conversations (id, character_card_id, character_name, created_at) VALUES (?, ?, ?, ?)",
                (conv, f"char-{index}", f"Persona{index}", STAMP),
            )
            elsewhere = TIC if kind == "elsewhere" else ""
            rows = [("assistant", FILLER + elsewhere, 0)]
            for turn in range(1, 7, 2):
                rows.append(("user", "*I nod.*" + elsewhere, turn))
                rows.append(("assistant", FILLER + (TIC if kind == "reply" else ""), turn + 1))
            conn.executemany(
                "INSERT INTO messages (conversation_id, role, content, turn_index, created_at) VALUES (?, ?, ?, ?, ?)",
                [(conv, role, content, turn, STAMP) for role, content, turn in rows],
            )
        conn.commit()
    finally:
        conn.close()


def _keys(result: miner.MineResult) -> dict[str, dict]:
    return {draft["key"]: dict(draft) for draft in result["suggestions"] or []}


def test_a_tic_spread_across_characters_and_absent_from_cards_is_suggested(db_path):
    path = str(db_path)
    _seed(path, tic_in_replies=12, tic_elsewhere=0)

    result = miner.mine(path, [], {}, [])
    assert result["suggestions"] is not None, result["status"]
    beat = _keys(result)["n:= a beat"]
    assert beat["lane"] == "longstanding"
    assert beat["label"] == "A beat."
    assert beat["stats"]["card_observed"] == 0 and beat["stats"]["spread"] == 12
    assert beat["examples"] == ["*A beat.*"]

    assert "n:= a beat" not in _keys(miner.mine(path, [], {}, ["n:= a beat"])), "a dismissed key came back"

    flagged = miner.mine(path, [{"kind": "literal", "variants": ["a beat"]}], {}, [])
    assert "n:= a beat" not in _keys(flagged), "a phrase the bank already catches was suggested"


def test_user_messages_and_greetings_contribute_nothing(db_path):
    path = str(db_path)
    _seed(path, tic_in_replies=0, tic_elsewhere=12)

    result = miner.mine(path, [], {}, [])
    assert result["suggestions"] is not None, result["status"]
    assert "n:= a beat" not in _keys(result)


async def test_accept_is_the_only_path_into_the_bank_and_dismissals_persist(client, db):
    drafts = [
        {
            "key": key,
            "lane": "longstanding",
            "label": label,
            "pattern": pattern,
            "stats": {},
            "fillers": [],
            "examples": [label],
        }
        for key, label, pattern in [
            ("n:= a beat", "A beat.", r"^[\W_]*a[\W_]+beat[\W_]*$"),
            ("n:= a pause", "A pause.", r"^[\W_]*a[\W_]+pause[\W_]*$"),
        ]
    ]
    # Recorded at the current reply count, so reading the list starts no run.
    await replace_slop_suggestions(drafts, replies_at_run=await count_model_replies(), status="ok", mined_at=STAMP)

    async def bank_size() -> int:
        return (await (await db.execute("SELECT COUNT(*) FROM phrase_bank")).fetchone())[0]

    before = await bank_size()
    listed = (await client.get("/api/phrase-bank/suggestions")).json()
    assert listed["refreshing"] is False
    assert [s["label"] for s in listed["suggestions"]] == ["A beat.", "A pause."]
    beat, pause = listed["suggestions"]

    too_long = await client.post(f"/api/phrase-bank/suggestions/{beat['id']}/accept", json={"pattern": "a" * 301})
    assert too_long.status_code == 400
    assert (await client.post(f"/api/phrase-bank/suggestions/{pause['id']}/dismiss", json={})).status_code == 200
    assert await bank_size() == before

    edited = r"^[\W_]*a[\W_]+(?:beat|moment)[\W_]*$"
    accepted = await client.post(f"/api/phrase-bank/suggestions/{beat['id']}/accept", json={"pattern": edited})
    assert accepted.status_code == 200
    assert await bank_size() == before + 1
    row = await (
        await db.execute("SELECT kind, pattern, variants FROM phrase_bank WHERE id = ?", (accepted.json()["id"],))
    ).fetchone()
    assert (row["kind"], row["pattern"], json.loads(row["variants"])) == ("regex", edited, [])
    assert (await client.post(f"/api/phrase-bank/suggestions/{beat['id']}/accept", json={"pattern": edited})).status_code == 404

    # A run that started before both actions mines both keys again, with the
    # original patterns: the dismissed key and the one accepted with an edit stay gone.
    await replace_slop_suggestions(
        drafts,
        replies_at_run=await count_model_replies(),
        status="ok",
        mined_at=STAMP,
        keys_at_start=[d["key"] for d in drafts],
    )
    assert (await client.get("/api/phrase-bank/suggestions")).json()["suggestions"] == []
    # A later run still never offers the dismissed key.
    await replace_slop_suggestions(drafts, replies_at_run=await count_model_replies(), status="ok", mined_at=STAMP)
    assert [s["label"] for s in (await client.get("/api/phrase-bank/suggestions")).json()["suggestions"]] == ["A beat."]
