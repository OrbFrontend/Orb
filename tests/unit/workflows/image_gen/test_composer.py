from __future__ import annotations

import json

import pytest

from backend.workflows.image_gen import composer, prompts, scrub
from backend.workflows.image_gen.composer import (
    FIRST,
    THIRD,
    _render_scene,
    addressable_subjects,
    analyze_scene,
    compose_scene,
)
from backend.workflows.image_gen.subjects import Subject


def _subject(name: str, appearance: str = "", card_id: str = "") -> Subject:
    """One resolved subject, as `subjects.resolve` would hand it to the composer."""
    return Subject(
        member_id=f"m-{name}",
        card_id=card_id or f"card-{name}",
        name=name,
        profile={"appearance_prompt": appearance},
    )


def _fake_forced(results: dict, captured: dict | None = None):
    """Stand in for ``forced_tool_call``, answering each tool from *results* and
    optionally recording the tail each one was sent."""

    async def fake(*, tool_name, tail_messages=None, **kwargs):
        if captured is not None:
            captured[tool_name] = " ".join(m["content"] for m in tail_messages or [])
        yield {"type": "result", "args": results.get(tool_name, {})}

    return fake


async def _run(*, client=None, model_name="m", prefix=(), settings=None, scene_analysis: bool = False, **kwargs):
    """The composer sequence one render performs, in the order it performs it.

    Mirrors `hooks._generate_fresh`: the analyzer runs first and its answer is handed
    to the compose call. The two are separate entry points because production fills the
    reference slots in between -- `addressable_subjects` reads the analysis to decide
    whose likeness may leave the machine -- so a test that ran `compose_scene` alone
    would be exercising an order no render uses.
    """
    lane = {
        "client": client,
        "model_name": model_name,
        "prefix": prefix,
        "settings": settings if settings is not None else {"model_name": "writer"},
    }
    analysis = (
        await analyze_scene(
            **lane,
            pov=kwargs.get("pov", THIRD),
            reasoning_on=kwargs.get("reasoning_on", False),
            subjects=kwargs.get("subjects", ()),
            supports_negative=kwargs.get("supports_negative", True),
        )
        if scene_analysis
        else None
    )
    return await compose_scene(**lane, analysis=analysis, **kwargs)


async def _compose(monkeypatch, results: dict, captured: dict | None = None, **kwargs):
    """One render's composer sequence against stubbed forced-call results."""
    monkeypatch.setattr(composer, "forced_tool_call", _fake_forced(results, captured))
    return await _run(**kwargs)


# ── the structured scene block ───────────────────────────────────────────────


def test_render_scene_lays_out_each_character_with_outfit_and_position():
    block = _render_scene(
        {
            "characters": [
                {
                    "name": "Ashley",
                    "appearance": "",
                    "outfit": "silk dress, bare feet",
                    "position": "left, holding a book",
                    "pose": "sitting",
                    "action": "reading",
                },
                {"name": "nobleman", "appearance": "tall man, dark hair", "position": "right, behind her"},
            ],
            "anchors": "stone bench",
            "setting": "medieval garden, midday",
        },
        THIRD,
    )
    lines = block.splitlines()
    # Pose/position first, then visible attributes (outfit, appearance).
    assert lines[0] == "Ashley: left, holding a book, sitting, reading, wearing: silk dress, bare feet"
    assert lines[1] == "nobleman: right, behind her, tall man, dark hair"
    assert lines[2] == "setting and framing: medieval garden, midday, stone bench"


def test_render_scene_hides_face_for_turned_away_character():
    block = _render_scene(
        {
            "characters": [
                {
                    "name": "Malina",
                    "action": "flying away",
                    "face_visible": False,
                    "face_view": "back view",
                    "expression": "annoyed",
                    "gaze": "looking ahead",
                }
            ]
        },
        THIRD,
    )
    # No expression is readable off the back of a head.
    assert block.splitlines()[0] == "Malina: back view, flying away, gaze: looking ahead"


def test_render_scene_carries_viewer_contact_only_in_first_person():
    scene = {"characters": [{"name": "a", "action": "smiling"}], "viewer_contact": "one hand on her shoulder"}
    assert "the user's hand or arm in frame: one hand on her shoulder" in _render_scene(scene, FIRST)
    # Third-person never reads it, so an analyzer that filled it anyway cannot put
    # a disembodied hand in the shot.
    assert "one hand on her shoulder" not in _render_scene(scene, THIRD)


def test_render_scene_tolerates_junk_and_empties():
    assert _render_scene(None, THIRD) == ""
    assert _render_scene({"characters": ["not-a-dict", {}]}, THIRD) == ""  # no bits -> whole block empty


def test_the_structured_block_states_no_viewpoint_the_composer_could_copy():
    # The compose OOC tells the model to render this block exactly, so anything in
    # it is a candidate for the image prompt. The shot rules belong in the head.
    for mode_pov in (FIRST, THIRD):
        block = _render_scene({"characters": [{"name": "a", "action": "smiling"}]}, mode_pov).lower()
        assert "camera" not in block and "viewpoint" not in block
        assert "first-person" not in block and "third-person" not in block


