from __future__ import annotations

import json

import pytest

from backend.workflows.image_gen import composer
from backend.workflows.image_gen.composer import (
    FIRST,
    THIRD,
    _render_scene,
    compose_scene,
)


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
    line = block.splitlines()[0]
    assert line == "Malina: back view, flying away, gaze: looking ahead"
    assert "expression" not in line  # no expression readable off the back of a head


def test_render_scene_carries_viewer_contact_only_in_first_person():
    scene = {"characters": [{"name": "a", "action": "smiling"}], "viewer_contact": "one hand on her shoulder"}
    assert "the user's hand or arm in frame: one hand on her shoulder" in _render_scene(scene, FIRST)
    # Third-person never reads it, so an analyzer that filled it anyway cannot put
    # a disembodied hand in the shot.
    assert "one hand on her shoulder" not in _render_scene(scene, THIRD)


def test_render_scene_tolerates_junk_and_empties():
    assert _render_scene(None, THIRD) == ""
    assert _render_scene({"characters": ["not-a-dict", {}]}, THIRD) == ""  # no bits -> character dropped -> whole block empty


def test_count_anchor_counts_cast_and_rejects_missing_sex():
    assert composer._count_anchor([{"sex": "girl"}]) == "1girl, solo"
    assert composer._count_anchor([{"sex": "girl"}, {"sex": "girl"}, {"sex": "boy"}]) == "2girls, 1boy"
    assert composer._count_anchor([]) == ""
    assert composer._count_anchor([{"sex": "girl"}, {"name": "no-sex"}]) is None
    assert composer._count_anchor("junk") is None


def _fake_forced(results: dict):
    async def fake(*, tool_name, **kwargs):
        yield {"type": "result", "args": results.get(tool_name, {})}

    return fake


async def test_scene_analysis_prepends_analysis_and_reports_mode(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {"characters": [{"name": "a", "action": "waving"}]},
                "compose_image_prompt": {"scene": "1girl, waving", "avoid": None},
            }
        ),
    )
    scene, avoid, mode = await compose_scene(
        client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, scene_analysis=True
    )
    assert scene == "1girl, waving"  # no sex reported -> anchor not pinned, scene untouched
    assert mode == "scene_analysis"


async def test_first_person_pin_strips_leaked_camera_boy(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [{"name": "Ashley", "sex": "girl", "action": "smiling"}],
                },
                # Composer leaks the camera character into the count anchor, and a pov tag with it.
                "compose_image_prompt": {"scene": "1boy 1girl, pov, long red hair, smiling", "avoid": None},
            }
        ),
    )
    scene, _, mode = await compose_scene(
        client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, scene_analysis=True, pov=FIRST
    )
    assert scene == "1girl, solo, long red hair, smiling"
    assert mode == "scene_analysis"


async def test_first_person_keeps_only_profile_owner(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [
                        {"name": "Ashley", "is_profile_owner": True, "sex": "girl", "action": "smiling"},
                        {"name": "bystander", "sex": "boy", "action": "walking past"},
                    ],
                },
                "compose_image_prompt": {"scene": "2girls 1boy, smiling", "avoid": None},
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        scene_analysis=True,
        profile_owner_name="Ashley",
        pov=FIRST,
    )
    # Bystander stripped: count anchor is solo, and the composer's leaked counts are pinned over.
    assert scene == "1girl, solo, smiling"


async def test_third_person_keeps_the_whole_cast(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [
                        {"name": "Ashley", "is_profile_owner": True, "sex": "girl", "action": "smiling"},
                        {"name": "bystander", "sex": "boy", "action": "walking past"},
                    ],
                },
                "compose_image_prompt": {"scene": "1girl, smiling", "avoid": None},
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        scene_analysis=True,
        profile_owner_name="Ashley",
        pov=THIRD,
    )
    # The owner-only filter is first-person's alone: an outside camera draws everyone.
    assert scene == "1girl, 1boy, smiling"


def test_keep_profile_owner_no_op_when_owner_absent():
    analysis = {"characters": [{"name": "stranger", "sex": "girl"}]}
    composer._keep_profile_owner(analysis, "Ashley")
    assert analysis["characters"] == [{"name": "stranger", "sex": "girl"}]


