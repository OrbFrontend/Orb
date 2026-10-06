"""Check that reset retains attachment access counter and budget alongside cached rows, while resetting ordinary settings and
fragments. Resetting the counter would invert eviction order across old and new artifacts.
"""

import json

import pytest

from backend.database import DEFAULT_SETTINGS, add_message, insert_workflow_attachment_row, reset_to_defaults, set_active_leaf
from backend.workflows.attachment_cache import record_access

from ._fixtures import registered_artifact_workflow


@pytest.fixture(autouse=True)
def _register_wf_workflow():
    with registered_artifact_workflow():
        yield


async def _seed_attachment(client) -> int:
    cid = await client.create("/api/conversations", json={"title": "Reset test"})
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    att = {"filename": "x", "mime": "application/octet-stream", "data": b"payload", "workflow_id": "wf"}
    return await insert_workflow_attachment_row(mid, att)


async def test_reset_preserves_access_counter_and_budget(client, db):
    att_id = await _seed_attachment(client)

    # Tune the budget and advance the LRU clock so both diverge from the schema defaults reset would otherwise restore.
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = ? WHERE id = 1", (12345,))
    await db.commit()
    for _ in range(5):
        await record_access([att_id])

    before = list(
        await db.execute_fetchall("SELECT attachment_cache_budget_bytes, attachment_access_counter FROM settings WHERE id = 1")
    )[0]
    assert before["attachment_cache_budget_bytes"] == 12345
    assert before["attachment_access_counter"] == 5

    await reset_to_defaults()

    after = list(
        await db.execute_fetchall("SELECT attachment_cache_budget_bytes, attachment_access_counter FROM settings WHERE id = 1")
    )[0]
    assert after["attachment_cache_budget_bytes"] == 12345
    assert after["attachment_access_counter"] == 5


async def test_reset_keeps_counter_above_retained_recent_accesses(client, db):
    """The carried counter stays >= every retained row's recent_accesses, so a
    post-reset access assigns a strictly larger value and LRU-3 ordering holds."""
    att_id = await _seed_attachment(client)
    for _ in range(5):
        await record_access([att_id])

    await reset_to_defaults()

    counter = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    ra_row = list(await db.execute_fetchall("SELECT recent_accesses FROM workflow_attachments WHERE id = ?", (att_id,)))[0]
    assert ra_row["recent_accesses"] is not None
    assert counter >= max(json.loads(ra_row["recent_accesses"]))


async def test_reset_retains_attachment_rows_and_clears_settings(client, db):
    att_id = await _seed_attachment(client)
    # Mutate a setting that reset is supposed to restore.
    await db.execute("UPDATE settings SET length_guard_max_words = 999 WHERE id = 1")
    await db.commit()

    await reset_to_defaults()

    # The attachment row survives.
    assert len(list(await db.execute_fetchall("SELECT id FROM workflow_attachments WHERE id = ?", (att_id,)))) == 1
    # The tuned setting is back to its default.
    words = list(await db.execute_fetchall("SELECT length_guard_max_words FROM settings WHERE id = 1"))[0]
    assert words["length_guard_max_words"] == DEFAULT_SETTINGS["length_guard_max_words"]