def test_count_anchor_counts_cast_and_rejects_missing_sex():
    assert scrub.count_anchor([{"sex": "girl"}]) == "1girl, solo"
    assert scrub.count_anchor([{"sex": "girl"}, {"sex": "girl"}, {"sex": "boy"}]) == "2girls, 1boy"
    assert scrub.count_anchor([]) == ""
    assert scrub.count_anchor([{"sex": "girl"}, {"name": "no-sex"}]) is None
    assert scrub.count_anchor("junk") is None


# ── the cast, and who is in it ───────────────────────────────────────────────


async def test_scene_analysis_prepends_analysis_and_reports_mode(monkeypatch):
    scene, _, mode = await _compose(
        monkeypatch,
        {
            "analyze_scene": {"characters": [{"name": "a", "action": "waving"}]},
            "compose_image_prompt": {"scene": "1girl, waving", "avoid": None},
        },
        scene_analysis=True,
    )
    assert scene == "1girl, waving"  # no sex reported -> anchor not pinned, scene untouched
    assert mode == "scene_analysis"


async def test_empty_analysis_reports_analysis_failed(monkeypatch):
    _, _, mode = await _compose(
        monkeypatch,
        {"analyze_scene": {}, "compose_image_prompt": {"scene": "1girl"}},
        scene_analysis=True,
    )
    assert mode == "analysis_failed"


async def test_first_person_pin_strips_leaked_camera_boy(monkeypatch):
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {"characters": [{"name": "Ashley", "sex": "girl", "action": "smiling"}]},
            # The composer leaks the camera character into the count anchor, and a pov tag with it.
            "compose_image_prompt": {"scene": "1boy 1girl, pov, long red hair, smiling", "avoid": None},
        },
        scene_analysis=True,
        pov=FIRST,
    )
    assert scene == "1girl, solo, long red hair, smiling"


@pytest.mark.parametrize(
    ("mode_pov", "composed", "expected"),
    [
        # First-person is the user looking at the profile owner, so a background
        # cast member is dropped and the leaked counts are pinned over.
        (FIRST, "2girls 1boy, smiling", "1girl, solo, smiling"),
        # An outside camera draws everyone: the owner-only filter is first-person's alone.
        (THIRD, "1girl, smiling", "1girl, 1boy, smiling"),
    ],
    ids=["first person keeps only the owner", "third person keeps the cast"],
)
async def test_the_camera_decides_who_stays_in_the_cast(monkeypatch, mode_pov, composed, expected):
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {
                "characters": [
                    {"name": "Ashley", "is_listed_subject": True, "sex": "girl", "action": "smiling"},
                    {"name": "bystander", "sex": "boy", "action": "walking past"},
                ],
            },
            "compose_image_prompt": {"scene": composed, "avoid": None},
        },
        scene_analysis=True,
        subjects=[_subject("Ashley")],
        pov=mode_pov,
    )
    assert scene == expected


# ── the tool blob, and what the camera may move ──────────────────────────────


async def _tails_for(monkeypatch, pov: str) -> list[str]:
    """Every tail message the two forced calls carry for one camera."""
    captured: dict = {}
    await _compose(
        monkeypatch,
        {
            "analyze_scene": {"characters": [{"name": "a", "sex": "girl", "action": "smiling"}]},
            "compose_image_prompt": {"scene": "1girl, smiling", "avoid": None},
        },
        captured,
        scene_analysis=True,
        pov=pov,
    )
    return list(captured.values())


async def test_the_camera_moves_the_tails_and_never_the_tool_schemas(monkeypatch):
    """The cache invariant: POV is a tail-only concern.

    Both schemas ship on every off-turn call as one byte-stable blob that analyze,
    compose, and the next chat turn all reuse. A schema that varied with the camera
    would evict that prefix on every manual flip and every chat switch.
    """
    before = json.dumps([prompts.ANALYZE_TOOL_SCHEMA, prompts.COMPOSE_TOOL_SCHEMA, prompts.OFFER_TOOLS], sort_keys=True)

    first_tails = await _tails_for(monkeypatch, FIRST)
    third_tails = await _tails_for(monkeypatch, THIRD)

    after = json.dumps([prompts.ANALYZE_TOOL_SCHEMA, prompts.COMPOSE_TOOL_SCHEMA, prompts.OFFER_TOOLS], sort_keys=True)
    assert before == after, "the tool blob must not depend on the camera"
    assert first_tails != third_tails, "vacuity guard: the camera really did change what the model was told"
    # Asserted against the constants, not their wording: the prompts get tuned, the
    # wiring must not. Each camera ships its own copy and none of the other's.
    first_copy = (prompts._ANALYZE_CAMERA[FIRST], prompts._SHOT_COUNTED_FIRST, prompts._SHOT_PROSE_FIRST)
    third_copy = (prompts._ANALYZE_CAMERA[THIRD], prompts._SHOT_COUNTED_THIRD, prompts._SHOT_PROSE_THIRD)
    for tails, mine, theirs in ((first_tails, first_copy, third_copy), (third_tails, third_copy, first_copy)):
        joined = "".join(tails)
        assert sum(c in joined for c in mine) >= 2  # the analyze camera plus one shot rule
        assert not any(c in joined for c in theirs)