async def _tails_for(monkeypatch, pov: str) -> list[str]:
    """Every tail message the two forced calls carry for one camera."""
    seen: list[str] = []

    async def fake(*, tool_name, tail_messages, **kwargs):
        seen.extend(m["content"] for m in tail_messages)
        yield {
            "type": "result",
            "args": {
                "analyze_scene": {"characters": [{"name": "a", "sex": "girl", "action": "smiling"}]},
                "compose_image_prompt": {"scene": "1girl, smiling", "avoid": None},
            }.get(tool_name, {}),
        }

    monkeypatch.setattr(composer, "forced_tool_call", fake)
    await compose_scene(client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, scene_analysis=True, pov=pov)
    return seen


async def test_the_camera_moves_the_tails_and_never_the_tool_schemas(monkeypatch):
    """The cache invariant: POV is a tail-only concern.

    Both schemas ship on every off-turn call as one byte-stable blob that analyze,
    compose, and the next chat turn all reuse. A schema that varied with the camera
    would evict that prefix on every manual flip, every classifier disagreement,
    and every chat switch -- so the camera may only ever move the tail messages,
    which sit after the shared prefix and cost nothing.
    """
    before = json.dumps([composer.ANALYZE_TOOL_SCHEMA, composer.COMPOSE_TOOL_SCHEMA, composer._OFFER_TOOLS], sort_keys=True)

    first_tails = await _tails_for(monkeypatch, FIRST)
    third_tails = await _tails_for(monkeypatch, THIRD)

    after = json.dumps([composer.ANALYZE_TOOL_SCHEMA, composer.COMPOSE_TOOL_SCHEMA, composer._OFFER_TOOLS], sort_keys=True)
    assert before == after, "the tool blob must not depend on the camera"
    # Vacuity guard: the camera really did change what the model was told.
    assert first_tails != third_tails
    # Asserted against the constants, not their wording: the prompts get tuned, the
    # wiring must not. Each camera ships its own copy and none of the other's.
    first_copy = (composer._ANALYZE_CAMERA[FIRST], composer._SHOT_COUNTED_FIRST, composer._SHOT_PROSE_FIRST)
    third_copy = (composer._ANALYZE_CAMERA[THIRD], composer._SHOT_COUNTED_THIRD, composer._SHOT_PROSE_THIRD)
    for tails, mine, theirs in ((first_tails, first_copy, third_copy), (third_tails, third_copy, first_copy)):
        joined = "".join(tails)
        assert sum(c in joined for c in mine) >= 2  # the analyze camera plus one shot rule
        assert not any(c in joined for c in theirs)


def test_the_analyze_schema_states_no_viewpoint():
    params = composer.ANALYZE_TOOL_SCHEMA["function"]["parameters"]
    assert "viewpoint" not in params["properties"]
    assert "viewpoint" not in params["required"]
    # viewer_contact is present in BOTH modes on purpose: a first-person-only field
    # is a schema that varies with the camera.
    assert "viewer_contact" in params["properties"]
    assert "viewer_contact" in params["required"]


@pytest.mark.parametrize("mode_pov", [FIRST, THIRD])
async def test_the_word_camera_never_reaches_the_image_prompt(monkeypatch, mode_pov):
    # A diffusion text encoder draws "camera" as an object in frame. The word is
    # unavoidable in the instructions, so a composer echoing it back is a matter of
    # when, not if -- the chunk carrying it is dropped whatever the model wrote.
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "compose_image_prompt": {
                    "scene": "1girl, the camera is her eyes, camerawork from above, looking at viewer, red hair",
                    "avoid": None,
                }
            }
        ),
    )
    scene, _, _ = await compose_scene(client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, pov=mode_pov)
    assert "camera" not in scene.lower()
    # The booru tag "looking at viewer" is real and wanted; only the camera chunks go.
    assert scene == "1girl, looking at viewer, red hair"


