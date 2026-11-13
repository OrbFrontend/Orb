"""The assistant's fragment creates and setters, and the names, projections and checks its fragment tools share."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator

from ....core import (
    DECISION_FIELD_TYPE,
    DECISION_RESOLUTIONS_BY_TYPE,
    DEFAULT_STATE_MODE,
    DEFAULT_STATE_TEMPLATE,
    MAX_SCORE_LEVELS,
    MIN_SCORE_LEVELS,
    RESERVED_FRAGMENT_IDS,
    STATE_FIELD_TYPE,
    outcome_keys_of,
    outcome_text,
    with_outcome_text,
)
from ....core.settings import Settings
from ....database import (
    create_interactive_fragment,
    create_mood_fragment,
    get_interactive_fragments,
    get_mood_fragments,
    get_settings,
    reorder_interactive_fragments,
    update_interactive_fragment,
    update_mood_fragment,
)
from ....database.models import InteractiveFragmentRow, MoodFragmentRow
from ..contracts import AssistantTool, ToolArguments, ToolFailure, ToolOutput, ToolSet
from .host import AppReads, AssistantHost, HostRefusal
from .targets import clip, fragment_field, interactive_fragment_of, mood_fragment_of, not_blank, quoted
from .toggles import switch_summary

# The stages an interactive fragment runs in, as the fragment list groups them; order counts within a stage.
Stage = Literal["judge", "director", "state", "editor"]
_EDITOR_FIELD_TYPES = frozenset(("feedback", "post_processing"))

# The field types under the names the fragment editor shows; Single and List are stored as string and array.
FIELD_TYPE_NAMES = {
    "string": "single",
    "array": "list",
    "feedback": "feedback",
    "state": "state",
    "post_processing": "post_processing",
    "decision": "decision",
}
_STORED_TYPES = {name: stored for stored, name in FIELD_TYPE_NAMES.items()}
DECISION_TYPE_NAMES = {"noul": "yes_no", "choice": "choice", "score": "score"}
_STORED_DECISION_TYPES = {name: stored for stored, name in DECISION_TYPE_NAMES.items()}
STAGES: tuple[Stage, ...] = ("judge", "director", "state", "editor")

# An interactive fragment's text fields under the names the tools take, and the columns they live in.
INTERACTIVE_TEXT_COLUMNS = {
    "label": "label",
    "description": "description",
    "injection_label": "injection_label",
    "gate_question": "post_processing_gate",
    "question": "decision_instructions",
    "situation": "decision_state_template",
}
OUTCOME_PARTS = {"outcome_meaning": "meaning", "outcome_guidance": "guidance"}
MOOD_TEXT_FIELDS = ("label", "description", "prompt_text", "negative_prompt")
# The longest each text may be, as the fragment editor holds them.
TEXT_CAPS = {
    "label": 200,
    "injection_label": 200,
    "description": 10_000,
    "prompt_text": 50_000,
    "negative_prompt": 50_000,
    "gate_question": 4000,
    "question": 8000,
    "situation": 16_000,
    "outcome_meaning": 4000,
    "outcome_guidance": 4000,
}
# Text a fragment cannot be left without.
REQUIRED_TEXT = frozenset(("label", "prompt_text", "injection_label", "question", "situation", "outcome_meaning"))
# Which field types each interactive text field belongs to; the rest belong to every type.
_FIELD_TYPES_OF = {
    "description": frozenset(set(FIELD_TYPE_NAMES) - {DECISION_FIELD_TYPE}),
    "gate_question": frozenset({"post_processing"}),
    "question": frozenset({DECISION_FIELD_TYPE}),
    "situation": frozenset({DECISION_FIELD_TYPE}),
    "outcome_meaning": frozenset({DECISION_FIELD_TYPE}),
    "outcome_guidance": frozenset({DECISION_FIELD_TYPE}),
}

_LABEL_CHARS = TEXT_CAPS["label"]
_ARG_TEXT_CHARS = 8000
_OPTION_CHARS = 64
_OUTCOMES = 20
_SLUG_CHARS = 48

Switch = Literal["unchanged", "on", "off"]
_SWITCH = "on or off; unchanged leaves it as it is."
_Outcome = Annotated[str, StringConstraints(max_length=TEXT_CAPS["outcome_meaning"])]
_OptionName = Annotated[str, StringConstraints(min_length=1, max_length=_OPTION_CHARS)]
_TYPE_HELP = (
    "single: one value the Director writes each turn. list: several values. feedback: a note to the user after the reply. "
    "state: facts kept across turns. post_processing: an edit the Editor makes to each reply."
)
_RESOLUTION_HELP = (
    "yes_no: threshold or roll (by its odds). choice: argmax (most likely), weighted (drawn by the odds) or gated (first "
    "option if most likely, else drawn). score: argmax, weighted or nearest."
)


def _flag(key: str, value: Switch) -> dict[str, bool]:
    return {} if value == "unchanged" else {key: value == "on"}


def stage_of(field_type: str) -> Stage:
    if field_type == DECISION_FIELD_TYPE:
        return "judge"
    if field_type == STATE_FIELD_TYPE:
        return "state"
    return "editor" if field_type in _EDITOR_FIELD_TYPES else "director"


def required_applies(field_type: str, state_mode: str | None, state_update: str | None) -> bool:
    """Whether Required means anything: a field the Director fills, or a single state value it sets before the Writer."""
    if field_type == STATE_FIELD_TYPE:
        return (state_mode or DEFAULT_STATE_MODE) == "value" and state_update == "before_writer"
    return field_type not in ("post_processing", DECISION_FIELD_TYPE)


def fragment_text(row: Mapping[str, Any], field: str, outcome: str = "") -> str | None:
    """An interactive fragment's text *field* as stored; ``None`` for an outcome the decision does not have."""
    part = OUTCOME_PARTS.get(field)
    if part is not None:
        return outcome_text(row, part, outcome)
    return str(row.get(INTERACTIVE_TEXT_COLUMNS[field]) or "")