def test_the_analyze_schema_states_no_viewpoint_and_orders_viewer_contact_last():
    params = prompts.ANALYZE_TOOL_SCHEMA["function"]["parameters"]
    assert "viewpoint" not in params["properties"] and "viewpoint" not in params["required"]
    # `viewer_contact` ships in BOTH modes on purpose -- a first-person-only field is
    # a schema that varies with the camera. Strict decoding emits fields in schema
    # order, so it sits after the cast rather than ruling on the user's hand first;
    # `required` repeats that order for the same reason.
    assert "viewer_contact" in params["properties"] and "viewer_contact" in params["required"]
    order = list(params["properties"])
    assert order.index("viewer_contact") > order.index("characters")
    assert order.index("viewer_contact") > order.index("framing")
    assert params["required"] == [key for key in order if key in params["required"]]


async def test_both_calls_ride_the_prefix_unchanged_with_shared_tool_blob(monkeypatch):
    """KV-cache contract: analyze and compose send the byte-identical shared prefix
    (per-call instructions ride only the tail) and ship the same workflow-local
    tools blob, forcing one via tool_choice -- the pipeline pattern. A chat model
    needs the real tool; forcing via tools=None is unreliable (Gemma) or rejected
    (DeepSeek)."""
    calls = _record_forced_calls(monkeypatch)
    prefix = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "she waves"}]
    await _run(model_name="agent-m", prefix=prefix, scene_analysis=True)
    assert [c["tool_name"] for c in calls] == ["analyze_scene", "compose_image_prompt"]
    for call in calls:
        assert call["prefix"] is prefix
        assert call["model_name"] == "agent-m"
        assert call["offer_tools"] == ("analyze_scene", "compose_image_prompt")
        assert call.get("tools_in_prompt", True) is not False  # ship the tools, never tools=None
        assert all(msg["role"] == "user" for msg in call["tail_messages"])


def _record_forced_calls(monkeypatch) -> list[dict]:
    """Capture every ``forced_tool_call`` kwargs the composer issues."""
    calls: list[dict] = []
    inner = _fake_forced(
        {
            "analyze_scene": {"characters": [{"name": "a", "sex": "girl", "action": "waving"}]},
            "compose_image_prompt": {"scene": "1girl, waving", "avoid": None},
        }
    )

    def fake(**kwargs):
        calls.append(kwargs)
        return inner(tool_name=kwargs["tool_name"])

    monkeypatch.setattr(composer, "forced_tool_call", fake)
    return calls


async def test_reasoning_mode_is_explicit_and_ignores_pipeline_pass_flags(monkeypatch):
    """The workflow setting owns both calls; no pipeline pass is its fallback."""
    for reasoning_on in (False, True):
        calls = _record_forced_calls(monkeypatch)
        await _run(
            model_name="agent-m",
            settings={
                "model_name": "writer",
                "reasoning_enabled_passes": {p: not reasoning_on for p in ("director", "writer", "editor")},
            },
            reasoning_on=reasoning_on,
            scene_analysis=True,
        )
        assert [c["tool_name"] for c in calls] == ["analyze_scene", "compose_image_prompt"]
        assert all(c["reasoning_on"] is reasoning_on and c["model_name"] == "agent-m" for c in calls)


# ── scene hygiene ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode_pov", [FIRST, THIRD])
async def test_the_word_camera_never_reaches_the_image_prompt(monkeypatch, mode_pov):
    # A diffusion text encoder draws "camera" as an object in frame. The word is
    # unavoidable in the instructions, so a composer echoing it back is a matter of
    # when, not if -- the chunk carrying it is dropped whatever the model wrote.
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "compose_image_prompt": {
                "scene": "1girl, the camera is her eyes, camerawork from above, looking at viewer, red hair",
                "avoid": None,
            }
        },
        pov=mode_pov,
    )
    # The booru tag "looking at viewer" is real and wanted; only the camera chunks go.
    assert scene == "1girl, looking at viewer, red hair"


async def test_single_call_strips_negation_from_scene(monkeypatch):
    # No analysis path here: negation hygiene must still run, or CLIP draws the
    # item. The negation phrase sits mid-chunk, so a start-anchored match misses it.
    scene, _, mode = await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "1girl, red dress, not wearing shoes, garden", "avoid": None}},
    )
    assert scene == "1girl, red dress, garden"
    assert mode == "single_call"