@pytest.mark.parametrize(
    "mode_pov, prompt_format, expected",
    [
        # The encoder has no "grips the viewer's collar" -- it has the reach toward
        # the lens. Both contact chunks name one grab, so the tag block lands once.
        (
            FIRST,
            "tags",
            "1girl, reaching beyond edge of screen, foreshortening, looking at viewer, red hair",
        ),
        # Third-person has no viewer to touch: the gate is off and nothing is swapped.
        (
            THIRD,
            "tags",
            "1girl, arm gripping viewer's shirt collar, pulling the viewer closer, looking at viewer, red hair",
        ),
        # Prose keeps its literal phrasing: a natural-language encoder can read it,
        # and booru tags spliced into a sentence cost more than they fix. The lead
        # count tag goes for the unrelated reason that prose encoders read it literally.
        (
            "prose",
            "prose",
            "arm gripping viewer's shirt collar, pulling the viewer closer, looking at viewer, red hair",
        ),
    ],
    ids=["first_person_tags", "third_person", "prose"],
)
async def test_viewer_contact_becomes_tags_the_encoder_has_seen(monkeypatch, mode_pov, prompt_format, expected):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "compose_image_prompt": {
                    "scene": "1girl, arm gripping viewer's shirt collar, pulling the viewer closer, looking at viewer, red hair",
                    "avoid": None,
                }
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        pov=FIRST if mode_pov == "prose" else mode_pov,
        prompt_format=prompt_format,
    )
    assert scene == expected


def test_unmapped_viewer_talk_collapses_rather_than_naming_the_viewers_body():
    # The rewrite is not a nicety: kept verbatim, "the viewer's chest" puts the
    # viewer's own body in a shot taken from behind their eyes. Leaning is contact
    # without a reach, so it fills the frame rather than extending toward it.
    assert composer._rewrite_viewer_contact("1girl, leaning against the viewer's chest") == ("1girl, close-up, foreshortening")
    assert composer._rewrite_viewer_contact("1girl, kissing the viewer") == "1girl, incoming kiss, close-up, foreshortening"
    assert composer._rewrite_viewer_contact("1girl, red hair, looking at viewer") == "1girl, red hair, looking at viewer"
    # Second person is the same viewer by another name.
    assert composer._rewrite_viewer_contact("1girl, grabbing your collar") == (
        "1girl, reaching beyond edge of screen, foreshortening"
    )


def test_naming_the_viewer_without_touching_them_earns_no_arm():
    # The fallback fires on contact, not on the word. An invented reaching arm
    # is a worse failure than a lost chunk, because it draws a limb nobody wrote.
    assert composer._rewrite_viewer_contact("1girl, standing close to the viewer, red hair") == "1girl, red hair"
    assert composer._rewrite_viewer_contact("1girl, blocking the viewer's path") == "1girl"
    # Gaze survives in every phrasing, normalized onto the one tag booru has for it.
    assert composer._rewrite_viewer_contact("1girl, looking up at the viewer") == "1girl, looking at viewer"
    assert composer._rewrite_viewer_contact("1girl, her eyes searching the viewer's face") == "1girl, looking at viewer"
    # Contact without a reach, and a threat without contact: both would be wrong
    # as the fallback's reach past the frame edge -- one flatly, one by being dropped.
    assert composer._rewrite_viewer_contact("1girl, straddling the viewer's lap") == (
        "1girl, on top, straddling, foreshortening"
    )
    assert composer._rewrite_viewer_contact("1girl, pointing a knife at the viewer") == (
        "1girl, aiming at viewer, foreshortening"
    )


def test_short_verb_stems_do_not_false_fire_on_unrelated_words():
    # "pin" and "cup" are real words on their own -- unlike the other stems here,
    # which only collide with their own inflections -- so a costume tag or a prop
    # must not turn into a fabricated incoming arm. The regression that motivated
    # this: both matched as bare substrings before the exclusion was added.
    assert composer._rewrite_viewer_contact("1girl, wearing a pin-up costume near the viewer") == "1girl"
    assert composer._rewrite_viewer_contact("1girl, a cupcake sits beside the viewer") == "1girl"
    # The exclusion must not cost the stem its real inflections.
    assert composer._rewrite_viewer_contact("1girl, pinning the viewer against the wall") == "1girl, close-up, foreshortening"
    assert composer._rewrite_viewer_contact("1girl, cupping the viewer's cheek") == (
        "1girl, reaching beyond edge of screen, foreshortening"
    )


def test_the_viewer_can_be_the_hand_or_the_throat_and_they_do_not_rewrite_alike():
    # Viewer as patient: their throat is not drawn, the grip reaching the lens is.
    assert composer._rewrite_viewer_contact("1girl, hand gripping the viewer's throat") == (
        "1girl, strangling, reaching towards viewer, foreshortening"
    )
    # Viewer as agent: the shot rules ask for this by name when a hand is in frame.
    # Collapsing it would reverse who is choking whom, so only the possessive goes.
    assert composer._rewrite_viewer_contact("1girl, the user's hand gripping her throat, kneeling") == (
        "1girl, pov hands, hand gripping her throat, kneeling"
    )
    # Both directions at once, which is what a struggle actually looks like.
    assert composer._rewrite_viewer_contact("your hand on her throat, her arm gripping your shirt") == (
        "pov hands, hand on her throat, reaching beyond edge of screen, foreshortening"
    )


