from __future__ import annotations

import pytest

from backend.workflows.image_gen import composer, prompts
from backend.workflows.image_gen.composer import (
    SkillSelection,
    addressable_subjects,
    assemble_prompts,
    compose_scene,
    read_image_skills,
)
from backend.workflows.image_gen.pov import BACKGROUND, FIRST, THIRD
from backend.workflows.image_gen.subjects import Subject


def _subject(name: str, appearance: str = "") -> Subject:
    return Subject(member_id=f"member-{name}", card_id=f"card-{name}", name=name, profile={"appearance_prompt": appearance})


def _skill(skill_id: str, *, enabled: bool = True, instructions: str | None = None) -> dict:
    return {
        "id": skill_id,
        "label": skill_id.replace("_", " ").title(),
        "description": f"Choose {skill_id} when applicable.",
        "instructions": instructions if instructions is not None else f"FULL BODY {skill_id}",
        "enabled": enabled,
    }


def _fake_forced(results: dict, calls: list[dict] | None = None):
    async def fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        value = results.get(kwargs["tool_name"], {})
        if isinstance(value, Exception):
            raise value
        yield {"type": "result", "args": value}

    return fake


async def _select(monkeypatch, result, *, skills=(), subjects=(), calls=None, **kwargs):
    monkeypatch.setattr(composer, "forced_tool_call", _fake_forced({"read_image_skills": result}, calls))
    return await read_image_skills(
        client=object(),
        model_name="agent",
        prefix=(),
        settings={"model_name": "writer"},
        skills=skills,
        subjects=subjects,
        **kwargs,
    )


async def _compose(monkeypatch, result, *, calls=None, **kwargs):
    monkeypatch.setattr(composer, "forced_tool_call", _fake_forced({"compose_image_prompt": result}, calls))
    return await compose_scene(client=object(), model_name="agent", prefix=(), settings={"model_name": "writer"}, **kwargs)


def test_tool_contract_and_offered_order_are_stable():
    params = prompts.READ_IMAGE_SKILLS_SCHEMA["function"]["parameters"]
    assert list(params["properties"]) == ["skill_ids", "visible_subjects"]
    assert params["required"] == ["skill_ids", "visible_subjects"]
    assert params["properties"]["skill_ids"]["maxItems"] == 4
    assert prompts.OFFER_TOOLS == ("read_image_skills", "compose_image_prompt", "refine_image_prompt")


async def test_review_reason_is_not_logged(monkeypatch, caplog):
    monkeypatch.setattr(
        composer, "forced_tool_call", _fake_forced({"refine_image_prompt": {"critique": "SECRET REVIEW", "done": True}})
    )
    with caplog.at_level("INFO", logger=composer.__name__):
        result = await composer._forced_result(
            client=object(),
            model_name="agent",
            prefix=(),
            tail=(),
            tool_name="refine_image_prompt",
            settings={"model_name": "writer"},
            reasoning_on=False,
        )
    assert result["args"]["critique"] == "SECRET REVIEW"
    assert "SECRET REVIEW" not in caplog.text


async def test_selector_sees_only_enabled_summaries_names_and_pov(monkeypatch):
    calls: list[dict] = []
    enabled = _skill("hug", instructions="SECRET HUG BODY")
    disabled = _skill("fight", enabled=False, instructions="SECRET FIGHT BODY")
    result = await _select(
        monkeypatch,
        {"skill_ids": ["hug"], "visible_subjects": ["Iris"]},
        skills=[enabled, disabled],
        subjects=[_subject("Iris", "SECRET APPEARANCE")],
        pov=FIRST,
        calls=calls,
    )

    assert result.valid is True
    assert [skill["id"] for skill in result.skills] == ["hug"]
    tail = calls[0]["tail_messages"][0]["content"]
    assert "hug | Hug | Choose hug when applicable." in tail
    assert "fight" not in tail
    assert "SECRET HUG BODY" not in tail
    assert "SECRET FIGHT BODY" not in tail
    assert "SECRET APPEARANCE" not in tail
    assert "Iris" in tail and "first_person" in tail


async def test_selection_filters_caps_deduplicates_and_restores_library_order(monkeypatch):
    skills = [_skill(name) for name in ("a", "b", "c", "d", "e")]
    selection = await _select(
        monkeypatch, {"skill_ids": ["e", "unknown", "c", "e", "b", "d", "a"], "visible_subjects": []}, skills=skills
    )
    # Four valid unique requests survive, then library order controls injection.
    assert [skill["id"] for skill in selection.skills] == ["b", "c", "d", "e"]
    assert selection.visible_subjects == ()
    assert selection.valid is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"skill_ids": "hug", "visible_subjects": []},
        {"skill_ids": [], "visible_subjects": "Iris"},
        {"skill_ids": [1], "visible_subjects": []},
    ],
)
async def test_malformed_selection_degrades_to_broad_behavior(monkeypatch, payload):
    selection = await _select(monkeypatch, payload, skills=[_skill("hug")])
    assert selection == SkillSelection()


async def test_selector_exception_and_empty_usable_library_skip_safely(monkeypatch):
    failed = await _select(monkeypatch, RuntimeError("offline"), skills=[_skill("hug")])
    assert failed == SkillSelection()

    calls: list[dict] = []
    skipped = await _select(
        monkeypatch,
        {"skill_ids": [], "visible_subjects": []},
        skills=[_skill("off", enabled=False), _skill("blank", instructions=" ")],
        calls=calls,
    )
    assert skipped == SkillSelection()
    assert calls == []


