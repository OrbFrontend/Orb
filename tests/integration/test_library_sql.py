"""The card generator's model-written SQL runs read-only over documented views only."""

from pathlib import Path

import pytest

from backend.database import (
    add_message,
    create_user_persona,
    get_persona_conversation_counts,
    run_library_query,
)
from backend.features.card_generator.deep import LIBRARY_VIEWS, VIEW_DOCS

CAPS = {"max_rows": 50, "max_cell_chars": 500, "max_result_chars": 4000, "time_limit_s": 5}
FEATURE_DOC = Path(__file__).resolve().parents[2] / "docs" / "features" / "card-generator.md"


async def query(sql: str, **caps):
    return await run_library_query(sql, **{**CAPS, **caps})


@pytest.fixture
async def library(client, db):
    await db.execute("DELETE FROM user_personas")
    await db.execute("INSERT INTO endpoints (url, api_key) VALUES ('https://provider.invalid', 'sk-SECRET')")
    await db.commit()
    personas = {
        name: (await create_user_persona({"name": name, "description": f"{name} body"}))["id"] for name in ("Kai", "Rin")
    }
    await db.execute("UPDATE settings SET active_persona_id = ? WHERE id = 1", (personas["Kai"],))
    await db.commit()
    cards = []
    for name, tags in [("Mara", ["Noir", "Harbour"]), ("Ivo", ["Noir"])]:
        card = (await client.post("/api/characters", json={"name": name, "tags": tags, "description": f"{name} prose"})).json()
        cards.append(card["id"])
    await db.execute("UPDATE character_cards SET avatar_b64 = 'AVATAR', system_prompt = 'OVERRIDE' WHERE id = ?", (cards[0],))
    await db.commit()
    await client.put(f"/api/characters/{cards[1]}", json={"persona_lock_id": personas["Rin"]})
    conversations = []
    for card_id in (cards[0], cards[1], cards[1]):
        conversations.append((await client.post("/api/conversations", json={"character_card_id": card_id})).json()["id"])
    await client.put(f"/api/conversations/{conversations[2]}", json={"persona_lock_id": personas["Kai"]})
    root, _ = await add_message(conversations[0], "user", "I lean on the rail.", 0)
    for text in ("Mara grins.", "Mara scowls."):
        await add_message(conversations[0], "assistant", text, 1, parent_id=root, advance_leaf=True)
    return {"cards": cards, "conversations": conversations, "personas": personas}


async def test_views_expose_documented_columns_in_order(library):
    doc = FEATURE_DOC.read_text()
    for view, names in LIBRARY_VIEWS.items():
        assert (await query(f"SELECT * FROM {view} LIMIT 0"))["columns"] == list(names)
        assert f"- {view}({', '.join(names)})" in VIEW_DOCS
        assert f"| `{view}` | {', '.join(f'`{name}`' for name in names)} |" in doc


async def test_views_read_rows(library):
    result = await query("SELECT name, tags FROM characters ORDER BY name")
    assert result == {
        "columns": ["name", "tags"],
        "rows": [["Ivo", '["Noir"]'], ["Mara", '["Noir", "Harbour"]']],
        "more_rows": False,
    }
    result = await query("SELECT role, content FROM messages WHERE role = 'user'")
    assert result["rows"] == [["user", "I lean on the rail."]]
    assert (await query("SELECT count(*) FROM user_personas"))["rows"] == [[2]]


async def test_persona_id_resolves_like_the_digest_counts(library):
    result = await query(
        "SELECT persona_id, count(*) FROM conversations GROUP BY persona_id HAVING persona_id IS NOT NULL",
    )
    assert {row[0]: row[1] for row in result["rows"]} == await get_persona_conversation_counts()
    personas = library["personas"]
    assert {row[0]: row[1] for row in result["rows"]} == {personas["Kai"]: 2, personas["Rin"]: 1}


