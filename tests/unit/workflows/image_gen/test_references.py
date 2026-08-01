"""Which image an edit workflow's `LoadImage` slot actually gets fed.

The failure this guards is silent and expensive: a reference resolved off the
wrong row produces a picture of the wrong person, which reads as a bad model
rather than a bad lookup. So the walk-back rules -- active sibling, evicted rows,
the excluded anchor -- are pinned here rather than left to the end-to-end path.
"""

from __future__ import annotations

import base64

import pytest

from backend.workflows.image_gen import references as refs
from backend.workflows.image_gen.engine.contracts import ImageGenerationError

PNG = b"\x89PNG\r\n\x1a\n" + b"first"
OTHER = b"\x89PNG\r\n\x1a\n" + b"second"
AVATAR = b"\x89PNG\r\n\x1a\n" + b"avatar"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _gen(att_id: int, data: bytes = PNG, **extra) -> dict:
    return {
        "id": att_id,
        "workflow_id": "image_gen",
        "mime_type": "image/png",
        "data_b64": _b64(data),
        "parent_attachment_id": None,
        "active_sibling_id": None,
        **extra,
    }


def _msg(msg_id: int, *, workflow=(), user=()) -> dict:
    return {"id": msg_id, "workflow_attachments": list(workflow), "user_attachments": list(user)}


def _upload(att_id: int, data: bytes) -> dict:
    return {"id": att_id, "mime_type": "image/jpeg", "data_b64": _b64(data)}


def _entries(source: str = "previous", node: str = "72") -> list[dict]:
    return [{"slot": [node, "image"], "source": source, "label": f"Load Image (#{node})"}]


async def _resolve(entries, history, anchor_id=99, character_id=None):
    return await refs.resolve_references(entries, history=history, anchor_id=anchor_id, character_id=character_id)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Nothing here should reach the database unless a test says so."""

    async def unavailable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(refs, "get_character_avatar", unavailable)
    monkeypatch.setattr(refs, "get_workflow_character_state", unavailable)


@pytest.mark.asyncio
async def test_no_mapped_slots_resolves_to_nothing():
    """A plain text-to-image graph must not pay for any of this."""
    assert await _resolve([], []) == ()


# Every rule the walk back applies, as one table. `anchor` is the message being
# visualized, and `origin` is the row those rules must land on.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history", "anchor", "origin"),
    [
        # Reroll siblings share a group root; active_sibling_id names the one on
        # screen. Taking the newest would reference an image the user swiped away.
        ([_msg(1, workflow=[_gen(10, active_sibling_id=10), _gen(11, OTHER, parent_attachment_id=10)])], 99, "attachment:10"),
        ([_msg(1, workflow=[_gen(10), _gen(11, OTHER, parent_attachment_id=10)])], 99, "attachment:11"),
        # An evicted row holds a sentinel, not bytes, so the walk keeps going.
        ([_msg(1, workflow=[_gen(10)]), _msg(2, workflow=[_gen(20) | {"data_b64": "[evicted]"}])], 99, "attachment:10"),
        # The anchor is excluded, or a regenerate would edit the render already on
        # the message instead of the scene, drifting further from the reply each pass.
        ([_msg(1, workflow=[_gen(10)]), _msg(2, workflow=[_gen(20, OTHER)])], 2, "attachment:10"),
        # A user upload counts, and its origin carries the message id too, since
        # user attachments are only readable per-message.
        ([_msg(1, user=[_upload(5, OTHER)])], 99, "upload:1:5"),
        ([_msg(1, workflow=[_gen(10)], user=[_upload(5, OTHER)])], 99, "attachment:10"),
    ],
    ids=["active sibling", "newest sibling", "evicted skipped", "anchor excluded", "upload counts", "generated wins"],
)
async def test_the_walk_back_lands_on_the_image_the_user_is_looking_at(history, anchor, origin):
    resolved = await _resolve(_entries(), history, anchor_id=anchor)
    assert (resolved[0].origin, resolved[0].slot) == (origin, ("72", "image"))


@pytest.mark.asyncio
async def test_both_sources_empty_names_the_slot_and_what_was_tried():
    with pytest.raises(ImageGenerationError) as raised:
        await _resolve(_entries("previous_or_character"), [_msg(1)], character_id="card-1")
    message = str(raised.value)
    assert "Load Image (#72)" in message
    assert "previous image" in message and "character reference" in message


@pytest.mark.asyncio
async def test_two_slots_sharing_a_source_resolve_to_one_upload(monkeypatch):
    async def avatar(_card_id):
        return AVATAR, "image/png"

    monkeypatch.setattr(refs, "get_character_avatar", avatar)
    entries = _entries("character", "72") + _entries("character", "90")

    resolved = await _resolve(entries, [], character_id="card-1")

    # One digest, so the adapter uploads one file and patches both slots with it.
    assert len(resolved) == 2
    assert resolved[0].digest == resolved[1].digest


# ── replay ───────────────────────────────────────────────────────────────────

RECORDED = [{"slot": ["72", "image"], "source": "previous", "origin": "attachment:10", "digest": "x"}]


@pytest.mark.asyncio
async def test_a_reroll_refetches_strictly_by_recorded_origin(monkeypatch):
    async def by_id(att_id):
        assert att_id == 10
        return {"id": 10, "mime_type": "image/png", "data_b64": _b64(PNG)}

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", by_id)

    resolved = await refs.refetch_references(RECORDED)

    assert (resolved[0].data, resolved[0].origin) == (PNG, "attachment:10")


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"id": 10, "mime_type": "image/png", "data_b64": "[evicted]"}])
async def test_a_gone_or_evicted_origin_fails_rather_than_substituting(monkeypatch, row):
    """A reroll promises the same picture with a different seed. Re-resolving
    would hand back a different subject and report success."""

    async def lookup(_att_id):
        return row

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", lookup)

    with pytest.raises(ImageGenerationError, match="cannot be reproduced exactly"):
        await refs.refetch_references(RECORDED)


@pytest.mark.asyncio
async def test_a_character_origin_rereads_the_current_profile(monkeypatch):
    """The origin addresses a setting, not a chat message, so "change the
    character reference, then reroll" does what it says."""

    async def state(card_id, workflow_id):
        assert (card_id, workflow_id) == ("card-1", "image_gen")
        return {"reference_image_b64": _b64(OTHER), "reference_mime": "image/png"}

    monkeypatch.setattr(refs, "get_workflow_character_state", state)

    resolved = await refs.refetch_references([{"slot": ["72", "image"], "source": "character", "origin": "character:card-1"}])

    assert resolved[0].data == OTHER


@pytest.mark.asyncio
async def test_nothing_recorded_replays_as_no_references():
    assert await refs.refetch_references(None) == ()
    assert await refs.refetch_references([]) == ()