@pytest.mark.parametrize(
    ("mode_pov", "prompt_format", "expected"),
    [
        # The encoder has no "grips the viewer's collar" -- it has the reach toward
        # the lens. Both contact chunks name one grab, so the tag block lands once.
        (FIRST, "tags", "1girl, reaching beyond edge of screen, foreshortening, looking at viewer, red hair"),
        # Third-person has no viewer to touch: the gate is off and nothing is swapped.
        (
            THIRD,
            "tags",
            "1girl, arm gripping viewer's shirt collar, pulling the viewer closer, looking at viewer, red hair",
        ),
        # Prose keeps its literal phrasing: a natural-language encoder can read it,
        # and booru tags spliced into a sentence cost more than they fix. The lead
        # count tag goes for the unrelated reason that prose encoders read it literally.
        (FIRST, "prose", "arm gripping viewer's shirt collar, pulling the viewer closer, looking at viewer, red hair"),
    ],
    ids=["first_person_tags", "third_person", "prose"],
)
async def test_viewer_contact_becomes_tags_the_encoder_has_seen(monkeypatch, mode_pov, prompt_format, expected):
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "compose_image_prompt": {
                "scene": "1girl, arm gripping viewer's shirt collar, pulling the viewer closer, looking at viewer, red hair",
                "avoid": None,
            }
        },
        pov=mode_pov,
        prompt_format=prompt_format,
    )
    assert scene == expected


@pytest.mark.parametrize(
    ("chunk", "expected"),
    [
        # Contact collapses to a composition booru has in volume. Kept verbatim,
        # "the viewer's chest" would put the viewer's own body in a shot taken from
        # behind their eyes.
        ("1girl, leaning against the viewer's chest", "1girl, close-up, foreshortening"),
        ("1girl, kissing the viewer", "1girl, incoming kiss, close-up, foreshortening"),
        # Contact without a reach, and a threat without contact: both would be wrong
        # as the fallback's reach past the frame edge -- one flatly, one by dropping.
        ("1girl, straddling the viewer's lap", "1girl, on top, straddling, foreshortening"),
        ("1girl, pointing a knife at the viewer", "1girl, aiming at viewer, foreshortening"),
        # Second person is the same viewer by another name.
        ("1girl, grabbing your collar", "1girl, reaching beyond edge of screen, foreshortening"),
        # The fallback fires on contact, not on the word: an invented reaching arm is
        # a worse failure than a lost chunk, because it draws a limb nobody wrote.
        ("1girl, standing close to the viewer, red hair", "1girl, red hair"),
        ("1girl, blocking the viewer's path", "1girl"),
        # Gaze survives in every phrasing, normalized onto the one tag booru has.
        ("1girl, red hair, looking at viewer", "1girl, red hair, looking at viewer"),
        ("1girl, looking up at the viewer", "1girl, looking at viewer"),
        ("1girl, her eyes searching the viewer's face", "1girl, looking at viewer"),
        # "pin" and "cup" are real words on their own, unlike the other stems, so a
        # costume or a prop must not become a fabricated arm -- without the stems
        # losing their real inflections.
        ("1girl, wearing a pin-up costume near the viewer", "1girl"),
        ("1girl, a cupcake sits beside the viewer", "1girl"),
        ("1girl, pinning the viewer against the wall", "1girl, close-up, foreshortening"),
        ("1girl, cupping the viewer's cheek", "1girl, reaching beyond edge of screen, foreshortening"),
        # Which side of the contact the viewer is on decides everything. As patient,
        # their throat is not drawn and the grip reaching the lens is; as agent, the
        # limb keeps its action, because collapsing it would reverse who chokes whom.
        ("1girl, hand gripping the viewer's throat", "1girl, strangling, reaching towards viewer, foreshortening"),
        ("1girl, the user's hand gripping her throat, kneeling", "1girl, pov hands, hand gripping her throat, kneeling"),
        # Both directions at once, which is what a struggle actually looks like.
        (
            "your hand on her throat, her arm gripping your shirt",
            "pov hands, hand on her throat, reaching beyond edge of screen, foreshortening",
        ),
    ],
)
def test_viewer_talk_is_rewritten_into_tags_the_encoder_has_seen(chunk, expected):
    assert scrub.rewrite_viewer_contact(chunk) == expected


async def test_nullish_strings_never_reach_the_scene_or_the_negative(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(composer, "_render_scene", lambda a, p: seen.append(_render_scene(a, p)) or seen[-1])
    _, avoid, _ = await _compose(
        monkeypatch,
        {
            # Model wrote the word instead of emitting JSON null.
            "analyze_scene": {
                "anchors": "null",
                "characters": [{"name": "Ashley", "sex": "girl", "action": "smiling", "outfit": "None"}],
                "setting": "garden",
                "framing": "null",
                "avoid": "null",
            },
            "compose_image_prompt": {"scene": "1girl, smiling", "avoid": "null"},
        },
        scene_analysis=True,
    )
    assert avoid == ""
    assert "null" not in seen[0].casefold() and "none" not in seen[0].casefold()
    assert seen[0].splitlines()[-1] == "setting and framing: garden"


async def test_no_negative_workflow_tells_model_to_leave_avoid_empty(monkeypatch):
    captured: dict = {}
    await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "1girl", "visible_subjects": []}},
        captured,
        supports_negative=False,
    )
    assert prompts._LEAVE_AVOID_EMPTY in captured["compose_image_prompt"]


