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


def _slots(source: str = "previous", node: str = "72") -> dict:
    return {"references": [{"slot": [node, "image"], "source": source, "label": f"Load Image (#{node})"}]}


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
    assert await refs.resolve_references({}, history=[], anchor_id=1, character_id=None) == ()


@pytest.mark.asyncio
async def test_the_walk_back_picks_the_sibling_the_user_is_looking_at():
    # Reroll siblings share a group root; active_sibling_id names the one on
    # screen. Taking the newest instead would reference an image the user
    # explicitly swiped away from.
    root = _gen(10, PNG, active_sibling_id=10)
    sibling = _gen(11, OTHER, parent_attachment_id=10)
    history = [_msg(1, workflow=[root, sibling]), _msg(2)]

    resolved = await refs.resolve_references(_slots(), history=history, anchor_id=2, character_id=None)

    assert resolved[0].data == PNG
    assert resolved[0].origin == "attachment:10"
    assert resolved[0].slot == ("72", "image")


@pytest.mark.asyncio
async def test_a_group_with_no_active_sibling_uses_the_newest():
    history = [_msg(1, workflow=[_gen(10, PNG), _gen(11, OTHER, parent_attachment_id=10)]), _msg(2)]
    resolved = await refs.resolve_references(_slots(), history=history, anchor_id=2, character_id=None)
    assert resolved[0].origin == "attachment:11"


@pytest.mark.asyncio
async def test_an_evicted_row_is_skipped_for_the_next_usable_image():
    evicted = _gen(20, data=PNG)
    evicted["data_b64"] = "[evicted]"
    history = [_msg(1, workflow=[_gen(10, PNG)]), _msg(2, workflow=[evicted]), _msg(3)]

    resolved = await refs.resolve_references(_slots(), history=history, anchor_id=3, character_id=None)

    assert resolved[0].origin == "attachment:10"


@pytest.mark.asyncio
async def test_the_anchor_message_cannot_reference_its_own_image():
    """Otherwise a regenerate edits the render already on the message instead of
    the scene, and each pass drifts further from what the reply describes."""
    history = [_msg(1, workflow=[_gen(10, PNG)]), _msg(2, workflow=[_gen(20, OTHER)])]
    resolved = await refs.resolve_references(_slots(), history=history, anchor_id=2, character_id=None)
    assert resolved[0].origin == "attachment:10"


@pytest.mark.asyncio
async def test_a_user_upload_counts_as_a_previous_image():
    upload = {"id": 5, "mime_type": "image/jpeg", "data_b64": _b64(OTHER)}
    history = [_msg(1, user=[upload]), _msg(2)]

    resolved = await refs.resolve_references(_slots(), history=history, anchor_id=2, character_id=None)

    # User attachments are only readable per-message, so the origin carries both ids.
    assert resolved[0].origin == "upload:1:5"
    assert resolved[0].data == OTHER


@pytest.mark.asyncio
async def test_a_generated_image_beats_an_upload_on_the_same_message():
    upload = {"id": 5, "mime_type": "image/jpeg", "data_b64": _b64(OTHER)}
    history = [_msg(1, workflow=[_gen(10, PNG)], user=[upload]), _msg(2)]
    resolved = await refs.resolve_references(_slots(), history=history, anchor_id=2, character_id=None)
    assert resolved[0].origin == "attachment:10"


@pytest.mark.asyncio
async def test_the_character_source_falls_back_to_the_card_avatar(monkeypatch):
    async def avatar(card_id):
        assert card_id == "card-1"
        return AVATAR, "image/png"

    monkeypatch.setattr(refs, "get_character_avatar", avatar)

    resolved = await refs.resolve_references(_slots("character"), history=[], anchor_id=1, character_id="card-1")

    assert resolved[0].data == AVATAR
    assert resolved[0].origin == "character:card-1"


@pytest.mark.asyncio
async def test_an_explicit_character_reference_beats_the_avatar(monkeypatch):
    async def avatar(_card_id):
        return AVATAR, "image/png"

    monkeypatch.setattr(refs, "get_character_avatar", avatar)
    profile = {"reference_image_b64": _b64(OTHER), "reference_mime": "image/png"}

    resolved = await refs.resolve_references(
        _slots("character"), history=[], anchor_id=1, character_id="card-1", profile=profile
    )

    assert resolved[0].data == OTHER


@pytest.mark.asyncio
async def test_the_combined_source_prefers_the_previous_image(monkeypatch):
    async def avatar(_card_id):
        return AVATAR, "image/png"

    monkeypatch.setattr(refs, "get_character_avatar", avatar)
    history = [_msg(1, workflow=[_gen(10, PNG)]), _msg(2)]

    resolved = await refs.resolve_references(
        _slots("previous_or_character"), history=history, anchor_id=2, character_id="card-1"
    )
    assert resolved[0].data == PNG

    # ...and falls through on the first Visualize of a new conversation, which is
    # the cold-start cliff this source exists to remove.
    cold = await refs.resolve_references(_slots("previous_or_character"), history=[_msg(1)], anchor_id=1, character_id="card-1")
    assert cold[0].data == AVATAR


@pytest.mark.asyncio
async def test_both_sources_empty_names_the_slot_and_what_was_tried():
    with pytest.raises(ImageGenerationError) as raised:
        await refs.resolve_references(_slots("previous_or_character"), history=[_msg(1)], anchor_id=1, character_id="card-1")
    message = str(raised.value)
    assert "Load Image (#72)" in message
    assert "previous image" in message and "character reference" in message


@pytest.mark.asyncio
async def test_two_slots_sharing_a_source_resolve_to_one_upload(monkeypatch):
    async def avatar(_card_id):
        return AVATAR, "image/png"

    monkeypatch.setattr(refs, "get_character_avatar", avatar)
    slots = {
        "references": [
            {"slot": ["72", "image"], "source": "character", "label": "a"},
            {"slot": ["90", "image"], "source": "character", "label": "b"},
        ]
    }

    resolved = await refs.resolve_references(slots, history=[], anchor_id=1, character_id="card-1")

    assert len(resolved) == 2
    # One digest, so the adapter uploads one file and patches both slots with it.
    assert resolved[0].digest == resolved[1].digest


# ── replay ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_reroll_refetches_strictly_by_recorded_origin(monkeypatch):
    async def by_id(att_id):
        assert att_id == 10
        return {"id": 10, "mime_type": "image/png", "data_b64": _b64(PNG)}

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", by_id)
    recorded = [{"slot": ["72", "image"], "source": "previous", "origin": "attachment:10", "digest": "x"}]

    resolved = await refs.refetch_references(recorded)

    assert resolved[0].data == PNG
    assert resolved[0].origin == "attachment:10"


@pytest.mark.asyncio
async def test_a_deleted_origin_fails_rather_than_substituting(monkeypatch):
    """A reroll promises the same picture with a different seed. Re-resolving
    would hand back a different subject and report success."""

    async def gone(_att_id):
        return None

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", gone)
    recorded = [{"slot": ["72", "image"], "source": "previous", "origin": "attachment:10", "digest": "x"}]

    with pytest.raises(ImageGenerationError, match="cannot be reproduced exactly"):
        await refs.refetch_references(recorded)


@pytest.mark.asyncio
async def test_an_evicted_origin_fails_the_same_way(monkeypatch):
    async def evicted(_att_id):
        return {"id": 10, "mime_type": "image/png", "data_b64": "[evicted]"}

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", evicted)
    with pytest.raises(ImageGenerationError):
        await refs.refetch_references([{"slot": ["72", "image"], "source": "previous", "origin": "attachment:10"}])


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
