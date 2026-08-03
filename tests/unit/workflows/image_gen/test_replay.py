"""Reroll and rehydrate must reproduce the image the row records, not the style.

The failure this guards is silent: resolving replay through the style renders an
old attachment on whatever checkpoint that style points at *today*, and for
rehydrate -- which promises to restore evicted bytes -- that overwrites the row
with a different image and reports success.
"""

from __future__ import annotations

import base64

import pytest

from backend.workflows.image_gen import hooks
from backend.workflows.image_gen.config import normalize_config, resolve_style
from backend.workflows.image_gen.engine import ImageGenerationError, get_adapter

GRAPH = {
    "0": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
    "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
}
SLOTS = {"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]}


def _config(default_style: str = "anime", **external) -> dict:
    base = {
        "styles": [{"id": "anime", "label": "Anime", "checkpoint": "current.safetensors"}],
    }
    base.update(external)
    return normalize_config({"default_style": default_style, "external_comfy": base})


def _target(config: dict, style_id: str, replay: dict | None = None):
    """What the render path resolves: the style's adapter, asked about that style."""
    style = resolve_style(config, style_id)
    return get_adapter(config, style).resolve_target(replay)


def test_a_fresh_render_follows_the_style():
    # The style pins no workflow and external mode ships no default graph, so the
    # target graph is empty; the adapter turns that into an "assign a workflow" error.
    target = _target(_config(), "anime")
    assert (target.source, target.target_id, target.model, target.notes) == ("external_comfy", "", "current.safetensors", ())
    # No graph, so no mapped size slots: the workflow decides, exactly as before the
    # slot existed. Orb pins a resolution only where it can actually write one.
    assert (target.supports_dimensions, target.width, target.height) == (False, None, None)


def test_a_graph_mapping_size_slots_takes_the_styles_resolution():
    sized = {
        "id": "user_sized",
        "label": "Sized",
        "graph": {**GRAPH, "l": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}}},
        "slots": {**SLOTS, "width": ["l", "width"], "height": ["l", "height"]},
    }
    config = _config(
        user_graphs=[sized, {"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}],
        styles=[
            {"id": "anime", "label": "Anime", "workflow": "user_sized", "width": 1024, "height": 1536},
            {"id": "own", "label": "Own", "workflow": "user_a", "width": 1024, "height": 1536},
        ],
    )
    sized_target = _target(config, "anime")
    assert (sized_target.supports_dimensions, sized_target.width, sized_target.height) == (True, 1024, 1536)
    assert sized_target.notes == ()

    # The same style setting against a graph that maps nothing: inert, and disclosed
    # rather than left to be noticed, because the picker still shows the resolution.
    own = _target(config, "own")
    assert (own.supports_dimensions, own.width, own.height) == (False, None, None)
    assert any("decides its own output size" in note for note in own.notes)


def test_a_replay_pins_the_resolution_it_was_generated_at():
    """The substitution rehydrate exists to avoid, now reachable on ComfyUI too:
    once Orb can write the size, reading today's picker hands a rehydrate an image of
    a different shape than the one it promised to restore."""
    sized = {
        "id": "user_sized",
        "label": "Sized",
        "graph": {**GRAPH, "l": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}}},
        "slots": {**SLOTS, "width": ["l", "width"], "height": ["l", "height"]},
    }
    config = _config(
        user_graphs=[sized],
        styles=[{"id": "anime", "label": "Anime", "workflow": "user_sized", "width": 1536, "height": 1024}],
    )
    target = _target(config, "anime", {"workflow_id": "user_sized", "width": 1024, "height": 1024})
    assert (target.width, target.height) == (1024, 1024)


@pytest.mark.parametrize(
    ("replay", "expected"),
    [
        ({"workflow_id": "user_a", "backend_model": "old.safetensors"}, ("user_a", "old.safetensors")),
        # `backend_model` is null when the original ran a graph carrying its own
        # loaders, so there is no pin to restore -- inventing one would be worse.
        ({"workflow_id": "user_a", "backend_model": None}, ("user_a", "current.safetensors")),
    ],
    ids=["recorded pins win", "no recorded model falls through to the style"],
)
def test_replay_prefers_what_the_stored_image_recorded(replay, expected):
    config = _config(user_graphs=[{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}])
    target = _target(config, "anime", replay)
    assert (target.target_id, target.model) == expected
    assert target.notes == ()