async def test_analysis_avoid_items_ride_avoid(monkeypatch):
    _, avoid, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {
                "characters": [{"name": "Ashley", "sex": "girl", "appearance": "", "action": "walking away"}],
                "avoid": "looking at viewer",
            },
            "compose_image_prompt": {"scene": "1girl, from behind", "avoid": "blur", "visible_subjects": []},
        },
        scene_analysis=True,
    )
    assert avoid == "blur, looking at viewer"


async def test_failed_compose_stops_instead_of_shipping_the_reply(monkeypatch):
    # Every forced call returns empty args -> no scene. The composer must stop,
    # never fall back to the raw reply text as the image prompt (prose the
    # tag-trained checkpoints render as mush).
    with pytest.raises(ValueError, match="couldn't compose an image prompt"):
        await _compose(monkeypatch, {})


# ── prose, which reads count tags literally ──────────────────────────────────


@pytest.mark.parametrize(
    ("results", "extra"),
    [
        (
            {
                "compose_image_prompt": {
                    "scene": "1girl, solo. Iris sits beside the window. Two women cross the garden behind her.",
                    "avoid": None,
                    "visible_subjects": [],
                }
            },
            {},
        ),
        (
            {
                "analyze_scene": {
                    "characters": [
                        {"name": "Iris", "sex": "girl", "action": "standing"},
                        {"name": "Ren", "sex": "boy", "action": "sitting"},
                    ],
                },
                "compose_image_prompt": {
                    "scene": "1girl, 1boy. Iris sits beside the window. Two women cross the garden behind her.",
                    "avoid": None,
                    "visible_subjects": [],
                },
            },
            {"scene_analysis": True},
        ),
    ],
    ids=["leaked prefix stripped", "analysis never pins an anchor"],
)
async def test_prose_never_carries_booru_count_tags(monkeypatch, results, extra):
    scene, _, _ = await _compose(monkeypatch, results, prompt_format="prose", **extra)
    assert scene == "Iris sits beside the window. Two women cross the garden behind her."


def test_final_prose_assembly_strips_count_tags_from_stale_scene_and_saved_style():
    config = {
        "styles": [
            {
                "id": "prose",
                "label": "Prose",
                "prompt_format": "prose",
                "prompt": "cinematic photograph, 1girl, natural light",
                "negative_prompt": "",
                "checkpoint": "",
                "workflow": "",
            }
        ]
    }
    positive, _, _ = composer.assemble_prompts(
        config, "prose", {"appearance_prompt": ""}, "1girl, solo. Iris sits beside the window.", ""
    )
    assert positive == "cinematic photograph, natural light, Iris sits beside the window."


def test_assemble_keeps_profile_out_of_positive_because_composer_owns_it():
    config = {
        "styles": [
            {
                "id": "anime",
                "label": "Anime",
                "prompt": "anime illustration, clean line art, very aesthetic, high contrast",
                "negative_prompt": "photorealistic, 3d render, muddy colors",
                "checkpoint": "",
                "workflow": "",
            }
        ]
    }
    positive, _, _ = composer.assemble_prompts(
        config, "anime", {"appearance_prompt": "1girl, solo, long red hair"}, "2girls, garden", ""
    )
    assert positive == "2girls, anime illustration, clean line art, very aesthetic, high contrast, garden"


# ── the saved profile appearance ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt_format", "expected"),
    [
        ("tags", "2girls, long silver hair, blue eyes, Iris sits beside Ashley."),
        ("hybrid", "2girls, Iris: long silver hair, blue eyes, Iris sits beside Ashley."),
        ("prose", "Iris has these traits: long silver hair, blue eyes. Iris sits beside Ashley."),
    ],
)
async def test_profile_appearance_binding_follows_prompt_format(monkeypatch, prompt_format, expected):
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "compose_image_prompt": {
                "scene": "2girls, Iris sits beside Ashley.",
                "avoid": None,
                "visible_subjects": ["Iris"],
            }
        },
        subjects=[_subject("Iris", "long silver hair, blue eyes")],
        prompt_format=prompt_format,
    )
    assert scene == expected


async def test_single_call_inserts_the_profile_only_when_its_owner_is_visible(monkeypatch):
    visible, _, _ = await _compose(
        monkeypatch,
        {
            "compose_image_prompt": {
                "scene": "1girl, solo, sitting by a window",
                "avoid": None,
                "visible_subjects": ["Iris"],
            }
        },
        subjects=[_subject("Iris", "long silver hair, blue eyes")],
        prompt_format="hybrid",
    )
    assert visible == "1girl, solo, Iris: long silver hair, blue eyes, sitting by a window"

    absent, _, _ = await _compose(
        monkeypatch,
        {
            "compose_image_prompt": {
                "scene": "1boy, solo, standing in a doorway",
                "avoid": None,
                "visible_subjects": [],
            }
        },
        subjects=[_subject("Iris", "long silver hair, blue eyes")],
    )
    assert "silver hair" not in absent


