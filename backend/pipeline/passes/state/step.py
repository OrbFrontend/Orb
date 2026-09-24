"""Run the ``update_state`` model call and turn its result into validated changes."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....core import (
    ChatMessage,
    ContentPart,
    StateFragment,
    StateOp,
    StateRejection,
    StateView,
    extract_hyperparams,
    plan_state_ops,
)
from ....inference import CachedBase, LLMClient, parse_tool_calls, reasoning_cfg
from ....prompting.tool_schemas import UPDATE_STATE_CHOICE, build_state_tool
from .prompts import AliasedEntry, build_state_request, entry_aliases

logger = logging.getLogger(__name__)

_ALIAS = re.compile(r"^\[?\s*(e\d+)\s*\]?$", re.IGNORECASE)


def _blank(value: Any) -> bool:
    """``None``, blank text, or a list of nothing but blank text."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, list) and all(isinstance(item, str) and not item.strip() for item in value)


@dataclass(slots=True)
class StateStepResult:
    """The ``done`` payload of a state step: applied events and rejected operations."""

    events: list[dict] = field(default_factory=list)
    rejections: list[StateRejection] = field(default_factory=list)
    agent_raw: str = ""


def parse_state_call(
    tool_calls: Sequence[Mapping[str, Any]],
    fragments: Sequence[StateFragment],
    aliases: Sequence[AliasedEntry],
    *,
    known_ids: frozenset[str] = frozenset(),
) -> tuple[list[StateOp], list[StateRejection]]:
    """Map a parsed ``update_state`` call to public operations for *fragments*.

    Retirements come first, so they free room for adds in the same call. The
    result is read against the turn's captured contract: a string where a list
    field expects an array is malformed, not reinterpreted. ``known_ids`` are
    other fragments of the turn -- the shared schema offers them all, so a value
    for one outside this call is ignored rather than reported.
    """
    by_id = {fragment.id: fragment for fragment in fragments}
    by_alias = {alias.alias: alias for alias in aliases}
    retires: list[StateOp] = []
    writes: list[StateOp] = []
    rejections: list[StateRejection] = []
    for call in tool_calls:
        if call.get("name") != "update_state":
            continue
        args = call.get("arguments")
        if not isinstance(args, Mapping):
            rejections.append(StateRejection("", "update", "malformed", "The call's arguments were not an object."))
            continue
        raw_retire = args.get("retire")
        items = [raw_retire] if isinstance(raw_retire, str) else raw_retire
        if isinstance(items, list):
            for item in items:
                if _blank(item):
                    continue
                match = _ALIAS.match(item.strip()) if isinstance(item, str) else None
                alias = by_alias.get(match.group(1).lower()) if match else None
                if alias is None:
                    shown = item if isinstance(item, str) else json.dumps(item)
                    rejections.append(
                        StateRejection("", "retire", "unknown_entry", f"No listed entry {shown!r}.", entry=str(shown))
                    )
                    continue
                retires.append(StateOp("retire", alias.fragment_id, entry_id=alias.entry_id, alias=alias.alias))
        elif raw_retire is not None:
            rejections.append(StateRejection("", "retire", "malformed", "`retire` must be a list of entry ids."))
        for key, value in args.items():
            # Empty means keep whatever its shape: small models fill every field,
            # and "" for a list or [] for a value is a declined field, not a mistake.
            if key == "retire" or _blank(value):
                continue
            fragment = by_id.get(key)
            if fragment is None:
                if key not in known_ids:
                    rejections.append(StateRejection(key, "update", "unknown_fragment", f"No state field {key!r}."))
                continue
            if fragment.mode == "entries":
                if not isinstance(value, list):
                    rejections.append(StateRejection(key, "add", "malformed", f"{fragment.label} takes a list of new entries."))
                    continue
                for item in value:
                    if isinstance(item, str):
                        writes.append(StateOp("add", key, text=item))
                    else:
                        rejections.append(StateRejection(key, "add", "malformed", "An entry must be text."))
            elif isinstance(value, str):
                writes.append(StateOp("set", key, text=value))
            else:
                rejections.append(StateRejection(key, "set", "malformed", f"{fragment.label} takes one text value."))
    return [*retires, *writes], rejections


async def state_step(
    client: LLMClient,
    base: CachedBase,
    *,
    settings: Mapping[str, Any],
    fragments: Sequence[StateFragment],
    view: StateView,
    placement: str,
    known_ids: frozenset[str] = frozenset(),
    scene_direction: str = "",
    decision_guidance: str = "",
    user_message: str = "",
    reply_text: str | None = None,
    writer_user_msg: str | list[ContentPart] | None = None,
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Yield reasoning chunks during the call(s), then a single done dict.

    One forced ``update_state`` call for every fragment, or one per fragment when
    the per-fragment Director toggle is on. The split is request-text-only: the
    wire schema stays the shared blob, so the extra calls reuse the cached
    prefix. Each call's changes are validated and applied to *view* before the
    next, and a failed call changes nothing.

    Yields:
        ``{"type": "reasoning", "delta": str}``
        ``{"type": "done", "result": StateStepResult}``
    """
    result = StateStepResult()
    if not fragments:
        yield {"type": "done", "result": result}
        return

    per_fragment_on = bool(settings.get("director_individual_fragments", 0))
    groups = [[fragment] for fragment in fragments] if per_fragment_on else [list(fragments)]
    hyperparams = extract_hyperparams(settings, lane="agent", defaults={"temperature": 0.4})
    raws: list[str] = []

    for group in groups:
        if client.is_aborted:
            break
        aliases = entry_aliases(group, view)
        request = build_state_request(
            group,
            view,
            aliases,
            placement=placement,
            tool_schema=build_state_tool(group),
            reasoning_on=reasoning_on,
            scene_direction=scene_direction,
            decision_guidance=decision_guidance,
            user_message=user_message,
        )
        if placement == "after_reply":
            # Replay the Writer exchange so each call extends the warm Writer prefix.
            trailing: list[ChatMessage] = [
                {"role": "user", "content": writer_user_msg or ""},
                {"role": "assistant", "content": reply_text or ""},
                {"role": "user", "content": request},
            ]
        else:
            trailing = [{"role": "user", "content": request}]

        resp: dict = {}
        try:
            async for event in base.complete_into(
                client,
                resp,
                label="update_state",
                trailing=trailing,
                tool_choice=UPDATE_STATE_CHOICE,
                kv_tracker=kv_tracker,
                **hyperparams,
                **reasoning_cfg(reasoning_on, reasoning_prefill),
            ):
                yield event
        except Exception:
            # A failed call keeps the state as it was. It must not propagate: the
            # after-reply placement runs just before ``_result``, and an exception
            # there would skip persisting the finished reply.
            logger.exception("State update call failed; keeping this group's state")
            continue

        raw = json.dumps(resp, default=str)
        logger.info("State step output:\n%s", raw)
        raws.append(raw)
        ops, parse_rejections = parse_state_call(parse_tool_calls(resp), group, aliases, known_ids=known_ids)
        events, rejections = plan_state_ops(ops, {fragment.id: fragment for fragment in group}, view, source="agent")
        result.events.extend(events)
        result.rejections.extend([*parse_rejections, *rejections])

    result.agent_raw = "\n".join(raws)
    yield {"type": "done", "result": result}
