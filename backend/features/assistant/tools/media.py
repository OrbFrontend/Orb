"""The assistant's media tools: an image or speech for one saved reply, rendered by the host as one job of the run."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any

from pydantic import Field

from ....core.settings import Settings
from ....database import get_active_path, get_message_by_id, get_settings, message_workflow_attachment_ids
from ..contracts import AssistantTool, ToolArguments, ToolFailure, ToolOutput, ViewContext
from .host import AssistantHost, HostRefusal, MediaOutcome, MediaRun, WorkflowSwitches
from .targets import chat_field, message_label, quoted, reply_target


@dataclass(frozen=True)
class _Medium:
    tool: str
    workflow_id: str
    action: str
    noun: str
    # The noun with its article, as a sentence names one: "an image", "speech".
    some: str
    # Completes "only a reply can be …".
    verb: str
    starting: str
    description: str
    # Takes the user's own direction for what to make, beside the reply it is made of.
    directed: bool = False


# The longest direction an image request carries; the image workflow bounds it again.
_DIRECTION_CHARS = 1000

_MEDIA = (
    _Medium(
        "generate_image",
        "image_gen",
        "generate",
        "image",
        "an image",
        "illustrated",
        "Composing the image prompt…",
        "Make an image of one saved reply in a chat with the Image Generation workflow and save it on that message. With "
        "direction, the image follows what the user asked for (moment, framing, focus, pose, mood, lighting), the reply "
        "supplying the scene and who is in it; without, it is composed from the reply alone, as the reply's image button does.",
        directed=True,
    ),
    _Medium(
        "generate_speech",
        "tts",
        "create",
        "speech",
        "speech",
        "voiced",
        "Synthesizing speech…",
        "Make speech audio of one saved reply in a chat with the Text-to-Speech workflow, in the speaking character's voice, "
        "and save it on that message.",
    ),
)


class _MediaArgs(ToolArguments):
    conversation_id: str = chat_field()
    message_id: int = Field(ge=1, description="The saved reply to make it of, from read_messages.")


class _ImageArgs(_MediaArgs):
    direction: str = Field(
        default="",
        max_length=_DIRECTION_CHARS,
        description="What the user wants this image to show, in their words or a faithful paraphrase; empty: from the reply alone.",
    )


def build_media_tools(view: ViewContext, run: MediaRun | None, host: AssistantHost) -> list[AssistantTool]:
    """One tool per media workflow that is installed with an on-demand hook; none without a run to report to."""
    if run is None:
        return []
    return [
        AssistantTool(
            m.tool,
            m.description,
            _ImageArgs if m.directed else _MediaArgs,
            "write",
            partial(_make, view, run, host, m),
            reports_progress=True,
        )
        for m in _MEDIA
        if host.media_available(m.workflow_id)
    ]


def _switched_off(workflow_id: str, settings: Settings, host: WorkflowSwitches) -> list[str]:
    """Each switch that keeps *workflow_id* from running, named with its toggle; empty when it may run."""
    off = []
    if not settings.get("workflows_globally_enabled", 1):
        off.append("Secondary workflows (toggle `workflows`) are")
    stored = settings.get("workflow_enabled")
    if not (stored if isinstance(stored, dict) else {}).get(workflow_id, True):
        name = next((workflow.display_name for workflow in host.workflows() if workflow.id == workflow_id), workflow_id)
        off.append(f"The {name} workflow (toggle `workflow:{workflow_id}`) is")
    return off


async def _make(view: ViewContext, run: MediaRun, host: AssistantHost, medium: _Medium, args: _MediaArgs) -> ToolOutput:
    conv, message = await reply_target(args.conversation_id, args.message_id, view, host, medium.verb, idle=False)
    cid, mid = conv["id"], message["id"]
    if not (message["content"] or "").strip():
        raise ToolFailure(f"Message {mid} has no text to make {medium.noun} of.")
    # Read now: a toggle earlier in this request may have changed what the request's own snapshot says. The dispatch gates
    # too, but answers a bare 404; this names the switch.
    off = _switched_off(medium.workflow_id, await get_settings(), host)
    if off:
        raise ToolFailure(
            f"{' and '.join(off)} switched off, so nothing was made. Tell the user; you can turn it on with "
            "set_pipeline_toggle if they ask."
        )

    direction = args.direction.strip() if isinstance(args, _ImageArgs) else ""
    body: dict[str, Any] = {"action": medium.action, "message_id": mid, **({"direction": direction} if direction else {})}
    before = set(await message_workflow_attachment_ids(mid, medium.workflow_id))
    run.report(medium.starting)
    try:
        outcome = await host.run_media(cid, medium.workflow_id, medium.noun, body, run)
    except HostRefusal:
        raise ToolFailure("That chat or message is being deleted, so nothing was made.") from None
    return await _result((cid, conv["title"], mid), medium, before, outcome, host)


async def _result(
    where: tuple[str, str, int], medium: _Medium, before: set[int], outcome: MediaOutcome, host: AssistantHost
) -> ToolOutput:
    """The tool's answer from what its own job reported: an attachment it saved is success, and a started job alone is not.

    Only the ids the job reported count: another render of the same message, such as one the user started from its button,
    saves rows too.
    """
    cid, title, mid = where
    stopped, cut = outcome.stopped, outcome.cut
    # A delete settles the job before it deletes, so the message may still be there, about to go.
    if await get_message_by_id(mid) is None or host.message_deleting(mid) or host.chat_deleting(cid):
        raise ToolFailure(f"Message {mid} was deleted while its {medium.noun} was being made, so nothing was kept.")
    stored = set(await message_workflow_attachment_ids(mid, medium.workflow_id))
    ids = [attachment for attachment in outcome.ids if attachment in stored]
    if not ids:
        if (stopped or cut or not outcome.finished) and stored - before:
            # A job cut short may have saved a render whose event was never read, but the new row may as well be another's.
            ended = "was stopped" if stopped else "was cut short" if cut else "ended before finishing"
            raise ToolFailure(
                f"The {medium.noun} job {ended} without reporting a saved {medium.noun}, but {medium.some} may have been saved "
                f"on message {mid} as it ended. Tell the user to check the message."
            )
        if stopped:
            raise ToolFailure(f"Stopped before any {medium.noun} was saved; nothing was made.")
        if cut:
            raise ToolFailure(f"The {medium.noun} job was stopped before anything was saved, so nothing was made.")
        raise ToolFailure(f"No {medium.noun} was made: {outcome.error or 'the workflow ended without saving anything.'}")
    what = f"{len(ids)} images" if medium.noun == "image" and len(ids) > 1 else medium.some
    summary = f"Saved {what} on {message_label(await get_active_path(cid), mid)} in {quoted(title)}"
    if stopped or cut:
        status, summary = "stopped early; kept what was saved", f"{summary} before it was stopped."
    elif outcome.error:
        status = f"saved; a later step failed: {outcome.error}"
        summary = f"{summary}, but a later step failed: {outcome.error}"
    else:
        status, summary = "saved", f"{summary}."
    data: dict[str, Any] = {
        "saved": True,
        "kind": medium.noun,
        "conversation_id": cid,
        "message_id": mid,
        "attachment_ids": ids,
        "status": status,
        "note": "It is on the message in the chat; nothing else changed.",
    }
    ids_out = {"conversation_id": cid, "message_id": mid, "attachment_ids": ids}
    return ToolOutput(data, summary, ids_out, committed=({"kind": "media", **ids_out, "workflow_id": medium.workflow_id},))