def test_replay_of_a_deleted_graph_degrades_with_disclosure():
    target = _target(_config(), "anime", {"workflow_id": "user_gone", "backend_model": "old.safetensors"})
    # The style has no workflow to fall back to, so the target is empty and the
    # note discloses both the missing graph and the unconfigured style.
    assert (target.target_id, target.model) == ("", "old.safetensors")
    assert len(target.notes) == 1
    assert "user_gone" in target.notes[0]


# ── reference images on reroll ───────────────────────────────────────────────

EDIT_GRAPH = {**GRAPH, "r": {"class_type": "LoadImage", "inputs": {"image": "exported.png"}}}
EDIT_SLOTS = {**SLOTS, "references": [{"slot": ["r", "image"], "source": "character", "label": "Load Image (#r)"}]}


class _RerollCtx:
    def __init__(self, prior_style: str, *, stored_seed: str = "1234", replay: bool = False):
        self.prior_consumption_metadata = {"style_id": prior_style}
        self.original_attachment = {"seed": stored_seed}
        # What the route declares, and the only thing this hook branches on:
        # False is /reroll-gen (render on today's style), True is /rehydrate
        # (reproduce what the row recorded).
        self.replay = replay


@pytest.fixture
def _edit_config(monkeypatch):
    config = _config(
        default_style="edit",
        user_graphs=[{"id": "user_edit", "label": "Edit", "graph": EDIT_GRAPH, "slots": EDIT_SLOTS}],
        styles=[{"id": "edit", "label": "Edit", "workflow": "user_edit"}, {"id": "plain", "label": "Plain"}],
    )

    async def get_config(_workflow_id):
        return config

    monkeypatch.setattr(hooks, "get_workflow_config", get_config)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_edit_config")
async def test_a_style_swap_on_reroll_ignores_the_stale_graph_pins():
    """`workflow_id` and `backend_model` name things the OLD style owned, so the
    new style must resolve its own. `references` is not one of them -- it records
    an *origin*, which re-fetches with no history and no graph."""
    # The override swapped this reroll from the edit style onto a plain one.
    params = {
        "prompt": "a quiet room",
        "negative_prompt": "",
        "style_id": "plain",
        "workflow_id": "user_edit",
        "references": [{"slot": ["r", "image"], "source": "character", "origin": "character:card-1"}],
    }

    # The plain style pins no workflow, so the render dies on the normal "assign a
    # workflow" path. The recorded graph is still sitting in `params` and is simply
    # not consulted -- what the sibling records is rewritten from the render that
    # succeeds, so there is nothing to pop on the path that does not.
    with pytest.raises(ImageGenerationError, match="Import a ComfyUI workflow"):
        await hooks.reroll_gen(_RerollCtx("edit"), params, "1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_edit_config")
async def test_rerolling_onto_a_style_needing_an_unrecorded_reference_is_refused():
    """Submitting anyway would ship the new graph's exporter filenames, which
    fails at ComfyUI with nothing the user can act on. The refusal now fires on
    what is actually unreplayable -- a required slot with no recorded origin to
    fill it -- rather than on any style change that touches references at all."""
    params = {"prompt": "p", "negative_prompt": "", "style_id": "edit", "workflow_id": "user_other"}

    with pytest.raises(ImageGenerationError, match="needs a reference image the stored image did not record"):
        await hooks.reroll_gen(_RerollCtx("plain"), params, "1")


# ── which configuration a reroll renders on ──────────────────────────────────
#
# The one thing the two routes backed by this hook disagree about. /rehydrate owes
# the row the image it lost, so it pins what the row recorded; /reroll-gen owes the
# user another variant of the same subject, so it renders on the style as it stands.
# Reading the stored record on both is what made a style's resolution picker inert
# for every image already made -- change it, press the dice, get the old size back.

SIZED_GRAPH = {**GRAPH, "l": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}}}
SIZED_SLOTS = {**SLOTS, "width": ["l", "width"], "height": ["l", "height"]}
# What the parent recorded: an older, smaller render on a checkpoint since replaced.
STORED_COMFY = {"workflow_id": "user_sized", "backend_model": "old.safetensors", "width": 512, "height": 512}