def fragment_text_problem(row: Mapping[str, Any], field: str, outcome: str, text: str, rules: AppReads) -> str:
    """Why *text* cannot become *field* of interactive fragment *row*; empty when it can.

    A decision is checked whole, as its route checks it, for the problems this text would add.
    """
    if len(text) > TEXT_CAPS[field]:
        return f"That {field} is {len(text):,} characters; it may be at most {TEXT_CAPS[field]:,}."
    if field in REQUIRED_TEXT and not text.strip():
        return f"A fragment's {field} cannot be left empty."
    if row.get("field_type") != DECISION_FIELD_TYPE or field in ("label", "injection_label"):
        return ""
    part = OUTCOME_PARTS.get(field)
    changed = with_outcome_text(row, part, outcome, text) if part else {INTERACTIVE_TEXT_COLUMNS[field]: text}
    before = set(rules.definition_problems(row))
    added = [problem for problem in rules.definition_problems({**row, **changed}) if problem not in before]
    return f"That would leave the decision unable to run: {'; '.join(added)}." if added else ""


def check_field_applies(row: Mapping[str, Any], field: str, outcome: str) -> None:
    """Refuse an interactive text field the fragment's type does not have, or an outcome named for any other field."""
    field_type = str(row.get("field_type") or "")
    if field_type not in _FIELD_TYPES_OF.get(field, frozenset(FIELD_TYPE_NAMES)):
        if field == "description":
            raise ToolFailure("A decision fragment's description is unused; the Judge reads its question.")
        raise ToolFailure(f"{field} belongs to another type of fragment; this one is {FIELD_TYPE_NAMES.get(field_type)}.")
    if field in OUTCOME_PARTS:
        keys = outcome_keys_of(row)
        if outcome not in keys:
            raise ToolFailure(f"Name one outcome of this decision: {', '.join(keys) or 'it has none'}.")
    elif outcome:
        raise ToolFailure("outcome is only for outcome_meaning and outcome_guidance; leave it empty.")


def ordered_stage(fragments: Iterable[InteractiveFragmentRow], stage: Stage) -> list[InteractiveFragmentRow]:
    """One stage's fragments in the order they run and the fragment list shows them."""
    return sorted(
        (row for row in fragments if stage_of(row["field_type"]) == stage),
        key=lambda row: (row["sort_order"] or 0, row["id"]),
    )


def decision_settings(row: Mapping[str, Any]) -> dict[str, Any]:
    """How a decision resolves, as the tools name it; none of its texts."""
    return {
        "decision_type": DECISION_TYPE_NAMES.get(str(row.get("decision_type")), row.get("decision_type")),
        "resolution": row.get("decision_resolution"),
        **({"threshold": row["decision_threshold"]} if row.get("decision_threshold") is not None else {}),
        **({"min_confidence": row["decision_confidence_floor"]} if row.get("decision_confidence_floor") is not None else {}),
        "inject": row.get("decision_inject"),
    }


def decision_projection(row: Mapping[str, Any], cap: int, rules: AppReads, *, listing: bool = False) -> dict[str, Any]:
    """A decision's definition as the tools name it, each text clipped to *cap*; *listing* names its outcomes only."""
    keys = outcome_keys_of(row)
    data: dict[str, Any] = {**decision_settings(row), "question": clip(row.get("decision_instructions"), cap)}
    if listing:
        data["outcomes"] = list(keys)
    else:
        data["situation"] = clip(row.get("decision_state_template"), cap)
        data["outcomes"] = [
            {
                "outcome": key,
                "meaning": clip(outcome_text(row, "meaning", key), cap),
                "guidance": clip(outcome_text(row, "guidance", key), cap),
            }
            for key in keys
        ]
    problems = rules.definition_problems(row)
    if problems:
        data["problems"] = problems[:6]
    return data