def test_the_structured_block_states_no_viewpoint_the_composer_could_copy():
    # The compose OOC tells the model to render this block exactly, so anything in
    # it is a candidate for the image prompt. The shot rules belong in the head.
    for mode_pov in (FIRST, THIRD):
        block = _render_scene({"characters": [{"name": "a", "action": "smiling"}]}, mode_pov)
        assert "camera" not in block.lower()
        assert "viewpoint" not in block.lower()
        assert "first-person" not in block.lower() and "third-person" not in block.lower()


def test_viewer_contact_is_emitted_after_the_scene_it_depends_on():
    # Strict decoding emits fields in schema order, so a viewer_contact placed
    # first would make the analyzer rule on the user's hand before it has listed a
    # single character. `required` repeats `properties` order for the same reason.
    params = composer.ANALYZE_TOOL_SCHEMA["function"]["parameters"]
    order = list(params["properties"])
    assert order.index("viewer_contact") > order.index("characters")
    assert order.index("viewer_contact") > order.index("framing")
    assert params["required"] == [key for key in order if key in params["required"]]


async def test_nullish_strings_never_reach_the_scene_or_the_negative(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
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
            }
        ),
    )
    seen: list[str] = []
    monkeypatch.setattr(composer, "_render_scene", lambda a, p: seen.append(_render_scene(a, p)) or seen[-1])
    scene, avoid, _ = await compose_scene(
        client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, scene_analysis=True
    )
    assert avoid == ""
    assert "null" not in seen[0].casefold() and "none" not in seen[0].casefold()
    assert seen[0].splitlines()[-1] == "setting and framing: garden"


async def test_no_negative_workflow_tells_model_to_leave_avoid_empty(monkeypatch):
    seen: list[str] = []

    async def spy(*, tool_name, tail_messages, **kwargs):
        seen.extend(m["content"] for m in tail_messages)
        yield {"type": "result", "args": {"scene": "1girl", "profile_owner_visible": False}}

    monkeypatch.setattr(composer, "forced_tool_call", spy)
    await compose_scene(client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, supports_negative=False)
    assert any(composer._LEAVE_AVOID_EMPTY in c for c in seen)


async def test_analysis_avoid_items_ride_avoid(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [{"name": "Ashley", "sex": "girl", "appearance": "", "action": "walking away"}],
                    "avoid": "looking at viewer",
                },
                "compose_image_prompt": {
                    "scene": "1girl, from behind",
                    "avoid": "blur",
                    "profile_owner_visible": False,
                },
            }
        ),
    )
    _, avoid, _ = await compose_scene(
        client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, scene_analysis=True
    )
    assert avoid == "blur, looking at viewer"


async def test_empty_analysis_reports_analysis_failed(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced({"analyze_scene": {}, "compose_image_prompt": {"scene": "1girl"}}),
    )
    _, _, mode = await compose_scene(
        client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, scene_analysis=True
    )
    assert mode == "analysis_failed"


async def test_both_calls_ride_the_prefix_unchanged_with_shared_tool_blob(monkeypatch):
    """KV-cache contract: analyze and compose send the byte-identical shared
    prefix (per-call instructions ride only the tail) and ship the same
    workflow-local tools blob, forcing one via tool_choice -- the pipeline
    pattern. A chat model needs the real tool to call it; forcing via tools=None
    is unreliable (Gemma) or rejected (DeepSeek). In text mode the schemas still
    never render, so the cached conversation KV survives across the two calls."""
    calls: list[dict] = []

    def recording(results):
        inner = _fake_forced(results)

        def fake(**kwargs):
            calls.append(kwargs)
            return inner(tool_name=kwargs["tool_name"])

        return fake

    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        recording(
            {
                "analyze_scene": {"characters": [{"name": "a", "sex": "girl", "action": "waving"}]},
                "compose_image_prompt": {"scene": "1girl, waving", "avoid": None},
            }
        ),
    )
    prefix = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "she waves"}]
    await compose_scene(
        client=None, model_name="agent-m", prefix=prefix, settings={"model_name": "writer"}, scene_analysis=True
    )
    assert [c["tool_name"] for c in calls] == ["analyze_scene", "compose_image_prompt"]
    for call in calls:
        assert call["prefix"] is prefix
        assert call["model_name"] == "agent-m"
        assert call["offer_tools"] == ("analyze_scene", "compose_image_prompt")
        assert call.get("tools_in_prompt", True) is not False  # ship the tools, never tools=None
        for msg in call["tail_messages"]:
            assert msg["role"] == "user"


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
        await compose_scene(
            client=None,
            model_name="agent-m",
            prefix=[],
            settings={
                "model_name": "writer",
                "reasoning_enabled_passes": {
                    "director": not reasoning_on,
                    "writer": not reasoning_on,
                    "editor": not reasoning_on,
                },
            },
            reasoning_on=reasoning_on,
            scene_analysis=True,
        )
        assert [c["tool_name"] for c in calls] == ["analyze_scene", "compose_image_prompt"]
        assert all(c["reasoning_on"] is reasoning_on for c in calls)
        assert all(c["model_name"] == "agent-m" for c in calls)


