"""Reset-to-defaults must not desync the workflow attachment cache.

``reset_to_defaults`` rebuilds the settings row (DELETE + re-seed) but RETAINS
every ``workflow_attachments`` row. The two attachment-cache bookkeeping columns
describe those retained rows, so they are carried across the rebuild rather than
snapped back to schema defaults:

  - ``attachment_access_counter`` is the monotonic LRU-3 clock. If it reset to 0
    while retained rows still held ``recent_accesses`` from the old counter
    space, eviction order would invert (old artifacts protected, new ones
    evicted first).
  - ``attachment_cache_budget_bytes`` is the cache size limit.

These tests pin that retention while confirming reset still clears the data it
is supposed to (settings + seedable fragments).
"""

from __future__ import annotations

import json

import pytest

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    reset_to_defaults,
    set_active_leaf,
)
from backend.workflows.attachment_cache import record_access

from ._fixtures import make_workflow, register_for_test


@pytest.fixture(autouse=True)
def _register_wf_workflow():
    wf = make_workflow(
        "wf",
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(wf):
        yield


async def _seed_attachment(client) -> int:
    resp = await client.post("/api/conversations", json={"title": "Reset test"})
    assert resp.status_code == 200
    cid = resp.json()["id"]
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    att = {"filename": "x", "mime": "application/octet-stream", "data": b"payload", "workflow_id": "wf"}
    return await insert_workflow_attachment_row(mid, att)


async def test_reset_preserves_access_counter_and_budget(client, db):
    att_id = await _seed_attachment(client)

    # Tune the budget and advance the LRU clock so both diverge from the
    # schema defaults reset would otherwise restore.
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
    retained_max = max(json.loads(ra_row["recent_accesses"]))
    assert counter >= retained_max


async def test_reset_retains_attachment_rows_and_clears_settings(client, db):
    att_id = await _seed_attachment(client)
    # Mutate a setting that reset is supposed to restore.
    await db.execute("UPDATE settings SET temperature = 1.99 WHERE id = 1")
    await db.commit()

    await reset_to_defaults()

    # The attachment row survives.
    rows = list(await db.execute_fetchall("SELECT id FROM workflow_attachments WHERE id = ?", (att_id,)))
    assert len(rows) == 1
    # The tuned setting is back to its default (i.e. not 1.99).
    temp = list(await db.execute_fetchall("SELECT temperature FROM settings WHERE id = 1"))[0]["temperature"]
    assert temp != pytest.approx(1.99)