async def test_profile_appearance_negation_cannot_bypass_scene_cleanup(monkeypatch):
    scene, _, _ = await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "1girl, solo, sitting", "avoid": None, "visible_subjects": ["Iris"]}},
        subjects=[_subject("Iris", "long silver hair, not wearing glasses, blue eyes")],
        prompt_format="hybrid",
    )
    assert scene == "1girl, solo, Iris: long silver hair, blue eyes, sitting"


async def test_analysis_owns_profile_visibility_and_preserves_scene_fields(monkeypatch):
    captured: dict = {}
    scene, avoid, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {
                "characters": [
                    {
                        "name": "Iris",
                        "is_listed_subject": True,
                        "sex": "girl",
                        "appearance": None,
                        "outfit": "black dress",
                        "position": "left",
                        "pose": "standing",
                        "action": "holding Ashley's hand",
                        "expression": "smiling",
                        "gaze": "looking at Ashley",
                    }
                ],
                "setting": "library at night",
                "anchors": "window",
                "interaction": "Iris holds Ashley's hand",
                "framing": "medium shot",
                "avoid": "looking at viewer",
            },
            "compose_image_prompt": {
                "scene": "black dress, holding hands, smiling, library at night, medium shot",
                "avoid": None,
                # Structured analysis, not this redundant field, owns visibility.
                "visible_subjects": [],
            },
        },
        captured,
        subjects=[_subject("Iris", "long silver hair")],
        scene_analysis=True,
    )
    assert scene.startswith("1girl, solo, Iris: long silver hair,")  # identity stays near the prompt head
    assert scene.endswith("medium shot")
    assert avoid == "looking at viewer"
    structured_tail = captured["compose_image_prompt"]
    for fragment in ("expression: smiling", "gaze: looking at Ashley", "interaction: Iris holds Ashley's hand", "medium shot"):
        assert fragment in structured_tail


async def test_analysis_does_not_insert_an_off_frame_profile(monkeypatch):
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {
                "characters": [{"name": "Ashley", "is_listed_subject": False, "sex": "girl", "action": "reading"}],
                "setting": "library",
            },
            # The structured cast is authoritative, not this field.
            "compose_image_prompt": {"scene": "reading in a library", "avoid": None, "visible_subjects": ["Iris"]},
        },
        subjects=[_subject("Iris", "long silver hair")],
        scene_analysis=True,
    )
    assert "silver hair" not in scene


@pytest.mark.parametrize(
    ("face_visible", "kept", "dropped"),
    [
        # Malina flies away: the analyzer flags her face hidden, so the injected
        # frontal sheet must lose its face-only traits but keep hair and wings.
        (False, ("jet-black wings", "long silky black hair", "black nails"), ("eyes", "eyeliner")),
        (True, ("glowing purple eyes", "black eyeliner"), ()),
    ],
    ids=["back shot", "face toward camera"],
)
async def test_a_hidden_face_strips_face_traits_from_the_injected_appearance(monkeypatch, face_visible, kept, dropped):
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {
                "characters": [
                    {
                        "name": "Malina",
                        "is_listed_subject": True,
                        "sex": "girl",
                        "appearance": None,
                        "action": "flying upward away from the viewer",
                        "face_visible": face_visible,
                        "expression": None,
                    }
                ],
            },
            "compose_image_prompt": {"scene": "flying upward away from the viewer", "avoid": None},
        },
        subjects=[
            _subject("Malina", "jet-black wings, long silky black hair, glowing purple eyes, black eyeliner, black nails")
        ],
        prompt_format="tags",
        scene_analysis=True,
    )
    assert all(trait in scene for trait in kept)
    assert not any(trait in scene for trait in dropped)


# ── several subjects ─────────────────────────────────────────────────────────


async def test_two_subjects_are_injected_in_roster_order(monkeypatch):
    """Trap 4.3. The injector finds its insertion point by peeling the count anchor
    off the head, so a per-subject loop would stack the second block *in front of* the
    first and hand the reader the cast backwards. One call, one pass, roster order."""
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {
                "characters": [
                    # Deliberately analyzer order, not roster order: the answer must
                    # follow the subjects, which is what the slots were filled from.
                    {"name": "Ashley", "sex": "girl", "action": "reading"},
                    {"name": "Iris", "sex": "girl", "action": "sitting"},
                ]
            },
            "compose_image_prompt": {"scene": "Iris sits beside Ashley.", "avoid": None},
        },
        subjects=[_subject("Iris", "long silver hair"), _subject("Ashley", "red coat")],
        prompt_format="hybrid",
        scene_analysis=True,
    )
    assert scene == "2girls, Iris: long silver hair, Ashley: red coat, Iris sits beside Ashley."


