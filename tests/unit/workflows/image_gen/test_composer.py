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
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="x", scene_analysis=True
    )
    assert scene == "1girl, waving"
    assert mode == "scene_analysis"


async def test_empty_analysis_degrades_to_single_call(monkeypatch):
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced({"analyze_scene": {}, "compose_image_prompt": {"scene": "1girl"}}),
    )
    _, _, mode = await compose_scene(client=None, prefix=[], settings={"model_name": "m"}, anchor_text="x", scene_analysis=True)
    assert mode == "single_call"


async def test_failed_compose_falls_back_to_anchor_excerpt(monkeypatch):
    monkeypatch.setattr(composer, "forced_tool_call", _fake_forced({}))
    scene, _, mode = await compose_scene(
        client=None, prefix=[], settings={"model_name": "m"}, anchor_text="she stood by the window"
    )
    assert scene == "she stood by the window"
    assert mode == "fallback_excerpt"