def interactive_projection(
    row: InteractiveFragmentRow, position: int, cap: int, rules: AppReads, *, listing: bool = False
) -> dict[str, Any]:
    """An interactive fragment's settings, and its texts clipped to *cap*.

    *listing* is a row among many: no cooldown of 0, no injection label that is the label, a decision's question without
    its situation and outcome texts.
    """
    field_type = row["field_type"]
    data: dict[str, Any] = {
        "kind": "interactive",
        "id": row["id"],
        "label": clip(row["label"], _LABEL_CHARS),
        "field_type": FIELD_TYPE_NAMES.get(field_type, field_type),
        "stage": stage_of(field_type),
        "position": position,
        "enabled": bool(row["enabled"]),
    }
    if not listing or row["cooldown_turns"]:
        data["cooldown_turns"] = row["cooldown_turns"]
    if not listing or row["injection_label"] != row["label"]:
        data["injection_label"] = clip(row["injection_label"], _LABEL_CHARS)
    if required_applies(field_type, row["state_mode"], row["state_update"]):
        data["required"] = bool(row["required"])
    if field_type == STATE_FIELD_TYPE:
        data["state"] = {"mode": row["state_mode"], "update": row["state_update"], "inject": row["state_inject"]}
    if field_type == "post_processing":
        data["gate_question"] = clip(row["post_processing_gate"], cap)
        data["gate_replies"] = row["post_processing_gate_replies"]
    if field_type == DECISION_FIELD_TYPE:
        data["decision"] = decision_projection(row, cap, rules, listing=listing)
    else:
        data["description"] = clip(row["description"], cap)
    return data


def mood_projection(row: MoodFragmentRow, cap: int, *, listing: bool = False) -> dict[str, Any]:
    """A mood fragment's settings and texts clipped to *cap*; *listing* is a row among many, its description only."""
    data: dict[str, Any] = {
        "kind": "mood",
        "id": row["id"],
        "label": clip(row["label"], _LABEL_CHARS),
        "enabled": bool(row["enabled"]),
        **({} if listing and not row["cooldown_turns"] else {"cooldown_turns": row["cooldown_turns"]}),
        "description": clip(row["description"], cap),
    }
    if not listing:
        data["prompt_text"] = clip(row["prompt_text"], cap)
        data["negative_prompt"] = clip(row["negative_prompt"], cap)
    return data


async def effect_note(field_type: str, settings: Settings, host: AssistantHost) -> str:
    """What keeps a fragment of *field_type* (``mood`` for a mood) from taking effect now, or nothing."""
    switches = switch_summary(settings, host)
    if field_type == DECISION_FIELD_TYPE:
        if not await host.judge_configured(settings):
            return "No Judge endpoint is set (Endpoints, Judge), so decisions are skipped until one is. Tell the user."
        needs, switch = "", ""
    elif field_type in ("mood", "string", "array"):
        needs, switch = "The Director chooses moods and fills scene fields", "director"
    else:
        needs, switch = "The Agent runs state updates, feedback and post-processing", "agent"
    if not switch or switches[switch] is True:
        return ""
    return f"{needs}, and it is off now, so this has no effect yet. Tell the user; turn it on only if they ask."


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")[:_SLUG_CHARS].strip("_") or "fragment"


def new_fragment_id(label: str, taken: Iterable[str]) -> str:
    """An id for a fragment named *label*: its words in lowercase, numbered past any id already *taken*."""
    used = set(taken)
    base = fragment_id = _slug(label)
    number = 2
    while fragment_id in used:
        fragment_id, number = f"{base}_{number}", number + 1
    return fragment_id


def _text(cap: int, description: str, *, required: bool = False) -> Any:
    if required:
        return Field(min_length=1, max_length=cap, description=f"{description} At most {cap} characters.")
    return Field(default="", max_length=cap, description=f"{description} At most {cap} characters.")


def _cooldown(*, unchanged: bool = False) -> Any:
    if unchanged:
        return Field(default=-1, ge=-1, le=50, description="Turns it rests after it is used; -1 leaves it.")
    return Field(default=0, ge=0, le=50, description="Turns it rests after it is used.")


class _MoodCreate(ToolArguments):
    label: str = _text(_LABEL_CHARS, "Its name, such as Terse.", required=True)
    description: str = _text(_ARG_TEXT_CHARS, "When the Director should turn it on.", required=True)
    prompt_text: str = _text(_ARG_TEXT_CHARS, "The instruction the Writer gets while it is on.", required=True)
    negative_prompt: str = _text(_ARG_TEXT_CHARS, "Optional: the instruction the Writer gets when the Director turns it off.")
    cooldown_turns: int = _cooldown()
    enabled: bool = Field(default=True, description="false creates it switched off.")

    _blank = field_validator("label", "description", "prompt_text")(not_blank)