async def test_each_subject_keeps_its_own_face_visibility(monkeypatch):
    """Visibility is per person: Ashley faces away, so her frontal sheet loses its
    face-only traits while Iris keeps hers."""
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {
                "characters": [
                    {"name": "Iris", "sex": "girl", "face_visible": True, "action": "smiling"},
                    {"name": "Ashley", "sex": "girl", "face_visible": False, "action": "walking away"},
                ]
            },
            "compose_image_prompt": {"scene": "two women in a hallway", "avoid": None},
        },
        subjects=[_subject("Iris", "silver hair, blue eyes"), _subject("Ashley", "red coat, green eyes")],
        prompt_format="hybrid",
        scene_analysis=True,
    )
    assert "Iris: silver hair, blue eyes" in scene
    assert "Ashley: red coat" in scene and "green eyes" not in scene


async def test_a_subject_the_analyzer_left_out_contributes_nothing(monkeypatch):
    """Someone whose likeness a slot may still have been handed, but who walked out of
    frame: injecting their sheet is how a saved appearance draws a second person in."""
    scene, _, _ = await _compose(
        monkeypatch,
        {
            "analyze_scene": {"characters": [{"name": "Iris", "sex": "girl", "action": "alone at the window"}]},
            "compose_image_prompt": {"scene": "a woman at a window", "avoid": None},
        },
        subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red coat")],
        prompt_format="hybrid",
        scene_analysis=True,
    )
    assert "silver hair" in scene and "red coat" not in scene


async def test_two_subjects_are_named_even_in_tag_format(monkeypatch):
    """A lone subject keeps raw tags -- there is nothing to bind them to. Two do not:
    concatenated anonymously they read as one person wearing both outfits, which is
    worse than a name a booru encoder handles poorly."""
    scene, _, _ = await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "2girls, a hallway", "avoid": None, "visible_subjects": ["Iris", "Ashley"]}},
        subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red coat")],
        prompt_format="tags",
    )
    assert scene == "2girls, Iris: silver hair, Ashley: red coat, a hallway"


async def test_the_single_call_path_lists_the_visible_subjects_by_name(monkeypatch):
    """`visible_subjects` replaced a lone boolean because one boolean cannot answer for
    several people. Only the ones named get their sheet."""
    scene, _, _ = await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "1girl, solo, a hallway", "avoid": None, "visible_subjects": ["Ashley"]}},
        subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red coat")],
        prompt_format="hybrid",
    )
    assert "red coat" in scene and "silver hair" not in scene


async def test_both_calls_are_told_the_whole_roster_and_the_names_to_copy(monkeypatch):
    """The name is what binds an analyzed entry back to a subject afterwards, so both
    calls have to quote the same roster and ask for it verbatim."""
    captured: dict = {}
    await _compose(
        monkeypatch,
        {
            "analyze_scene": {"characters": [{"name": "Iris", "sex": "girl", "action": "sitting"}]},
            "compose_image_prompt": {"scene": "1girl, solo", "avoid": None},
        },
        captured,
        subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red coat")],
        scene_analysis=True,
    )
    for tail in captured.values():
        assert "- Iris" in tail and "- Ashley" in tail
        assert "silver hair" in tail and "red coat" in tail


async def test_the_subject_roster_is_never_numbered_against_the_reference_roster(monkeypatch):
    """Two numbered lists in one prompt is one list too many.

    The reference roster is numbered because its order is a fact about the request --
    a provider handed an array of images is told nothing else about which is which.
    The subject roster is *not* the same list: here Iris is subject 1 and no reference
    at all, while Ashley is subject 2 and reference 1. Numbering both would put
    "1. Iris" and "1. Ashley" in one prompt meaning different things."""
    captured: dict = {}
    await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "2girls", "avoid": None, "visible_subjects": []}},
        captured,
        subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red coat")],
        has_references=True,
        referenced_subjects=["Ashley"],
    )
    tail = captured["compose_image_prompt"]
    assert "- Iris" in tail and "- Ashley" in tail
    assert "1. Iris" not in tail and "2. Ashley" not in tail
    # The one numbered list left says what it is about: the images, in travel order.
    assert "in this order: 1. Ashley." in tail


async def test_a_nameless_subject_leaves_no_hole_in_the_roster(monkeypatch):
    """A solo card with no name used to be enumerated and then filtered, so the roster
    opened at "2." with no "1." -- a numbered list missing its first row, in a prompt
    that also carries the numbered reference list."""
    captured: dict = {}
    await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "2girls", "avoid": None, "visible_subjects": []}},
        captured,
        subjects=[_subject("", "silver hair"), _subject("Ashley", "red coat")],
    )
    tail = captured["compose_image_prompt"]
    assert "- Ashley - fixed positive tags added separately: red coat" in tail
    assert "2." not in tail.split("Do not copy or contradict")[0]


# ── trap 4.1: identity suppression is only for who was actually referenced ───


