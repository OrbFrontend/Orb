from __future__ import annotations

import pytest

from backend.workflows.image_gen import composer
from backend.workflows.image_gen.composer import _render_scene, compose_scene


def test_render_scene_lays_out_each_character_with_outfit_delta_and_position():
    block = _render_scene(
        {
            "characters": [
                {
                    "name": "Ashley",
                    "appearance": "",
                    "outfit_added": "silk dress",
                    "outfit_removed": "slippers",
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
    assert lines[0] == "Ashley: wearing silk dress, no longer wearing slippers, left, holding a book, sitting, reading"
    assert lines[1] == "nobleman: tall man, dark hair, right, behind her"
    assert lines[2] == "setting: medieval garden, midday, stone bench"


def test_render_scene_marks_first_person_pov():
    block = _render_scene({"viewpoint": "first_person", "characters": [{"name": "a", "action": "smiling"}]})
    assert block.splitlines()[0].startswith("viewpoint: first-person POV")
    # third_person adds no viewpoint line
    assert "viewpoint" not in _render_scene({"viewpoint": "third_person", "characters": [{"name": "a", "action": "x"}]})


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
    scene, avoid, mode, include_appearance = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, scene_analysis=True
    )
    assert scene == "1girl, waving"  # no sex reported -> anchor not pinned, scene untouched
    assert mode == "scene_analysis"
    assert include_appearance  # empty appearance marks the main character in frame


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
                # Composer leaks the camera character into the count anchor.
                "compose_image_prompt": {"scene": "1boy 1girl, long red hair, smiling", "avoid": None},
            }
        ),
    )
    scene, _, mode, _ = await compose_scene(client=None, prefix=[], settings={"model_name": "m"}, scene_analysis=True)
    assert scene == "1girl, solo, pov, long red hair, smiling"
    assert mode == "scene_analysis"


async def test_removed_outfit_rides_avoid_not_scene(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "viewpoint": "third_person",
                    "characters": [{"name": "Ashley", "sex": "girl", "appearance": "", "outfit_removed": "slippers"}],
                },
                # Composer copies the negation through; CLIP would draw the slippers.
                "compose_image_prompt": {"scene": "1girl, silk dress, no longer wearing slippers", "avoid": "blur"},
            }
        ),
    )
    scene, avoid, _, _ = await compose_scene(client=None, prefix=[], settings={"model_name": "m"}, scene_analysis=True)
    assert scene == "1girl, solo, silk dress"
    assert avoid == "blur, slippers"


async def test_hidden_elements_ride_avoid(monkeypatch):
    # `hidden` (present but not visible -- turned away, occluded, cropped) feeds
    # the negative so the checkpoint doesn't invent it (e.g. a face on a back view).
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "analyze_scene": {
                    "viewpoint": "third_person",
                    "characters": [{"name": "Ashley", "sex": "girl", "appearance": "", "action": "walking away"}],
                    "hidden": "looking at viewer, face",
                },
                "compose_image_prompt": {"scene": "1girl, from behind", "avoid": "blur"},
            }
        ),
    )
    _, avoid, _, _ = await compose_scene(client=None, prefix=[], settings={"model_name": "m"}, scene_analysis=True)
    assert avoid == "blur, looking at viewer, face"


async def test_main_character_off_frame_drops_profile_appearance(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                # Every visible character has their own appearance -> main char off-frame.
                "analyze_scene": {"characters": [{"name": "guard", "sex": "boy", "appearance": "tall, armored"}]},
                "compose_image_prompt": {"scene": "1boy, tall, armored, at the gate", "avoid": None},
            }
        ),
    )
    _, _, _, include_appearance = await compose_scene(client=None, prefix=[], settings={"model_name": "m"}, scene_analysis=True)
    assert not include_appearance


async def test_empty_analysis_reports_analysis_failed(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced({"analyze_scene": {}, "compose_image_prompt": {"scene": "1girl"}}),
    )
    _, _, mode, include_appearance = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, scene_analysis=True
    )
    assert mode == "analysis_failed"
    assert include_appearance  # no cast knowledge -> keep the old behavior


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
    await compose_scene(client=None, prefix=prefix, settings={"model_name": "m"}, scene_analysis=True)
    assert [c["tool_name"] for c in calls] == ["analyze_scene", "compose_image_prompt"]
    for call in calls:
        assert call["prefix"] is prefix
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


async def test_reasoning_mode_inherits_editor_and_ignores_director(monkeypatch):
    """Both off-turn calls track the editor's reasoning lane, per _reasoning_on.

    The image-gen call rides the writer/editor thinking-off lane so it reuses the
    turn's cached conversation prefix on a reasoning-forking provider (kv-cache §9).
    It must follow the editor flag specifically, not the director's: when a user
    enables director reasoning (writer/editor stay off), tracking the director would
    fork onto a thinking-on lane the anchor reply was never warmed in.
    """
    # editor on -> both calls reason, regardless of the director flag.
    calls = _record_forced_calls(monkeypatch)
    await compose_scene(
        client=None,
        prefix=[],
        settings={"model_name": "m", "reasoning_enabled_passes": {"director": False, "editor": True}},
        scene_analysis=True,
    )
    assert [c["tool_name"] for c in calls] == ["analyze_scene", "compose_image_prompt"]
    assert all(c["reasoning_on"] is True for c in calls)

    # director on, editor off -> both calls stay off (never inherit the director).
    calls = _record_forced_calls(monkeypatch)
    await compose_scene(
        client=None,
        prefix=[],
        settings={"model_name": "m", "reasoning_enabled_passes": {"director": True, "editor": False}},
        scene_analysis=True,
    )
    assert all(c["reasoning_on"] is False for c in calls)


async def test_reasoning_mode_defaults_off_when_config_absent_or_malformed(monkeypatch):
    """Missing/malformed reasoning config degrades to off (the writer/editor default)."""
    for passes in (None, "junk", {}):
        settings = {"model_name": "m"}
        if passes is not None:
            settings["reasoning_enabled_passes"] = passes
        calls = _record_forced_calls(monkeypatch)
        await compose_scene(client=None, prefix=[], settings=settings, scene_analysis=True)
        assert all(c["reasoning_on"] is False for c in calls), passes


async def test_failed_compose_stops_instead_of_shipping_the_reply(monkeypatch):
    # Every forced call returns empty args -> no scene. The composer must stop,
    # never fall back to the raw reply text as the image prompt (prose the
    # tag-trained checkpoints render as mush).
    monkeypatch.setattr(composer, "forced_tool_call", _fake_forced({}))
    with pytest.raises(ValueError, match="couldn't compose an image prompt"):
        await compose_scene(client=None, prefix=[], settings={"model_name": "m"})


def test_assemble_strips_profile_counts():
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
    assert positive == "long red hair, 2girls, garden, anime illustration, clean line art, very aesthetic, high contrast"
