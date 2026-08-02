"""Which image an edit workflow's `LoadImage` slot actually gets fed.

The failure this guards is silent and expensive: a reference resolved off the wrong
row produces a picture of the wrong person, which reads as a bad model rather than
a bad lookup. So the walk-back rules -- active sibling, evicted rows, the excluded
anchor -- are pinned here rather than left to the end-to-end path.
"""

from __future__ import annotations

import base64
import hashlib
import io

import pytest
from PIL import Image

from backend.workflows.image_gen import references as refs
from backend.workflows.image_gen.engine.contracts import ImageGenerationError

PNG = b"\x89PNG\r\n\x1a\n" + b"first"
OTHER = b"\x89PNG\r\n\x1a\n" + b"second"
AVATAR = b"\x89PNG\r\n\x1a\n" + b"avatar"

# The walk-back tests only need distinguishable bytes, so the fakes above stay
# fakes; the policy tests below re-encode, so they need bytes PIL can read.
CLOUD_SLOTS = [
    {
        "slot": ["cloud", "image_0"],
        "source": "previous",
        "label": "Reference image",
        "mimes": ["image/png", "image/jpeg"],
        "max_bytes": 4 * 1024 * 1024,
        "required": False,
    }
]
COMFY_SLOTS = [
    {
        "slot": ["72", "image"],
        "source": "previous",
        "label": "Load Image (#72)",
        "mimes": ["image/png", "image/jpeg", "image/webp"],
        "max_bytes": 8 * 1024 * 1024,
        "required": True,
    }
]


def _real(fmt: str = "WEBP") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (12, 34, 56)).save(buf, format=fmt)
    return buf.getvalue()


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


def _upload(att_id: int, data: bytes, mime: str = "image/jpeg") -> dict:
    return {"id": att_id, "mime_type": mime, "data_b64": _b64(data)}


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
        # A phone HEIC or an SVG is `image/*` and is not something any image backend
        # can be handed, so it is walked past like an evicted row.
        ([_msg(1, workflow=[_gen(10)]), _msg(2, user=[_upload(5, OTHER, "image/heic")])], 99, "attachment:10"),
        # The anchor is excluded, or a regenerate would edit the render already on
        # the message instead of the scene, drifting further from the reply each pass.
        ([_msg(1, workflow=[_gen(10)]), _msg(2, workflow=[_gen(20, OTHER)])], 2, "attachment:10"),
        # A user upload counts, and its origin carries the message id too, since
        # user attachments are only readable per-message.
        ([_msg(1, user=[_upload(5, OTHER)])], 99, "upload:1:5"),
        ([_msg(1, workflow=[_gen(10)], user=[_upload(5, OTHER)])], 99, "attachment:10"),
    ],
    ids=[
        "active sibling",
        "newest sibling",
        "evicted skipped",
        "unreadable mime skipped",
        "anchor excluded",
        "upload counts",
        "generated wins",
    ],
)
async def test_the_walk_back_lands_on_the_image_the_user_is_looking_at(history, anchor, origin):
    resolved = await _resolve(_entries(), history, anchor_id=anchor)
    assert (resolved[0].origin, resolved[0].slot) == (origin, ("72", "image"))


@pytest.mark.asyncio
async def test_the_walk_back_stops_before_the_whole_branch():
    """Unbounded, the first image in a conversation permanently retires the
    character reference: `previous_or_character` would find something forever, and a
    picture from two hundred messages ago is a worse likeness than the one the user
    set on purpose."""
    ancient = [_msg(1, workflow=[_gen(10)])]
    # The image sits one message beyond the window: the walk scans `since` first,
    # so reaching it costs len(since) + 1 steps.
    since = [_msg(i) for i in range(2, 2 + refs.PREVIOUS_LOOKBACK_MESSAGES)]

    assert refs._previous_image(ancient + since, 99) is None
    # One message closer and it is back in range, so the bound is the only reason.
    assert refs._previous_image(ancient + since[:-1], 99) is not None


@pytest.mark.asyncio
async def test_only_a_required_slot_fails_when_nothing_resolves():
    """A cloud provider's synthetic slot is optional: the same model has a plain
    generations endpoint one field away, so refusing would make turning reference
    images on break every new conversation until an image exists in it. A ComfyUI
    graph slot is not -- rendering it unfilled submits the exporter's own filename."""
    assert await _resolve([{**CLOUD_SLOTS[0], "source": "previous"}], [_msg(1)]) == ()

    with pytest.raises(ImageGenerationError):
        await _resolve([{**CLOUD_SLOTS[0], "required": True}], [_msg(1)])

    # And the message names the slot and every source that was tried.
    with pytest.raises(ImageGenerationError) as raised:
        await _resolve(_entries("previous_or_character"), [_msg(1)], character_id="card-1")
    assert "Load Image (#72)" in str(raised.value)
    assert "previous image" in str(raised.value) and "character reference" in str(raised.value)


@pytest.mark.asyncio
async def test_two_slots_sharing_a_source_resolve_to_one_upload(monkeypatch):
    async def avatar(_card_id):
        return AVATAR, "image/png"

    monkeypatch.setattr(refs, "get_character_avatar", avatar)

    resolved = await _resolve(_entries("character", "72") + _entries("character", "90"), [], character_id="card-1")

    # One digest, so the adapter uploads one file and patches both slots with it.
    assert len(resolved) == 2
    assert resolved[0].digest == resolved[1].digest


