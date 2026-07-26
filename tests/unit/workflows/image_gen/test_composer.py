from __future__ import annotations

import pytest

from backend.workflows.image_gen import composer
from backend.workflows.image_gen.composer import _render_scene, compose_scene


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
        }
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
                    "expression": "annoyed",
                    "gaze": "looking ahead",
                }
            ]
        }
    )
    line = block.splitlines()[0]
    assert line == "Malina: from behind, facing away, flying away, gaze: looking ahead"
    assert "expression" not in line  # no expression readable off the back of a head


def test_render_scene_marks_first_person():
    block = _render_scene({"viewpoint": "first_person", "characters": [{"name": "a", "action": "smiling"}]})
    assert block.splitlines()[0].startswith("viewpoint: first-person")
    assert _render_scene({"viewpoint": "third_person", "characters": [{"name": "a", "action": "x"}]}).startswith(
        "viewpoint: third-person"
    )


def test_render_scene_tolerates_junk_and_empties():
    assert _render_scene(None) == ""
    assert _render_scene({"characters": ["not-a-dict", {}]}) == ""  # no bits -> character dropped


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
                    "viewpoint": "first_person",
                    "characters": [{"name": "Ashley", "sex": "girl", "action": "smiling"}],
                },
                # Composer leaks the camera character into the count anchor, and a pov tag with it.
                "compose_image_prompt": {"scene": "1boy 1girl, pov, long red hair, smiling", "avoid": None},
            }
        ),
    )
    scene, _, mode = await compose_scene(
        client=None, model_name="m", prefix=[], settings={"model_name": "writer"}, scene_analysis=True
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
                    "viewpoint": "first_person",
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
    )
    # Bystander stripped: count anchor is solo, and the composer's leaked counts are pinned over.
    assert scene == "1girl, solo, smiling"


def test_keep_profile_owner_no_op_when_owner_absent():
    analysis = {"characters": [{"name": "stranger", "sex": "girl"}]}
    composer._keep_profile_owner(analysis, "Ashley")
    assert analysis["characters"] == [{"name": "stranger", "sex": "girl"}]


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
                    "viewpoint": "third_person",
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
    assert scene == "1girl, solo, sitting by a window, Iris: long silver hair, blue eyes"

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
        ("tags", "2girls, Iris sits beside Ashley., long silver hair, blue eyes"),
        (
            "hybrid",
            "2girls, Iris sits beside Ashley., Iris: long silver hair, blue eyes",
        ),
        (
            "prose",
            "2girls, Iris sits beside Ashley. Iris has these traits: long silver hair, blue eyes.",
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
    assert scene == "1girl, solo, sitting, Iris: long silver hair, blue eyes"


async def test_analysis_owns_profile_visibility_and_preserves_scene_fields(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced_capturing(
            {
                "analyze_scene": {
                    "viewpoint": "third_person",
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
    assert scene.endswith("Iris: long silver hair")  # appearance seated after the pose body
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
                    "viewpoint": "third_person",
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
                    "viewpoint": "third_person",
                    "characters": [{"name": "Malina", "is_profile_owner": True, "sex": "girl", "face_visible": True}],
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
                    "viewpoint": "third_person",
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
        "external_comfy": {
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
    }
    positive, _, _ = composer.assemble_prompts(
        config, "anime", {"appearance_prompt": "1girl, solo, long red hair"}, "2girls, garden", ""
    )
    assert positive == "2girls, anime illustration, clean line art, very aesthetic, high contrast, garden"