@pytest.fixture
def _sized_comfy(monkeypatch, request):
    """Two ComfyUI styles on one sized graph; yields the resolved target per render.

    `request.param` grades the size the fake reports, as `describe_render_params`
    grades a real one: True for a graph whose size slots are mapped, False for one
    where the value could only be scanned off some node. Defaults to True, which is
    what SIZED_SLOTS below actually describes.
    """
    from backend.workflows.image_gen.engine.contracts import ImageResult

    size_measured = getattr(request, "param", True)

    config = _config(
        user_graphs=[{"id": "user_sized", "label": "Sized", "graph": SIZED_GRAPH, "slots": SIZED_SLOTS}],
        styles=[
            {
                "id": "anime",
                "label": "Anime",
                "workflow": "user_sized",
                "checkpoint": "new.safetensors",
                "width": 1024,
                "height": 1536,
            },
            {
                "id": "other",
                "label": "Other",
                "workflow": "user_sized",
                "checkpoint": "other.safetensors",
                "width": 704,
                "height": 1408,
            },
        ],
    )
    captured: dict = {}

    async def fake_generate(_adapter, request, *, target=None, progress=None):
        captured["target"] = target
        # As the real adapter reports it: read back off the render that executed.
        return ImageResult(
            image_bytes=b"rendered",
            mime="image/png",
            backend_info={
                "source": "external_comfy",
                "workflow_id": target.target_id,
                "backend_model": target.model,
                "width": target.width,
                "height": target.height,
                "size_measured": size_measured,
                "notes": [],
            },
        )

    async def get_config(_workflow_id):
        return config

    monkeypatch.setattr(hooks, "get_workflow_config", get_config)
    monkeypatch.setattr(hooks, "resolve_and_generate", fake_generate)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("style_id", "expected"),
    [("anime", (1024, 1536, "new.safetensors")), ("other", (704, 1408, "other.safetensors"))],
    ids=["same style, resolution since changed", "rerolled onto another style"],
)
async def test_a_reroll_renders_on_the_style_as_it_stands_now(_sized_comfy, style_id, expected):
    """The reported bug. Editing a style's resolution and pressing the dice has to
    render at the new resolution -- on the style being rerolled *and* on one the
    picker has since moved to, which resolves its own everything."""
    params = {"prompt": "a quiet room", "negative_prompt": "", "style_id": style_id, **STORED_COMFY}

    await hooks.reroll_gen(_RerollCtx("anime"), params, "99")

    target = _sized_comfy["target"]
    assert (target.width, target.height, target.model) == expected


@pytest.mark.asyncio
async def test_a_rehydrate_still_pins_what_the_row_recorded(_sized_comfy):
    """The other half: these bytes are meant to *be* the ones the row lost, so
    today's picker must not reshape them."""
    params = {"prompt": "a quiet room", "negative_prompt": "", "style_id": "anime", **STORED_COMFY}

    await hooks.reroll_gen(_RerollCtx("anime", replay=True), params, "1234")

    target = _sized_comfy["target"]
    assert (target.width, target.height, target.model) == (512, 512, "old.safetensors")


@pytest.mark.asyncio
async def test_the_rerolled_sibling_records_the_render_it_actually_got(_sized_comfy):
    """`params` is what the route persists as the sibling's generation_metadata, and
    that sibling is itself rehydratable. Left naming the parent's target, its own
    rehydrate would restore an image it never made."""
    params = {"prompt": "a quiet room", "negative_prompt": "", "style_id": "other", **STORED_COMFY}

    _, consumption = await hooks.reroll_gen(_RerollCtx("anime"), params, "99")

    assert (params["width"], params["height"]) == (704, 1408)
    assert params["backend_model"] == "other.safetensors"
    # And the size reaches the display half, so Render details can show what was drawn
    # rather than leaving a stale one to be noticed by eye.
    assert (consumption["width"], consumption["height"]) == (704, 1408)


@pytest.mark.asyncio
@pytest.mark.parametrize("_sized_comfy", [False], indirect=True)
async def test_a_size_the_backend_only_guessed_at_is_not_shown(_sized_comfy):
    """The display half is a claim about the image, so it takes only a graded answer.

    ComfyUI degrades to scanning the graph for any node carrying a width/height pair
    -- which `test_graph` pins as able to pick an upscale node over the latent one --
    and the row a user would check their picker against is the last place to print a
    guess. The replay half still records it: a best-effort record degrades rather
    than failing, it just does not get to be shown as fact.
    """
    params = {"prompt": "a quiet room", "negative_prompt": "", "style_id": "anime", **STORED_COMFY}

    _, consumption = await hooks.reroll_gen(_RerollCtx("anime"), params, "99")

    assert "width" not in consumption and "height" not in consumption
    assert (params["width"], params["height"]) == (1024, 1536), "still recorded, just not shown"


