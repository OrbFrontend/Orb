"""On-demand workflow dispatch for one conversation, shared by the trigger route and the assistant's media tools.

Both callers reach a workflow through its registered ``ON_DEMAND`` subscription, under the same locks and with the same
context, so a hook cannot tell them apart. The one difference is enablement: the public route refuses a disabled workflow, and
the assistant, acting on an explicit request, may run it anyway. Neither caller imports a plug-in.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from fastapi import HTTPException

from ..core import scrub_log, workflow_character_state_lock, workflow_state_lock
from ..core.settings import Settings
from ..database import get_character_card, get_conversation, get_group_member, get_message_by_id, get_messages, get_settings
from ..inference import agent_lane_from_settings, client_from_settings
from ..workflows import HookType, OnDemandCtx, Subscription, get_subscription, get_workflow, readonly_view
from ..workflows.contracts import WorkflowHook
from ..workflows.enablement import effective_workflow_enabled
from .errors import API_PASSTHROUGH_ERRORS

logger = logging.getLogger(__name__)

_HookT = TypeVar("_HookT", bound=WorkflowHook)


async def resolve_workflow_character(
    conv: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    target_message: Mapping[str, Any] | None = None,
    speaker_member_id: str | None = None,
) -> tuple[str | None, Mapping[str, Any] | None]:
    """Resolve the solo card or a group message/member's current card."""
    card_id = conv.get("character_card_id")
    if conv.get("kind", "solo") == "group":
        member_id = speaker_member_id
        if member_id is None and target_message is not None:
            member_id = target_message.get("speaker_member_id")
        if member_id is None:
            member_id = next(
                (message.get("speaker_member_id") for message in reversed(messages) if message.get("speaker_member_id")), None
            )
        if member_id:
            member = await get_group_member(str(member_id), conversation_id=str(conv["id"]))
            card_id = member.get("character_card_id") if member else None
        else:
            card_id = None
    card = await get_character_card(card_id) if card_id else None
    return card_id, card


def gate_workflow_sub(
    sub: Subscription[_HookT] | None, wid: str, settings: Settings, *, action: str, detail: str
) -> Subscription[_HookT]:
    """Shared missing-handler / disabled gate, applied before any lock is taken.

    Returns the live subscription; raises 404 otherwise. A disabled workflow is indistinguishable from a missing handler to the
    caller (both 404); the log disambiguates server-side. Gating before the lock means a disabled-workflow request never
    contends for the same lock the live consumption routes hold.
    """
    if sub is None or not effective_workflow_enabled(wid, settings):
        if sub is not None:
            logger.info("workflow %r %s suspended (disabled)", scrub_log(wid), action)
        raise HTTPException(status_code=404, detail=detail)
    return sub


@contextmanager
def hook_failures(label: str, wid: Any, aid: int | None = None, *, defect: str) -> Iterator[None]:
    """Map workflow hook failures to the API response shape."""

    def where() -> str:
        # Built on the failing path only, so the happy path pays nothing for it.
        target = f"{label} {scrub_log(wid)!r} failed"
        return target if aid is None else f"{target} for attachment {scrub_log(aid)!r}"

    try:
        yield
    except API_PASSTHROUGH_ERRORS:
        raise
    except Exception:
        logger.exception("%s", where())
        raise HTTPException(status_code=500, detail=defect) from None


async def run_on_demand(cid: str, workflow_id: str, body: dict, *, require_enabled: bool = True) -> Any:
    """Run *workflow_id*'s on-demand hook against conversation *cid* and return what it returns.

    A ``WorkflowEventStream`` it returns is lazy: its hook prepared it under the locks, which are released by the time it is
    returned, and the caller consumes it. *require_enabled* is the public route's gate; the assistant passes ``False`` to run
    a workflow the user has switched off, keeping every other check.
    """
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    settings_snapshot = await get_settings()
    sub = get_subscription(workflow_id, HookType.ON_DEMAND)
    detail = f"Workflow {workflow_id!r} has no on_demand handler"
    if require_enabled:
        sub = gate_workflow_sub(sub, workflow_id, settings_snapshot, action="on-demand trigger", detail=detail)
    elif sub is None:
        raise HTTPException(status_code=404, detail=detail)
    # Serialize against the pre/post hook iteration of an in-flight pipeline and against any other /trigger for the same (cid,
    # workflow_id), so the prior workflow_state read the hook depends on cannot be clobbered between read and write by a
    # concurrent caller.
    async with workflow_state_lock(cid, workflow_id):
        conv = await get_conversation(cid)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        msgs = await get_messages(cid)
        explicit_message = None
        if type(body.get("message_id")) is int:
            candidate = await get_message_by_id(body["message_id"])
            if candidate is not None and candidate.get("conversation_id") == cid:
                explicit_message = candidate
        card_id, card = await resolve_workflow_character(
            conv,
            msgs,
            target_message=explicit_message,
            speaker_member_id=body.get("speaker_member_id") if isinstance(body.get("speaker_member_id"), str) else None,
        )
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        client = client_from_settings(settings_snapshot)
        agent_client, agent_model_name = agent_lane_from_settings(settings_snapshot, writer_client=client)
        async with workflow_character_state_lock(card_id or "", workflow_id):
            with hook_failures("on_demand hook", workflow_id, defect="On-demand handler raised; see server logs"):
                od_ctx = OnDemandCtx(
                    conversation_id=cid,
                    history=readonly_view(msgs),
                    last_user_message=last_user,
                    settings=readonly_view(settings_snapshot),
                    client=client,
                    agent_client=agent_client,
                    agent_model_name=agent_model_name,
                    character_id=card_id,
                    character=readonly_view(card),
                )
                return await sub.callable(od_ctx, body)