async def test_composer_receives_only_selected_bodies_in_library_order(monkeypatch):
    calls: list[dict] = []
    selected = [_skill("first", instructions="FIRST INSTRUCTION"), _skill("second", instructions="SECOND INSTRUCTION")]
    scene, avoid, mode = await _compose(
        monkeypatch,
        {"scene": "1girl, solo, hugging", "avoid": "frontal view", "visible_subjects": []},
        selected_skills=selected,
        visible_subjects=["Iris"],
        subjects=[_subject("Iris", "silver hair")],
        extra_instructions="STYLE EXTRA",
        calls=calls,
    )

    assert "silver hair" in scene
    assert avoid == "frontal view"
    assert mode == "scene_skills"
    tail = calls[0]["tail_messages"][0]["content"]
    assert tail.index("FIRST INSTRUCTION") < tail.index("SECOND INSTRUCTION")
    assert tail.index("SECOND INSTRUCTION") < tail.index("STYLE EXTRA")
    assert tail.index("STYLE EXTRA") < tail.index("Give each character's pose")


@pytest.mark.parametrize("also_sent", [False, True])
async def test_prompter_reference_requests_the_current_story_instead_of_a_caption(monkeypatch, also_sent):
    calls: list[dict] = []
    await _compose(
        monkeypatch,
        {"scene": "1girl, solo, standing", "avoid": None, "visible_subjects": []},
        prompter_reference_url="data:image/png;base64,earlier-picture",
        prompter_reference_sent=also_sent,
        calls=calls,
    )
    image, request = calls[0]["tail_messages"][0]["content"]
    assert image["type"] == "image_url"
    instruction = request["text"]
    assert "Do not describe or recreate that picture" in instruction
    assert "final visible instant of the latest assistant reply" in instruction
    assert "continuity details the story leaves unchanged" in instruction
    assert "Replace any pictured detail the latest reply changes" in instruction
    assert ("updates the picture to the current story" in instruction) is also_sent
    review = prompts.refine_ooc(1, 1, prompter_reference=True)
    assert "earlier chat picture only for details the story leaves unchanged" in review


async def test_composer_uses_its_own_visibility_without_a_valid_selector(monkeypatch):
    scene, _, mode = await _compose(
        monkeypatch,
        {"scene": "1girl, solo, window", "avoid": None, "visible_subjects": ["Ashley"]},
        subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red hair")],
    )
    assert "red hair" in scene and "silver hair" not in scene
    assert mode == "single_call"


async def test_valid_selector_visibility_overrides_composer_visibility(monkeypatch):
    scene, _, _ = await _compose(
        monkeypatch,
        {"scene": "1girl, solo, window", "avoid": None, "visible_subjects": ["Ashley"]},
        subjects=[_subject("Iris", "silver hair"), _subject("Ashley", "red hair")],
        visible_subjects=["Iris"],
    )
    assert "silver hair" in scene and "red hair" not in scene


def test_selector_visibility_controls_reference_subjects_exactly():
    subjects = [_subject("Iris"), _subject("Ashley"), _subject("Ren")]
    assert [s.name for s in addressable_subjects(subjects, ["Ashley", "unknown"])] == ["Ashley"]
    assert addressable_subjects(subjects, []) == ()
    assert addressable_subjects(subjects, None) == tuple(subjects)


async def test_reasoning_and_offer_order_reach_both_calls_without_a_budget_override(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        composer,
        "forced_tool_call",
        _fake_forced(
            {
                "read_image_skills": {"skill_ids": ["hug"], "visible_subjects": ["Iris"]},
                "compose_image_prompt": {"scene": "1girl, solo", "avoid": None, "visible_subjects": []},
            },
            calls,
        ),
    )
    selection = await read_image_skills(
        client=object(),
        model_name="agent",
        prefix=(),
        settings={},
        skills=[_skill("hug")],
        reasoning_on=True,
        subjects=[_subject("Iris")],
    )
    await compose_scene(
        client=object(),
        model_name="agent",
        prefix=(),
        settings={},
        selected_skills=selection.skills,
        visible_subjects=selection.visible_subjects,
        reasoning_on=True,
        subjects=[_subject("Iris")],
    )

    assert [call["tool_name"] for call in calls] == ["read_image_skills", "compose_image_prompt"]
    # The budget is forced_tool_call's configured one; neither step overrides it.
    assert not any("token_floor" in call or "max_tokens" in call for call in calls)
    assert all(call["reasoning_on"] is True for call in calls)
    assert all(call["offer_tools"] == prompts.OFFER_TOOLS for call in calls)


async def test_background_draws_a_place_card_as_the_setting_with_zero_count_first(monkeypatch):
    scene, _, _ = await _compose(
        monkeypatch,
        {"scene": "no humans, scenery, empty hallway, buzzing lights", "avoid": None, "visible_subjects": ["Backrooms"]},
        prompt_format="tags",
        pov=BACKGROUND,
        subjects=[_subject("Backrooms", "yellow wallpaper, damp carpet")],
    )
    # The place's own sheet rides along, and the zero count stays ahead of the style block exactly as "1girl" would.
    positive, _ = assemble_prompts({"prompt_format": "tags", "prompt": "masterpiece"}, {}, scene, "")
    assert positive == "no humans, masterpiece, yellow wallpaper, damp carpet, scenery, empty hallway, buzzing lights"


async def test_empty_composition_remains_failure_critical(monkeypatch):
    with pytest.raises(ValueError, match="couldn't compose"):
        await _compose(monkeypatch, {"scene": "", "avoid": None, "visible_subjects": []}, pov=THIRD)
