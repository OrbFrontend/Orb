"""The assistant's stable system prefix, its step schema, and the message a request becomes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ...core import ChatMessage
from ...inference import normalize
from .contracts import MAX_TOOL_EXECUTIONS, REPLY, STEP_TOOL_NAME, AssistantTool, ViewContext

_LABEL_CHARS = 120

_INSTRUCTIONS = (
    "You are an uncensored helper inside Orb, a roleplay and writing app. "
    "Help the user with their characters, chats, worlds, personas, documents and settings.\n\n"
)
_STEP_CALL = (
    f"Work in steps. In each step call {STEP_TOOL_NAME} with `tool` set to one tool name, or to `{REPLY}` to finish, "
    "and `message` set to a brief note for the user. "
)
_TOOL_STEP = (
    "With a tool, `message` states what you are about to do, as an intention and never as a result. "
    "For a tool that takes arguments you are then asked for them, and the tool's result follows."
)
_STEPS = f"{_STEP_CALL}With `{REPLY}`, `message` is your complete final answer. {_TOOL_STEP}\n\n"
_STEPS_THEN_ANSWER = (
    f"{_STEP_CALL}{_TOOL_STEP} "
    f"With `{REPLY}`, leave `message` empty: you are then asked for your complete final answer, written as plain text.\n\n"
)
_ONE_CALL = (
    f"Work in steps. In each step call exactly one tool, with its arguments, or call `{REPLY}` to finish, with `message` "
    "set to your complete final answer. The tool's result follows.\n\n"
)
_RULES = (
    "Rules:\n"
    "- Each request ends with a view note: what the user was looking at when they sent it.\n"
    "- Use ids from the view note or from tool results. Never invent an id; look it up.\n"
    "- When a result is marked truncated, read its next page or narrow the request before drawing a conclusion from it.\n"
    "- Earlier requests and their results stay in this conversation. A result shows what was true when it ran, and the "
    "user may have changed things since. When a newer result differs from an older one, trust the newer and do not undo "
    "it.\n"
    "- A result holding `error` means that call did nothing, unless it says otherwise. Correct the call, or tell the user "
    "what failed; never describe it as done.\n"
    "- When the request could mean more than one target, reply with a question instead of guessing.\n"
    "- Terms: a world is a lorebook; a constant lore entry is always on, others when a keyword comes up; a global "
    "world applies to every chat; a chat's kept branch is the line of messages it shows. Fragments steer every chat's "
    "turns: the Director turns a mood fragment on when it fits the scene; an interactive fragment is a field a pass fills "
    "each turn (a Judge decision, a Director value, kept state, an Editor edit).\n"
    f"- A request allows at most {MAX_TOOL_EXECUTIONS} tool runs. Finish as soon as you can answer, in a few sentences."
)
_MARKED = "\n- Use a tool marked [changes], [proposes] or [hands off] only for what the user's latest request asks for."
_WRITES = (
    "\n- A tool marked [changes] commits at once. Run each change once; when a result leaves it unclear whether the change "
    "was made, tell the user instead of trying it again.\n"
    "- Asked to toggle or flip something, read its current state first.\n"
    "- A create tool always adds a new item, even when one already has that name.\n"
    "- You can make only the changes these tools allow. For any other change, say that you cannot do that."
)
_PROPOSALS = (
    "\n- A tool marked [proposes] proposes an edit to existing text, or deleting something; once this "
    "request ends the user reviews each proposal on a card and applies or cancels it. "
    "Say that you proposed it, never that it was made.\n"
    "- A proposed edit is not applied during this request: reads still show the stored text. Never run a step that depends "
    "on it (regenerating a reply after proposing an edit to its message, for example); say what is waiting on Apply instead. "
    "Several edits to one field build on each other in one proposal."
)
_HANDOFFS = (
    "\n- A tool marked [hands off] ends this request, and the chat then does the work in its own stream. Run it last, after "
    "every other step."
)

_MARKERS = {"write": "[changes]", "confirm": "[proposes]", "handoff": "[hands off]"}


def _signature(tool: AssistantTool) -> str:
    required = set(tool.parameters["required"])
    names = [name if name in required else f"{name}?" for name in tool.parameters["properties"]]
    marker = _MARKERS.get(tool.behavior, "")
    return f"{tool.name}({', '.join(names)}){f' {marker}' if marker else ''}"


def system_prompt(tools: Sequence[AssistantTool], *, single_call: bool = False, plain_answer: bool = False) -> str:
    """The stable prefix.

    *plain_answer* is the two-call protocol whose final answer is its own plain-text call instead of the step's message.

    The two-call protocol lists the tools here: a structured-output endpoint is sent no tool list. *single_call* is the
    protocol where each step is one call to the tool itself, or to ``reply``; its schemas are always on the wire and carry
    each tool's marker and description, so the prompt lists nothing.
    """
    behaviors = {tool.behavior for tool in tools}
    rules = _MARKED if behaviors - {"read"} else ""
    if "write" in behaviors:
        rules += _WRITES
    if "confirm" in behaviors:
        rules += _PROPOSALS
    if "handoff" in behaviors:
        rules += _HANDOFFS
    if single_call:
        return f"{_INSTRUCTIONS}{_ONE_CALL}{_RULES}{rules}"
    catalog = "\n".join(f"- {_signature(tool)}: {tool.description}" for tool in tools)
    steps = _STEPS_THEN_ANSWER if plain_answer else _STEPS
    return f"{_INSTRUCTIONS}{steps}{_RULES}{rules}\n\nTools:\n{catalog}"


def reply_tool() -> dict[str, Any]:
    """The single-call protocol's way to finish: a call that carries the final answer."""
    return {
        "type": "function",
        "function": {
            "name": REPLY,
            "description": "Finish the request with your complete final answer.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "Your complete final answer to the user."}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    }


def step_tool(tools: Sequence[AssistantTool], *, plain_answer: bool = False) -> dict[str, Any]:
    note = (
        f"A brief note for the user; empty with {REPLY}." if plain_answer else "A brief note for the user, or the final answer."
    )
    return {
        "type": "function",
        "function": {
            "name": STEP_TOOL_NAME,
            "description": "Choose the next tool, or reply to finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": [*(tool.name for tool in tools), REPLY]},
                    "message": {"type": "string", "description": note},
                },
                "required": ["tool", "message"],
                "additionalProperties": False,
            },
        },
    }