# ── routing, when the replayed style is not the default one ──────────────────


@pytest.mark.asyncio
async def test_a_replay_routes_on_its_own_style_not_the_configs_default(monkeypatch):
    """The regression this plan is fixing. `normalize_config` derives `source` from
    the *default* style, and `/rehydrate` calls the hook with the attachment's stored
    `style_id` -- whatever the image was originally made with. Routing on `source`
    therefore handed a ComfyUI-linked style to the cloud adapter, which answered
    "Choose a model for xAI" about a style holding a perfectly good checkpoint.

    `/reroll-gen` never showed it because the widget overwrites `style_id` with the
    default style on every reroll; rehydrate does not.
    """
    from backend.workflows.image_gen.engine.contracts import ImageResult

    config = normalize_config(
        {
            "default_style": "remote",
            "styles": [
                {"id": "remote", "connection": "xai"},
                {"id": "local", "connection": "comfy", "workflow": "user_a", "checkpoint": "anime.safetensors"},
            ],
            "external_comfy": {"user_graphs": [{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}]},
            "cloud": {"providers": {"xai": {"api_key": "k"}}},
        }
    )
    assert config["source"] == "cloud", "precondition: the default style routes to the cloud"

    async def get_config(_workflow_id):
        return config

    captured: dict = {}

    async def fake_generate(_adapter, request, *, target=None, progress=None):
        captured["target"] = target
        return ImageResult(image_bytes=b"rendered", mime="image/png", backend_info={"notes": []})

    monkeypatch.setattr(hooks, "get_workflow_config", get_config)
    monkeypatch.setattr(hooks, "resolve_and_generate", fake_generate)

    params = {"prompt": "a quiet room", "negative_prompt": "", "style_id": "local", "source": "external_comfy"}
    _, consumption = await hooks.reroll_gen(_RerollCtx("local"), params, "1")

    assert captured["target"].source == "external_comfy"
    assert (captured["target"].target_id, captured["target"].model) == ("user_a", "anime.safetensors")
    assert consumption["source"] == "External ComfyUI"
    # And nothing claims a backend change, because there was none.
    assert not any("re-rendered on" in note for note in consumption.get("notes", []))


# ── reference images on a cloud reroll ───────────────────────────────────────
#
# The cloud slot is synthetic and constant, so every question the ComfyUI cases
# above answer about node ids has a different answer here.


def _cloud_config(reference_source: str, styles=None) -> dict:
    return normalize_config(
        {
            "source": "cloud",
            "default_style": "anime",
            "styles": styles or [{"id": "anime", "label": "Anime", "connection": "xai"}],
            "cloud": {
                "provider": "xai",
                "reference_source": reference_source,
                "providers": {
                    "xai": {
                        "api_key": "sk-test",
                        "model": "grok-imagine-image",
                        "reference_source": reference_source,
                    }
                },
            },
        }
    )


@pytest.fixture
def _cloud_reroll(monkeypatch):
    """Configure a cloud reroll and capture the request that reaches the adapter."""
    import io

    from PIL import Image

    from backend.workflows.image_gen import references as refs
    from backend.workflows.image_gen.engine.contracts import ImageResult

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="WEBP")
    stored = buf.getvalue()

    async def by_id(_att_id):
        return {"id": 10, "mime_type": "image/webp", "data_b64": base64.b64encode(stored).decode()}

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", by_id)
    captured: dict = {}

    async def fake_generate(_adapter, request, *, target=None, progress=None):
        captured["request"] = request
        captured["target"] = target
        # Mirrors what the real cloud adapter reports about itself, which is the half
        # of the round-trip below that a fake can silently stop doing: it writes these
        # off the *target*, so the record names what rendered rather than what the
        # style says now.
        return ImageResult(
            image_bytes=b"rendered",
            mime="image/webp",
            backend_info={
                "source": "cloud",
                "backend_model": target.model,
                "quality": target.quality,
                "reference_source": target.reference_source,
                "notes": [],
            },
        )

    monkeypatch.setattr(hooks, "resolve_and_generate", fake_generate)

    def _configure(config: dict):
        async def get_config(_workflow_id):
            return config

        monkeypatch.setattr(hooks, "get_workflow_config", get_config)
        return captured

    return _configure


RECORDED_CLOUD = [{"slot": ["cloud", "image_0"], "source": "previous", "origin": "attachment:10", "digest": "x"}]