async def test_single_call_strips_negation_from_scene(monkeypatch):
    # No analysis path here: negation hygiene must still run, or CLIP draws the
    # item. The negation phrase sits mid-chunk, so a start-anchored match misses it.
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced({"compose_image_prompt": {"scene": "1girl, red dress, not wearing shoes, garden", "avoid": None}}),
    )
    scene, _, mode = await compose_scene(client=None, model_name="m", prefix=[], settings={"model_name": "writer"})
    assert scene == "1girl, red dress, garden"
    assert mode == "single_call"


async def test_prose_strips_leaked_booru_count_prefix_without_touching_natural_cast_prose(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "compose_image_prompt": {
                    "scene": "1girl, solo. Iris sits beside the window. Two women cross the garden behind her.",
                    "avoid": None,
                    "profile_owner_visible": False,
                }
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        prompt_format="prose",
    )
    assert scene == "Iris sits beside the window. Two women cross the garden behind her."


async def test_scene_analysis_never_pins_a_count_anchor_into_prose(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [
                        {"name": "Iris", "sex": "girl", "action": "standing"},
                        {"name": "Ren", "sex": "boy", "action": "sitting"},
                    ],
                },
                "compose_image_prompt": {
                    "scene": "1girl, 1boy. Iris stands beside Ren. Ren sits on a bench.",
                    "avoid": None,
                    "profile_owner_visible": False,
                },
            }
        ),
    )
    scene, _, mode = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        prompt_format="prose",
        scene_analysis=True,
    )
    assert scene == "Iris stands beside Ren. Ren sits on a bench."
    assert mode == "scene_analysis"


async def test_single_call_inserts_named_profile_only_when_owner_is_visible(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "compose_image_prompt": {
                    "scene": "1girl, solo, sitting by a window",
                    "avoid": None,
                    "profile_owner_visible": True,
                }
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="long silver hair, blue eyes",
        profile_owner_name="Iris",
        prompt_format="hybrid",
    )
    assert scene == "1girl, solo, Iris: long silver hair, blue eyes, sitting by a window"

    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "compose_image_prompt": {
                    "scene": "1boy, solo, standing in a doorway",
                    "avoid": None,
                    "profile_owner_visible": False,
                }
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="long silver hair, blue eyes",
        profile_owner_name="Iris",
    )
    assert "silver hair" not in scene


@pytest.mark.parametrize(
    ("prompt_format", "expected"),
    (
        ("tags", "2girls, long silver hair, blue eyes, Iris sits beside Ashley."),
        (
            "hybrid",
            "2girls, Iris: long silver hair, blue eyes, Iris sits beside Ashley.",
        ),
        (
            "prose",
            "Iris has these traits: long silver hair, blue eyes. Iris sits beside Ashley.",
        ),
    ),
)
async def test_profile_appearance_binding_follows_prompt_format(monkeypatch, prompt_format, expected):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "compose_image_prompt": {
                    "scene": "2girls, Iris sits beside Ashley.",
                    "avoid": None,
                    "profile_owner_visible": True,
                }
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="long silver hair, blue eyes",
        profile_owner_name="Iris",
        prompt_format=prompt_format,
    )
    assert scene == expected


async def test_profile_appearance_negation_cannot_bypass_scene_cleanup(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "compose_image_prompt": {
                    "scene": "1girl, solo, sitting",
                    "avoid": None,
                    "profile_owner_visible": True,
                }
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="long silver hair, not wearing glasses, blue eyes",
        profile_owner_name="Iris",
        prompt_format="hybrid",
    )
    assert scene == "1girl, solo, Iris: long silver hair, blue eyes, sitting"