class _InteractiveCreate(ToolArguments):
    label: str = _text(_LABEL_CHARS, "Its name.", required=True)
    field_type: Literal["single", "list", "feedback", "state", "post_processing"]
    description: str = _text(
        _ARG_TEXT_CHARS,
        "What to write, note or record, or the edit to make; examples help.",
        required=True,
    )
    injection_label: str = _text(_LABEL_CHARS, "The heading it is sent under, such as Pacing:; empty uses the label.")
    required: bool = Field(
        default=False, description="A value every turn: single, list, feedback, or a state value updated before_writer."
    )
    state_mode: Literal["", "value", "entries"] = Field(default="", description="state: one value, or a list. Empty: value.")
    state_update: Literal["", "after_reply", "before_writer", "manual"] = Field(
        default="", description="state: when it is updated; manual is by the user only. Empty: after_reply."
    )
    state_inject: Literal["", "off", "director", "writer", "both"] = Field(
        default="", description="state: which passes get it. Empty: both."
    )
    gate_question: str = _text(4000, "post_processing: a yes/no question for the Judge; the edit runs only on yes.")
    gate_replies: int = Field(default=0, ge=0, le=10, description="With a gate: earlier replies the Judge sees.")
    cooldown_turns: int = _cooldown()
    enabled: bool = Field(default=True, description="false creates it switched off.")

    _blank = field_validator("label", "description")(not_blank)


class _DecisionCreate(ToolArguments):
    label: str = _text(_LABEL_CHARS, "Its name, such as Outcome.", required=True)
    decision_type: Literal["yes_no", "choice", "score"] = Field(
        description="yes_no: true or false. choice: named options. score: levels on a scale."
    )
    question: str = _text(_ARG_TEXT_CHARS, "What the Judge decides, such as Does the action succeed?", required=True)
    outcomes: list[_OptionName] = Field(
        default_factory=list,
        max_length=_OUTCOMES,
        description="choice only: the options' distinct names, which the Judge answers with.",
    )
    outcome_meanings: list[_Outcome] = Field(
        min_length=2,
        max_length=_OUTCOMES,
        description="What each outcome means: yes_no, when true then when false; choice, one per option; score, "
        f"{MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels, lowest first.",
    )
    outcome_guidance: list[_Outcome] = Field(
        default_factory=list,
        max_length=_OUTCOMES,
        description="What the story does on each, in order; empty copies the meanings.",
    )
    resolution: Literal["", "threshold", "roll", "argmax", "weighted", "gated", "nearest"] = Field(
        default="", description=f"{_RESOLUTION_HELP} Empty: threshold or argmax."
    )
    threshold_percent: int = Field(default=50, ge=0, le=100, description="threshold: the yes cutoff.")
    min_confidence_percent: int = Field(
        default=0, ge=0, le=100, description="choice, score: skip less confident answers; 0 never skips."
    )
    inject: Literal["director", "writer", "both"] = Field(default="both", description="Which passes get the guidance.")
    situation: str = _text(
        _ARG_TEXT_CHARS, "What the Judge reads, with macros like {{last_message}}. Empty: the last reply and request."
    )
    injection_label: str = _text(_LABEL_CHARS, "The heading its guidance goes under; empty uses the label.")
    cooldown_turns: int = _cooldown()
    enabled: bool = Field(default=True, description="false creates it switched off.")

    _blank = field_validator("label", "question", "outcomes", "outcome_meanings")(not_blank)


class _MoodSet(ToolArguments):
    fragment_id: str = fragment_field()
    enabled: Switch = Field(default="unchanged", description=f"Whether the Director may use it: {_SWITCH}")
    cooldown_turns: int = _cooldown(unchanged=True)


class _InteractiveSet(ToolArguments):
    fragment_id: str = fragment_field()
    enabled: Switch = Field(default="unchanged", description=f"Whether it runs: {_SWITCH}")
    required: Switch = Field(default="unchanged", description=f"Whether the Director must give a value every turn: {_SWITCH}")
    cooldown_turns: int = _cooldown(unchanged=True)
    field_type: Literal["unchanged", "single", "list", "feedback", "state", "post_processing"] = Field(
        default="unchanged", description="A new type, as create_interactive_fragment names them; a decision keeps its type."
    )
    state_mode: Literal["unchanged", "value", "entries"] = Field(default="unchanged", description="state only.")
    state_update: Literal["unchanged", "after_reply", "before_writer", "manual"] = Field(
        default="unchanged", description="state only."
    )
    state_inject: Literal["unchanged", "off", "director", "writer", "both"] = Field(
        default="unchanged", description="state only."
    )
    gate_replies: int = Field(
        default=-1, ge=-1, le=10, description="With a gate: earlier replies the Judge sees; -1 leaves it."
    )
    position: int = Field(default=0, ge=0, description="Its place in its stage, 1 runs first; 0 leaves it.")