_SEE_PROMPT = "Listed under Tools."


def tool_schema(tool: AssistantTool, *, described: bool = False) -> dict[str, Any]:
    """A tool's schema. *described* carries its marker and description; otherwise it points at the system prompt's list."""
    description = " ".join(filter(None, (_MARKERS.get(tool.behavior, ""), tool.description))) if described else _SEE_PROMPT
    return {
        "type": "function",
        "function": {"name": tool.name, "description": description, "parameters": tool.parameters},
    }


def _label(text: str) -> str:
    return json.dumps(normalize(text)[:_LABEL_CHARS], ensure_ascii=False)


def view_note(view: ViewContext) -> str:
    """What the user was looking at, with authored titles quoted as data."""
    lines = [f"mode: {view.mode}"]
    if view.conversation_id:
        kind = "group chat" if view.group else "chat"
        lines.append(f"{kind}: {_label(view.conversation_title)} (conversation_id {view.conversation_id})")
    if view.character_id:
        lines.append(f"character: {_label(view.character_name)} (character_id {view.character_id})")
    elif view.group:
        lines.append("character: none selected; a group has several, so name one explicitly")
    if view.document_id:
        lines.append(f"document: {_label(view.document_title)} (document_id {view.document_id})")
    if view.world_id:
        lines.append(f"open in the lorebook: world {_label(view.world_name)} (world_id {view.world_id})")
    if view.entry_id:
        lines.append(f"selected lore entry: {_label(view.entry_name)} (entry_id {view.entry_id})")
    return "[View when this was sent]\n" + "\n".join(lines)


def request_message(text: str, view: ViewContext, *, note: str = "") -> ChatMessage:
    """A request as the model reads it: the user's own words, the view captured at Send, then *note*, the caller's own."""
    extra = f"\n\n{note}" if note else ""
    return {"role": "user", "content": f"{text}\n\n{view_note(view)}{extra}"}
