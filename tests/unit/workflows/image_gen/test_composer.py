from __future__ import annotations

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
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="x", scene_analysis=True
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
    scene, _, mode, _ = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="x", scene_analysis=True
    )
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
    scene, avoid, _, _ = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="x", scene_analysis=True
    )
    assert scene == "1girl, solo, silk dress"
    assert avoid == "blur, slippers"


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
    _, _, _, include_appearance = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="x", scene_analysis=True
    )
    assert not include_appearance


async def test_empty_analysis_reports_analysis_failed(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced({"analyze_scene": {}, "compose_image_prompt": {"scene": "1girl"}}),
    )
    _, _, mode, include_appearance = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="x", scene_analysis=True
    )
    assert mode == "analysis_failed"
    assert include_appearance  # no cast knowledge -> keep the old behavior


async def test_failed_compose_falls_back_to_anchor_excerpt(monkeypatch):
    monkeypatch.setattr(composer, "forced_tool_call", _fake_forced({}))
    scene, _, mode, _ = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="she stood by the window"
    )
    assert scene == "she stood by the window"
    assert mode == "fallback_excerpt"


def test_assemble_strips_profile_counts():
    config = {
        "external_comfy": {
            "styles": [{"id": "anime", "label": "Anime", "prompt": "", "negative_prompt": "", "checkpoint": "", "workflow": ""}]
        }
    }
    positive, _, _ = composer.assemble_prompts(
        config, "anime", {"appearance_prompt": "1girl, solo, long red hair"}, "2girls, garden", ""
    )
    assert positive == "long red hair, 2girls, garden, anime illustration, clean line art, very aesthetic, high contrast"