class _DecisionSet(ToolArguments):
    fragment_id: str = fragment_field()
    resolution: Literal["unchanged", "threshold", "roll", "argmax", "weighted", "gated", "nearest"] = Field(
        default="unchanged", description=_RESOLUTION_HELP
    )
    threshold_percent: int = Field(default=-1, ge=-1, le=100, description="threshold only: the yes cutoff; -1 leaves it.")
    min_confidence_percent: int = Field(
        default=-1, ge=-1, le=100, description="choice, score: skip less confident answers; 0 never skips, -1 leaves it."
    )
    inject: Literal["unchanged", "director", "writer", "both"] = Field(
        default="unchanged", description="Which passes get the guidance."
    )


def _outcome_columns(args: _DecisionCreate) -> dict[str, Any]:
    """The criteria and outputs columns *args* describe, checked for the shape their decision type takes."""
    meanings = [text.strip() for text in args.outcome_meanings]
    guidance = list(args.outcome_guidance) or meanings
    if len(guidance) != len(meanings):
        raise ToolFailure(f"Give one outcome_guidance per meaning ({len(meanings)}), or none to copy the meanings.")
    if args.decision_type == "yes_no":
        if args.outcomes or len(meanings) != 2:
            raise ToolFailure("A yes_no decision takes two outcome_meanings, when true then when false, and no outcomes.")
        keys: list[str] = ["true", "false"]
    elif args.decision_type == "choice":
        keys = [name.strip() for name in args.outcomes]
        if len(keys) != len(meanings):
            raise ToolFailure(f"Name each option in outcomes, one per meaning: {len(meanings)} meanings, {len(keys)} names.")
        if len(set(keys)) != len(keys):
            raise ToolFailure("Each option needs its own name; the Judge answers with them.")
    else:
        if args.outcomes:
            raise ToolFailure("A score decision's levels are its outcome_meanings, lowest first; leave outcomes empty.")
        if len(meanings) > MAX_SCORE_LEVELS:
            raise ToolFailure(f"A score decision has {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels.")
        return {"decision_criteria": meanings, "decision_outputs": {str(i): text for i, text in enumerate(guidance)}}
    return {"decision_criteria": dict(zip(keys, meanings)), "decision_outputs": dict(zip(keys, guidance))}