def _styled(quality: str, reference_source: str) -> dict:
    """`_cloud_config`, with both replayable settings on the style where they live."""
    return _cloud_config(
        "", styles=[{"id": "anime", "connection": "xai", "quality": quality, "reference_source": reference_source}]
    )


@pytest.mark.asyncio
async def test_a_cloud_record_round_trips_from_the_hook_into_resolve_target(_cloud_reroll):
    """The names are written in `hooks._REPLAYED_FACTS` and read in the adapter's
    `resolve_target`: two files matched by nothing but a string.

    A typo on either side degrades in silence to "use whatever the style says today"
    -- the exact substitution this module exists to prevent -- and every adapter-level
    test still passes, because those hand-build the replay dict instead of taking one
    the hook produced. So this records through the hook, moves the settings, and
    replays through the real resolver.
    """
    captured = _cloud_reroll(_styled(quality="high", reference_source="character"))
    params = {"prompt": "p", "negative_prompt": "", "style_id": "anime"}

    await hooks.reroll_gen(_RerollCtx("anime"), params, "1")

    assert (params["quality"], params["reference_source"]) == ("high", "character")

    # Both pickers move, as they would between the original render and a rehydrate of
    # it months later. Nothing about the row changed; only the settings did.
    captured = _cloud_reroll(_styled(quality="low", reference_source=""))
    await hooks.reroll_gen(_RerollCtx("anime", replay=True), params, "1")

    target = captured["target"]
    assert target.quality == "high", "a rehydrate billed at today's quality is a rehydrate that lied"
    assert target.reference_source == "character"
    assert len(target.reference_slots) == 1, "turning references off must not re-render this from the prompt alone"


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [True, False], ids=["rehydrate", "reroll"])
async def test_only_a_replay_calls_its_substitutions_a_mismatch(_cloud_reroll, replay):
    """ "it will not match" reports a broken promise, and only a replay made one.

    A reroll is *allowed* to land on another backend or drop a reference the new
    style has no slot for -- rendering on today's configuration is what the button
    does. Both routes still say what changed; only one calls it a failure.
    """
    captured = _cloud_reroll(_cloud_config(""))
    params = {
        "prompt": "p",
        "negative_prompt": "",
        "style_id": "anime",
        "source": "external_comfy",
        "references": list(RECORDED_CLOUD),
    }

    _, consumption = await hooks.reroll_gen(_RerollCtx("anime", replay=replay), params, "1")

    graded = [n for n in consumption["notes"] if "re-rendered on" in n or "does not take reference images" in n]
    assert len(graded) == 2, "both substitutions still disclosed on both routes"
    assert all(("will not match" in note) is replay for note in graded)
    assert captured["request"].references == ()


@pytest.mark.asyncio
async def test_a_cloud_reroll_with_references_off_drops_them_and_says_so(_cloud_reroll):
    """Submitting them anyway is what sent a stored WebP into an edits endpoint
    that had declared PNG/JPEG -- the target's slot list is empty, so there was no
    policy to convert under and the ComfyUI defaults applied."""
    captured = _cloud_reroll(_cloud_config(""))
    params = {"prompt": "p", "negative_prompt": "", "style_id": "anime", "references": list(RECORDED_CLOUD)}

    _, consumption = await hooks.reroll_gen(_RerollCtx("anime"), params, "1")

    assert captured["request"].references == ()
    assert any("does not take reference images" in note for note in consumption["notes"])
    # And the sibling records what was actually sent, not what the parent recorded.
    assert params["references"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("style_id", ["anime", "realistic"], ids=["same style", "style changed"])
async def test_a_cloud_reroll_converts_the_reference_to_what_the_provider_takes(_cloud_reroll, style_id):
    """A style change carries the reference over: refusing it was right only for
    ComfyUI's recorded node ids, and a cloud reference is an origin against a
    constant synthetic slot, so it replays as it stands."""
    styles = [
        {"id": "anime", "label": "Anime", "connection": "xai"},
        {"id": "realistic", "label": "Realistic", "connection": "xai"},
    ]
    captured = _cloud_reroll(_cloud_config("previous", styles=styles))
    params = {"prompt": "p", "negative_prompt": "", "style_id": style_id, "references": list(RECORDED_CLOUD)}

    await hooks.reroll_gen(_RerollCtx("anime"), params, "1")

    (reference,) = captured["request"].references
    assert reference.mime in ("image/png", "image/jpeg")
    assert reference.slot == ("cloud", "image_0")