async def test_analysis_owns_profile_visibility_and_preserves_scene_fields(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced_capturing(
            {
                "analyze_scene": {
                    "characters": [
                        {
                            "name": "Iris",
                            "is_profile_owner": True,
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
                    "profile_owner_visible": False,
                },
            },
            captured,
        ),
    )
    scene, avoid, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="long silver hair",
        profile_owner_name="Iris",
        scene_analysis=True,
    )
    assert scene.startswith("1girl, solo,")
    assert scene.startswith("1girl, solo, Iris: long silver hair,")  # identity stays near the prompt head
    assert scene.endswith("medium shot")
    assert avoid == "looking at viewer"
    structured_tail = captured["compose_image_prompt"]
    assert "expression: smiling" in structured_tail
    assert "gaze: looking at Ashley" in structured_tail
    assert "interaction: Iris holds Ashley's hand" in structured_tail
    assert "medium shot" in structured_tail


async def test_back_shot_strips_face_traits_from_injected_appearance(monkeypatch):
    # Malina flies away: the analyzer flags her face hidden, so the injected fixed
    # sheet must lose its face-only traits (eyes, eyeliner) but keep hair/wings.
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [
                        {
                            "name": "Malina",
                            "is_profile_owner": True,
                            "sex": "girl",
                            "appearance": None,
                            "action": "flying upward away from the viewer",
                            "face_visible": False,
                            "expression": None,
                        }
                    ],
                },
                "compose_image_prompt": {"scene": "flying upward away from the viewer", "avoid": None},
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="jet-black wings, long silky black hair, glowing purple eyes, black eyeliner, black nails",
        profile_owner_name="Malina",
        prompt_format="tags",
        scene_analysis=True,
    )
    assert "eyes" not in scene and "eyeliner" not in scene
    assert "jet-black wings" in scene and "long silky black hair" in scene and "black nails" in scene


async def test_visible_face_keeps_all_traits(monkeypatch):
    # Same sheet, face toward camera -> nothing stripped.
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [
                        {"name": "Malina", "is_profile_owner": True, "sex": "girl", "action": "standing", "face_visible": True}
                    ],
                },
                "compose_image_prompt": {"scene": "standing", "avoid": None},
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="glowing purple eyes, black eyeliner",
        profile_owner_name="Malina",
        prompt_format="tags",
        scene_analysis=True,
    )
    assert "glowing purple eyes" in scene and "black eyeliner" in scene


async def test_analysis_does_not_insert_off_frame_profile(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "characters": [{"name": "Ashley", "is_profile_owner": False, "sex": "girl", "action": "reading"}],
                    "setting": "library",
                },
                "compose_image_prompt": {
                    "scene": "reading in a library",
                    "avoid": None,
                    # The structured cast is authoritative.
                    "profile_owner_visible": True,
                },
            }
        ),
    )
    scene, _, _ = await compose_scene(
        client=None,
        model_name="m",
        prefix=[],
        settings={"model_name": "writer"},
        appearance="long silver hair",
        profile_owner_name="Iris",
        scene_analysis=True,
    )
    assert "silver hair" not in scene


def _fake_forced_capturing(results: dict, captured: dict):
    async def fake(*, tool_name, tail_messages=None, **kwargs):
        captured[tool_name] = " ".join(m["content"] for m in tail_messages or [])
        yield {"type": "result", "args": results.get(tool_name, {})}

    return fake


async def test_failed_compose_stops_instead_of_shipping_the_reply(monkeypatch):
    # Every forced call returns empty args -> no scene. The composer must stop,
    # never fall back to the raw reply text as the image prompt (prose the
    # tag-trained checkpoints render as mush).
    monkeypatch.setattr(composer, "forced_tool_call", _fake_forced({}))
    with pytest.raises(ValueError, match="couldn't compose an image prompt"):
        await compose_scene(client=None, model_name="m", prefix=[], settings={"model_name": "writer"})


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
        config,
        "prose",
        {"appearance_prompt": ""},
        "1girl, solo. Iris sits beside the window.",
        "",
    )
    assert positive == "cinematic photograph, natural light, Iris sits beside the window."
    assert "1girl" not in positive
    assert "solo" not in positive