def build_fragment_tools(host: AssistantHost) -> list[AssistantTool]:
    """The fragment creates and setters, in their stable order."""
    tools = ToolSet()

    async def noted(data: dict[str, Any], field_type: str, enabled: object) -> dict[str, Any]:
        """*data* with what keeps an enabled fragment from taking effect now, if anything does."""
        note = await effect_note(field_type, await get_settings(), host) if enabled else ""
        return {**data, "note": note} if note else data

    async def stage_place(row: InteractiveFragmentRow) -> int:
        stage = ordered_stage(await get_interactive_fragments(), stage_of(row["field_type"]))
        return next((index + 1 for index, item in enumerate(stage) if item["id"] == row["id"]), 0)

    async def create_interactive(payload: dict[str, Any], label: str) -> ToolOutput:
        rows = await get_interactive_fragments()
        payload["id"] = new_fragment_id(label, [*(row["id"] for row in rows), *RESERVED_FRAGMENT_IDS])
        # Last in its stage, as a new fragment the user adds to the end of the list.
        payload["sort_order"] = max((row["sort_order"] or 0 for row in rows), default=0) + 1
        try:
            fields = host.checked_fragment_write(host.validated("interactive_fragment_create", payload))
        except HostRefusal as exc:
            raise ToolFailure(f"That decision would not run: {exc}. Nothing was created.") from None
        try:
            row = await create_interactive_fragment(fields)
        except sqlite3.IntegrityError:
            raise ToolFailure("A fragment with that id was added meanwhile. Nothing was created; ask again.") from None
        if row is None:
            raise ToolFailure("The fragment was not created.")
        ids = {"fragment_id": row["id"]}
        placed = {"stage": stage_of(row["field_type"]), "position": await stage_place(row)}
        data = await noted({"created": True, **ids, **placed}, row["field_type"], row["enabled"])
        kind = FIELD_TYPE_NAMES.get(row["field_type"], row["field_type"])
        summary = f"Created {kind} fragment {quoted(row['label'])}."
        return ToolOutput(data, summary, ids, committed=({"kind": "interactive_fragment", **ids},))

    @tools.add(
        "write",
        _MoodCreate,
        "Create a mood fragment: an instruction the Director turns on for a turn when its description fits the scene, "
        "sending its prompt text to the Writer while it is on.",
        name="create_mood_fragment",
    )
    async def add_mood_fragment(args: _MoodCreate) -> ToolOutput:
        taken = [row["id"] for row in await get_mood_fragments()]
        fields = host.validated(
            "mood_fragment_create",
            {
                "id": new_fragment_id(args.label, taken),
                "label": args.label.strip(),
                "description": args.description.strip(),
                "prompt_text": args.prompt_text.strip(),
                "negative_prompt": args.negative_prompt.strip(),
                "cooldown_turns": args.cooldown_turns,
                "enabled": args.enabled,
            },
        )
        try:
            row = await create_mood_fragment(fields)
        except sqlite3.IntegrityError:
            raise ToolFailure("A mood fragment with that id was added meanwhile. Nothing was created; ask again.") from None
        ids = {"fragment_id": row["id"]}
        data = await noted({"created": True, **ids}, "mood", row["enabled"])
        summary = f"Created mood fragment {quoted(row['label'])}."
        return ToolOutput(data, summary, ids, committed=({"kind": "mood_fragment", **ids},))

    @tools.add(
        "write",
        _InteractiveCreate,
        f"Create an interactive fragment the Agent handles each turn. {_TYPE_HELP} A decision: create_decision_fragment.",
        name="create_interactive_fragment",
    )
    async def add_interactive_fragment(args: _InteractiveCreate) -> ToolOutput:
        field_type = _STORED_TYPES[args.field_type]
        state = {"state_mode": args.state_mode, "state_update": args.state_update, "state_inject": args.state_inject}
        if field_type != STATE_FIELD_TYPE and any(state.values()):
            raise ToolFailure("state_mode, state_update and state_inject are for a state fragment only. Nothing was created.")
        if args.required and not required_applies(field_type, args.state_mode or None, args.state_update or None):
            raise ToolFailure(
                "required applies only to single, list and feedback fragments, and to a state value updated before_writer. "
                "Nothing was created."
            )
        gate = args.gate_question.strip()
        if (gate or args.gate_replies) and field_type != "post_processing":
            raise ToolFailure("gate_question and gate_replies are for a post_processing fragment only. Nothing was created.")
        if args.gate_replies and not gate:
            raise ToolFailure("gate_replies needs a gate_question. Nothing was created.")
        payload: dict[str, Any] = {
            "label": args.label.strip(),
            "description": args.description.strip(),
            "field_type": field_type,
            "required": args.required,
            "enabled": args.enabled,
            "injection_label": args.injection_label.strip() or args.label.strip(),
            "cooldown_turns": args.cooldown_turns,
            "post_processing_gate": gate,
            "post_processing_gate_replies": args.gate_replies,
            **{key: value or None for key, value in state.items()},
        }
        return await create_interactive(payload, args.label)

    @tools.add(
        "write",
        _DecisionCreate,
        "Create a decision fragment: a question the Judge answers before the Director runs, whose outcome's guidance the "
        "passes then follow.",
    )
    async def create_decision_fragment(args: _DecisionCreate) -> ToolOutput:
        decision_type = _STORED_DECISION_TYPES[args.decision_type]
        resolution = args.resolution or DECISION_RESOLUTIONS_BY_TYPE[decision_type][0]
        if resolution not in DECISION_RESOLUTIONS_BY_TYPE[decision_type]:
            allowed = ", ".join(DECISION_RESOLUTIONS_BY_TYPE[decision_type])
            raise ToolFailure(f"A {args.decision_type} decision resolves by {allowed}. Nothing was created.")
        if args.min_confidence_percent and decision_type == "noul":
            raise ToolFailure("min_confidence_percent is for choice and score decisions. Nothing was created.")
        payload: dict[str, Any] = {
            "label": args.label.strip(),
            "description": "",
            "field_type": DECISION_FIELD_TYPE,
            "enabled": args.enabled,
            "injection_label": args.injection_label.strip() or args.label.strip(),
            "cooldown_turns": args.cooldown_turns,
            "decision_type": decision_type,
            "decision_placement": "before_director",
            "decision_inject": args.inject,
            "decision_state_template": args.situation.strip() or DEFAULT_STATE_TEMPLATE,
            "decision_instructions": args.question.strip(),
            **_outcome_columns(args),
            "decision_resolution": resolution,
            "decision_threshold": args.threshold_percent / 100 if resolution == "threshold" else None,
            "decision_confidence_floor": args.min_confidence_percent / 100 if args.min_confidence_percent else None,
        }
        return await create_interactive(payload, args.label)

    @tools.add(
        "write",
        _MoodSet,
        "Turn a mood fragment on or off, or set its cooldown.",
        idempotent=True,
    )
    async def set_mood_fragment(args: _MoodSet) -> ToolOutput:
        row = await mood_fragment_of(args.fragment_id)
        wanted: dict[str, Any] = {
            **_flag("enabled", args.enabled),
            **({"cooldown_turns": args.cooldown_turns} if args.cooldown_turns >= 0 else {}),
        }
        if not wanted:
            raise ToolFailure("Name enabled or cooldown_turns; nothing was asked to change.")
        changes = {key: value for key, value in wanted.items() if row[key] != value}
        if (
            changes
            and await update_mood_fragment(row["id"], host.validated("mood_fragment_update", changes, exclude_none=True))
            is None
        ):
            raise ToolFailure(f"No mood fragment has the id {args.fragment_id}.")
        after = await mood_fragment_of(row["id"])
        data = {
            "fragment": clip(after["label"], _LABEL_CHARS),
            "enabled": bool(after["enabled"]),
            "cooldown_turns": after["cooldown_turns"],
            "changed": sorted(changes),
        }
        label, ids = f"mood fragment {quoted(row['label'])}", {"fragment_id": row["id"]}
        if not changes:
            return ToolOutput(data, f"The {label} already had that; nothing changed.", ids)
        data = await noted(data, "mood", after["enabled"])
        return ToolOutput(data, f"Set the {label}: {_said(changes)}.", ids, committed=({"kind": "mood_fragment", **ids},))

    @tools.add(
        "write",
        _InteractiveSet,
        "Set an interactive fragment's switches, cooldown, type, state settings, gate history or place in its stage. "
        "Its text changes through edit_interactive_fragment.",
        idempotent=True,
    )
    async def set_interactive_fragment(args: _InteractiveSet) -> ToolOutput:
        row = await interactive_fragment_of(args.fragment_id)
        wanted: dict[str, Any] = {
            **_flag("enabled", args.enabled),
            **({"cooldown_turns": args.cooldown_turns} if args.cooldown_turns >= 0 else {}),
        }
        if args.field_type != "unchanged":
            if row["field_type"] == DECISION_FIELD_TYPE:
                raise ToolFailure(
                    "A decision fragment keeps its type: another type would drop its question and outcomes. Nothing was "
                    "changed. create_interactive_fragment adds a new one, if the user wants that."
                )
            wanted["field_type"] = _STORED_TYPES[args.field_type]
        field_type = wanted.get("field_type", row["field_type"])
        state = {
            column: value
            for column, value in (
                ("state_mode", args.state_mode),
                ("state_update", args.state_update),
                ("state_inject", args.state_inject),
            )
            if value != "unchanged"
        }
        if state and field_type != STATE_FIELD_TYPE:
            raise ToolFailure("state_mode, state_update and state_inject are for a state fragment only. Nothing was changed.")
        wanted.update(state)
        if args.gate_replies >= 0:
            if field_type != "post_processing" or not (row["post_processing_gate"] or "").strip():
                raise ToolFailure(
                    "gate_replies is for a post_processing fragment with a gate question; edit_interactive_fragment adds "
                    "one. Nothing was changed."
                )
            wanted["post_processing_gate_replies"] = args.gate_replies
        mode = state.get("state_mode", row["state_mode"] if row["field_type"] == STATE_FIELD_TYPE else None)
        update = state.get("state_update", row["state_update"] if row["field_type"] == STATE_FIELD_TYPE else None)
        applies = required_applies(field_type, mode, update)
        if args.required == "on" and not applies:
            raise ToolFailure(
                "required applies only to single, list and feedback fragments, and to a state value updated before_writer. "
                "Nothing was changed."
            )
        wanted.update(_flag("required", args.required))
        if not applies and row["required"]:
            # As the editor does: Required that no longer means anything is cleared, not left to surprise a later change.
            wanted["required"] = False
        if not wanted and not args.position:
            raise ToolFailure("Name something to change; nothing was asked to.")
        changes = {key: value for key, value in wanted.items() if row.get(key) != value}
        if changes:
            fields = host.validated("interactive_fragment_update", changes, exclude_unset=True)
            try:
                fields = host.checked_fragment_write(fields, dict(row))
            except HostRefusal as exc:
                raise ToolFailure(f"That would leave the decision unable to run: {exc}. Nothing was changed.") from None
            if await update_interactive_fragment(row["id"], fields) is None:
                raise ToolFailure(f"No interactive fragment has the id {args.fragment_id}.")
        moved = await _move(row["id"], stage_of(field_type), args.position) if args.position else False
        after = await interactive_fragment_of(row["id"])
        settings = interactive_projection(after, await stage_place(after), 0, host, listing=True)
        data: dict[str, Any] = {
            # Its settings as they now stand; a setter changes no text.
            "fragment": {
                key: value
                for key, value in settings.items()
                if key not in ("kind", "id", "description", "decision", "gate_question")
            },
            "changed": sorted([*changes, *(["position"] if moved else [])]),
        }
        if "field_type" in changes and row["field_type"] == STATE_FIELD_TYPE:
            data["note_state"] = "Chats keep the facts it saved, read-only in the Inspector's State tab, until removed there."
        if "required" in changes and args.required == "unchanged":
            data["note_required"] = "Required was cleared: it means nothing for this type or these state settings."
        label, ids = f"interactive fragment {quoted(row['label'])}", {"fragment_id": row["id"]}
        if not changes and not moved:
            return ToolOutput(data, f"The {label} already had that; nothing changed.", ids)
        data = await noted(data, after["field_type"], after["enabled"])
        said = _said({**changes, **({"position": data["fragment"]["position"]} if moved else {})})
        return ToolOutput(data, f"Set the {label}: {said}.", ids, committed=({"kind": "interactive_fragment", **ids},))

    @tools.add(
        "write",
        _DecisionSet,
        "Set how a decision fragment resolves: its resolution, threshold, minimum confidence, and which passes get its "
        "guidance. Its question and outcomes change through edit_interactive_fragment.",
        idempotent=True,
    )
    async def set_decision_fragment(args: _DecisionSet) -> ToolOutput:
        row = await interactive_fragment_of(args.fragment_id)
        if row["field_type"] != DECISION_FIELD_TYPE:
            raise ToolFailure("That is not a decision fragment; set_interactive_fragment sets its settings.")
        resolution = row["decision_resolution"] if args.resolution == "unchanged" else args.resolution
        wanted: dict[str, Any] = {}
        if args.resolution != "unchanged":
            wanted["decision_resolution"] = resolution
        if args.inject != "unchanged":
            wanted["decision_inject"] = args.inject
        if args.threshold_percent >= 0:
            if resolution != "threshold":
                raise ToolFailure("threshold_percent is only for the threshold resolution. Nothing was changed.")
            wanted["decision_threshold"] = args.threshold_percent / 100
        elif resolution == "threshold" and row["decision_threshold"] is None:
            wanted["decision_threshold"] = 0.5
        if resolution != "threshold" and row["decision_threshold"] is not None:
            wanted["decision_threshold"] = None
        if args.min_confidence_percent >= 0:
            if row["decision_type"] == "noul":
                raise ToolFailure("min_confidence_percent is for choice and score decisions. Nothing was changed.")
            wanted["decision_confidence_floor"] = args.min_confidence_percent / 100 or None
        if not wanted:
            raise ToolFailure("Name resolution, threshold_percent, min_confidence_percent or inject; nothing was asked to.")
        changes = {key: value for key, value in wanted.items() if row.get(key) != value}
        label, ids = f"decision fragment {quoted(row['label'])}", {"fragment_id": row["id"]}
        if changes:
            try:
                fields = host.checked_fragment_write(dict(changes), dict(row))
            except HostRefusal as exc:
                raise ToolFailure(f"That would leave the decision unable to run: {exc}. Nothing was changed.") from None
            if await update_interactive_fragment(row["id"], fields) is None:
                raise ToolFailure(f"No interactive fragment has the id {args.fragment_id}.")
        after = await interactive_fragment_of(row["id"])
        data = {
            "fragment": clip(after["label"], _LABEL_CHARS),
            "decision": decision_settings(after),
            "changed": sorted(changes),
        }
        if not changes:
            return ToolOutput(data, f"The {label} already had that; nothing changed.", ids)
        return ToolOutput(
            data, f"Set the {label}: {_said(changes)}.", ids, committed=({"kind": "interactive_fragment", **ids},)
        )

    return tools