async def test_json_each_self_joins_and_ctes(library):
    tags = await query(
        "SELECT j.value, count(*) AS n FROM characters, json_each(characters.tags) AS j GROUP BY j.value ORDER BY n DESC, j.value"
    )
    assert tags["rows"] == [["Noir", 2], ["Harbour", 1]]
    siblings = await query("SELECT count(*) FROM messages AS a JOIN messages AS b ON b.parent_id = a.id")
    assert siblings["rows"] == [[2]]
    joined = await query(
        "SELECT c.name, count(*) FROM conversations AS v JOIN characters AS c ON c.id = v.character_card_id "
        "GROUP BY c.id ORDER BY 2 DESC"
    )
    assert joined["rows"] == [["Ivo", 2], ["Mara", 1]]
    cte = await query("WITH r AS (SELECT name FROM characters) SELECT count(*) FROM r")
    assert cte["rows"] == [[2]]
    recursive = await query("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r")
    assert recursive["rows"] == [[1], [2], [3]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM character_cards",
        "SELECT content FROM main.messages",
        "SELECT rowid FROM main.character_cards",
        "SELECT avatar_b64 FROM character_cards",
        "SELECT avatar_b64 FROM characters",
        "SELECT system_prompt FROM characters",
        "SELECT api_key FROM endpoints",
        "SELECT 1 FROM endpoints",
        "SELECT name FROM characters WHERE (SELECT api_key FROM endpoints) IS NOT NULL",
        "SELECT * FROM settings",
        "SELECT sql FROM sqlite_schema",
        "SELECT count(*) FROM sqlite_master",
        "SELECT count(*) FROM sqlite_temp_master",
        "SELECT count(*) FROM model_configs",
        "SELECT sql FROM sqlite_temp_schema",
        "SELECT * FROM pragma_table_info('endpoints')",
        # A CTE is attributed by its name exactly as a view is.
        "WITH characters AS (SELECT url, api_key FROM endpoints) SELECT * FROM characters",
        "WITH messages AS (SELECT sql FROM sqlite_schema) SELECT * FROM messages",
        "WITH characters AS (SELECT system_prompt FROM main.character_cards) SELECT * FROM characters",
        "WITH conversations AS (SELECT count(*) FROM endpoints) SELECT * FROM conversations",
        "WITH user_personas AS (SELECT writer_draft FROM main.messages) SELECT count(*) FROM user_personas",
        "SELECT load_extension('evil')",
        "DELETE FROM messages",
        "DELETE FROM main.messages",
        "UPDATE conversations SET title = 'x'",
        "INSERT INTO main.messages (conversation_id, role, content, turn_index, created_at) VALUES ('x', 'user', 'x', 0, '')",
        "CREATE TEMP TABLE scratch (a)",
        "DROP VIEW characters",
        "ATTACH DATABASE 'other.db' AS other",
        "PRAGMA table_info(endpoints)",
        "BEGIN",
        "VACUUM INTO 'copy.db'",
        "SELECT 1; SELECT 2",
        "SELECT randomblob(2000000)",
    ],
)
async def test_everything_else_is_refused(library, sql):
    result = await query(sql)
    assert set(result) == {"error"} and result["error"]
    assert "SECRET" not in result["error"] and "AVATAR" not in result["error"]


async def test_refusals_leave_the_library_intact(library, db):
    await query("DELETE FROM messages")
    async with db.execute("SELECT count(*) FROM messages") as cursor:
        assert (await cursor.fetchone())[0] == 3


async def test_runaway_query_is_interrupted(library):
    result = await query(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
        time_limit_s=0.05,
    )
    assert "time limit" in result["error"]


async def test_row_cell_and_total_caps(library):
    rows = await query(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 10) SELECT n FROM r", max_rows=4
    )
    assert rows["rows"] == [[1], [2], [3], [4]] and rows["more_rows"] is True
    cell = await query("SELECT content || printf('%.11c', '!') FROM messages WHERE role = 'user'", max_cell_chars=12)
    assert cell["rows"] == [["I lean on th…[+18 chars]"]]
    total = await query(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 40) SELECT printf('%.90c', 'y') FROM r",
        max_result_chars=500,
    )
    assert 1 <= len(total["rows"]) < 40 and total["more_rows"] is True
    oversized = await query("SELECT printf('%.900c', 'z')", max_cell_chars=2000, max_result_chars=100)
    assert len(oversized["rows"]) == 1


async def test_sql_errors_are_returned_not_raised(library):
    assert "syntax error" in (await query("SELEC name FROM characters"))["error"]
    assert "no such column" in (await query("SELECT nope FROM characters"))["error"]