# ── replay ───────────────────────────────────────────────────────────────────

RECORDED = [{"slot": ["72", "image"], "source": "previous", "origin": "attachment:10", "digest": "x"}]


@pytest.fixture
def _stored_webp(monkeypatch):
    """One recorded reference whose origin still holds a real WebP."""

    async def by_id(_att_id):
        return {"id": 10, "mime_type": "image/webp", "data_b64": _b64(_real("WEBP"))}

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", by_id)


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
    """A reroll promises the same picture with a different seed. Re-resolving would
    hand back a different subject and report success."""

    async def lookup(_att_id):
        return row

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", lookup)

    with pytest.raises(ImageGenerationError, match="cannot be reproduced exactly"):
        await refs.refetch_references(RECORDED)


@pytest.mark.asyncio
async def test_nothing_recorded_replays_as_no_references():
    assert await refs.refetch_references(None) == ()
    assert await refs.refetch_references([]) == ()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stored_webp")
@pytest.mark.parametrize(
    ("recorded_slot", "slots", "expected_slot"),
    [
        (["72", "image"], CLOUD_SLOTS, ("cloud", "image_0")),
        (["cloud", "image_0"], COMFY_SLOTS, ("72", "image")),
        # A graph re-exported with different node ids is the same problem in one
        # backend, and gets the same answer.
        (["72", "image"], [{**COMFY_SLOTS[0], "slot": ["81", "image"]}], ("81", "image")),
    ],
    ids=["comfy -> cloud", "cloud -> comfy", "graph re-exported"],
)
async def test_a_replay_rekeys_onto_the_slot_that_will_actually_carry_it(recorded_slot, slots, expected_slot):
    """A ComfyUI reference is recorded against a node id and a cloud one against a
    synthetic slot, so the two never match by key. Without the positional pass the
    cloud direction handed a provider that declared PNG/JPEG the WebP every render
    is stored as, and the ComfyUI direction patched a graph node named `cloud`."""
    recorded = [{"slot": recorded_slot, "source": "previous", "origin": "attachment:10", "digest": "x"}]

    resolved = await refs.refetch_references(recorded, slots=slots)

    assert resolved[0].slot == expected_slot
    assert resolved[0].mime in tuple(slots[0]["mimes"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stored_webp")
async def test_a_recorded_reference_with_no_slot_left_is_dropped_not_submitted():
    """The caller counts what came back and discloses the drop. Submitting it against
    a slot this render does not have is what patched a graph node named `cloud`."""
    recorded = [
        {"slot": ["cloud", "image_0"], "source": "previous", "origin": "attachment:10"},
        {"slot": ["cloud", "image_1"], "source": "previous", "origin": "attachment:10"},
    ]

    assert len(await refs.refetch_references(recorded, slots=CLOUD_SLOTS)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stored_webp")
async def test_a_required_slot_with_nothing_recorded_for_it_refuses_the_replay():
    required = [{**CLOUD_SLOTS[0], "required": True, "label": "Load Image (#r)"}]

    with pytest.raises(ImageGenerationError, match="did not record"):
        await refs.refetch_references([], slots=required)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stored_webp")
async def test_an_origin_whose_bytes_were_replaced_refuses_rather_than_substituting():
    """Rehydrate rewrites an evicted row *in place*, and on a seedless provider those
    are different bytes under the same id. Compared on `source_digest`, taken before
    this target's mime policy converts anything -- `digest` would differ for a
    re-keyed reference that never changed."""
    recorded = [
        {
            "slot": ["cloud", "image_0"],
            "source": "previous",
            "origin": "attachment:10",
            "source_digest": hashlib.sha256(b"what it used to be").hexdigest(),
        }
    ]

    with pytest.raises(ImageGenerationError, match="has been replaced"):
        await refs.refetch_references(recorded, slots=CLOUD_SLOTS)


@pytest.mark.asyncio
async def test_a_character_origin_rereads_the_current_profile_and_is_exempt_from_that_check(monkeypatch):
    """It addresses a *setting*, not a chat row, so "change the character reference,
    then reroll" is documented to apply -- a changed digest there is the point."""

    async def state(card_id, workflow_id):
        assert (card_id, workflow_id) == ("card-1", "image_gen")
        return {"reference_image_b64": _b64(_real("PNG")), "reference_mime": "image/png"}

    monkeypatch.setattr(refs, "get_workflow_character_state", state)
    recorded = [
        {
            "slot": ["cloud", "image_0"],
            "source": "character",
            "origin": "character:card-1",
            "source_digest": hashlib.sha256(b"the previous likeness").hexdigest(),
        }
    ]

    assert len(await refs.refetch_references(recorded, slots=CLOUD_SLOTS)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stored_webp")
async def test_a_replay_records_the_bytes_it_actually_sent():
    """`digest` names the bytes on the wire (ComfyUI dedupes uploads by it);
    `source_digest` names the bytes as fetched. A conversion makes them differ, and
    only the second is comparable across renders."""
    recorded = [{"slot": ["cloud", "image_0"], "source": "previous", "origin": "attachment:10"}]

    resolved = await refs.refetch_references(recorded, slots=CLOUD_SLOTS)

    assert resolved[0].source_digest == hashlib.sha256(_real("WEBP")).hexdigest()
    assert resolved[0].digest != resolved[0].source_digest