def test_the_reference_instruction_names_the_referenced_subjects_in_order():
    """With Iris referenced and Ashley merely visible, an unconditional "do not
    describe permanent identity traits" strips Ashley's face, eyes and hair and renders
    her as a generic person. So the instruction states the roster and says the rest are
    to be described in full -- which is also the only thing that can tell a cloud
    provider handed an array of images which one is which."""
    instruction = prompts._reference_instruction(["Iris", "Ashley"])

    assert "1. Iris, 2. Ashley" in instruction
    assert "For those people only" in instruction
    assert "every OTHER visible person in full" in instruction

    # A `previous` reference names nobody -- it is a picture of this scene, which is
    # what the original singular wording already describes.
    assert prompts._reference_instruction([]) == prompts._REFERENCE_INSTRUCTION


async def test_only_a_referenced_subject_has_its_identity_suppressed(monkeypatch):
    captured: dict = {}
    await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "2girls", "avoid": None, "visible_subjects": []}},
        captured,
        subjects=[_subject("Iris"), _subject("Ashley")],
        has_references=True,
        referenced_subjects=["Iris"],
    )
    tail = captured["compose_image_prompt"]
    assert "1. Iris" in tail
    assert "Ashley" not in tail.split("in this order:")[1].split(".")[0]
    assert "every OTHER visible person in full" in tail


async def test_no_references_means_no_reference_instruction_at_all(monkeypatch):
    captured: dict = {}
    await _compose(
        monkeypatch,
        {"compose_image_prompt": {"scene": "1girl", "avoid": None, "visible_subjects": []}},
        captured,
        subjects=[_subject("Iris")],
        has_references=False,
        referenced_subjects=["Iris"],
    )
    assert "reference image" not in captured["compose_image_prompt"]


# ── trap 4.2: a slot may only draw someone the picture actually contains ─────


def _analysis(*names: str) -> dict:
    return {"characters": [{"name": name, "sex": "girl"} for name in names]}


def test_a_subject_out_of_frame_is_not_addressable_by_a_slot():
    """The composer already drops an absent subject's *words*. The likeness has to go
    with them: an edit model handed a face the prompt never mentions draws that person
    back into the shot, which is the one failure a reference render cannot be talked
    out of."""
    subjects = [_subject("Iris"), _subject("Ashley"), _subject("Ren")]

    # Ashley left the room between speaking and the shot; Ren is still in it.
    assert [s.name for s in addressable_subjects(subjects, _analysis("Iris", "Ren"))] == ["Iris", "Ren"]


def test_the_primary_is_addressable_whether_or_not_the_analyzer_listed_them():
    """`character` means the render's primary, and a solo chat's one slot has to
    resolve. Filtering subject 0 would turn a wide shot of an established character
    into a hard failure on every ComfyUI graph built around a `LoadImage`."""
    subjects = [_subject("Iris"), _subject("Ashley")]

    # The analyzer named Ashley and not Iris. Iris is subject 0 regardless.
    assert [s.name for s in addressable_subjects(subjects, _analysis("Ashley"))] == ["Iris", "Ashley"]
    # An analysis that put nobody in frame still leaves the primary addressable.
    assert [s.name for s in addressable_subjects(subjects, _analysis())] == ["Iris"]


def test_a_missing_analysis_is_not_an_answer_of_nobody():
    """The single-call path has no analysis, and a forced call that came back empty has
    no answer either. Neither may be read as "the shot is empty" -- that would silently
    kill every cast slot the moment the analyzer hiccuped."""
    subjects = [_subject("Iris"), _subject("Ashley")]

    assert [s.name for s in addressable_subjects(subjects, None)] == ["Iris", "Ashley"]
    assert [s.name for s in addressable_subjects(subjects, {})] == ["Iris", "Ashley"]


def test_the_addressable_list_compacts_so_cast_still_means_the_next_one():
    """The slots address positions in *this* list. A sparse answer would leave the
    first `cast` row drawing nobody while the second drew somebody, which is not what
    "the next cast member" says on the picker."""
    subjects = [_subject("Iris"), _subject("Ashley"), _subject("Ren")]

    assert [s.name for s in addressable_subjects(subjects, _analysis("Iris", "Ren"))][1] == "Ren"


async def test_a_subject_the_model_renamed_is_logged_rather_than_silently_dropped(monkeypatch, caplog):
    """The one failure in this workflow with no user-visible symptom: the render
    succeeds, looks plausible, and is quietly a picture of someone slightly else."""
    with caplog.at_level("INFO", logger="backend.workflows.image_gen.composer"):
        await _compose(
            monkeypatch,
            {
                "analyze_scene": {"characters": [{"name": "the woman", "sex": "girl"}, {"name": "Ashley", "sex": "girl"}]},
                "compose_image_prompt": {"scene": "2girls", "avoid": None},
            },
            subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red coat")],
            scene_analysis=True,
        )

    assert "did not name 'Iris'" in caplog.text