def _said(changes: Mapping[str, Any]) -> str:
    """The changes a setter made, as a summary names them."""
    names = {"decision_resolution": "resolution", "decision_inject": "inject", "decision_threshold": "threshold"}
    names["decision_confidence_floor"] = "min confidence"
    names["post_processing_gate_replies"] = "gate replies"

    def value(key: str, item: Any) -> str:
        if isinstance(item, bool):
            return ("on" if item else "off") if key == "enabled" else str(item).lower()
        if key == "field_type":
            return str(FIELD_TYPE_NAMES.get(item, item))
        return "none" if item is None else str(item)

    return "; ".join(f"{names.get(key, key.replace('_', ' '))} {value(key, item)}" for key, item in changes.items())


async def _move(fragment_id: str, stage: Stage, position: int) -> bool:
    """Put *fragment_id* at *position* in *stage*, keeping the stage's own priority slots; whether it moved."""
    ordered = ordered_stage(await get_interactive_fragments(), stage)
    ids = [row["id"] for row in ordered]
    if position > len(ids):
        raise ToolFailure(f"Its stage holds {len(ids)} fragments, so position is 1 to {len(ids)}. Nothing was moved.")
    if ids.index(fragment_id) == position - 1:
        return False
    ids.remove(fragment_id)
    ids.insert(position - 1, fragment_id)
    slots = sorted(row["sort_order"] or 0 for row in ordered)
    # Fragments that share a slot run in an order no one chose; distinct slots keep the order asked for.
    for index in range(1, len(slots)):
        slots[index] = max(slots[index], slots[index - 1] + 1)
    if not await reorder_interactive_fragments(list(zip(ids, slots))):
        raise ToolFailure("A fragment in that stage was removed meanwhile. Nothing was moved; read the list again.")
    return True


def fragment_rows(
    moods: Sequence[MoodFragmentRow], interactive: Sequence[InteractiveFragmentRow], kind: str, cap: int, rules: AppReads
) -> list[dict[str, Any]]:
    """Every fragment as a listing shows it: moods by label, then interactive fragments stage by stage in their order."""
    rows: list[dict[str, Any]] = []
    if kind in ("all", "mood"):
        rows.extend(mood_projection(row, cap, listing=True) for row in moods)
    if kind in ("all", "interactive"):
        for stage in STAGES:
            for index, row in enumerate(ordered_stage(interactive, stage)):
                rows.append(interactive_projection(row, index + 1, cap, rules, listing=True))
    return rows
