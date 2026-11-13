"""The assistant's host: the API, pipeline, workflow and peer-feature calls its tools make, behind the feature's protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from ...core.settings import Settings
from ...database import delete_character_card, update_settings
from ...database.models import ConversationRow
from ...features import lorebook
from ...features.assistant.tools import (
    AssistantHost,
    ChatUnavailable,
    HostRefusal,
    MediaOutcome,
    MediaRun,
    NamedWorkflow,
    SchemaName,
)
from ...features.fragment_state import StateWriteError, state_panel
from ...pipeline import estimate_context_size, resolve_judge_config
from ...pipeline.passes.judge import definition_problems
from ...workflows import gpu_swap_host, list_workflows, set_workflow_toggle
from .. import deps
from ..conversation_copy import checkpoint_conversation, checkpoint_title
from ..conversation_start import DEFAULT_TITLE, start_solo_chat
from ..deletions import delete_chat, delete_message_subtree
from ..fragments import DecisionProblems, checked_interactive_write
from ..schemas import (
    CharacterCardCreate,
    ConversationUpdate,
    InteractiveFragmentCreate,
    InteractiveFragmentUpdate,
    LorebookEntryCreate,
    LorebookEntryUpdate,
    MoodFragmentCreate,
    MoodFragmentUpdate,
    UserPersonaCreate,
    WorldCreate,
    WorldUpdate,
)
from . import media

_SCHEMAS: dict[SchemaName, type[BaseModel]] = {
    "character_create": CharacterCardCreate,
    "conversation_update": ConversationUpdate,
    "interactive_fragment_create": InteractiveFragmentCreate,
    "interactive_fragment_update": InteractiveFragmentUpdate,
    "lorebook_entry_create": LorebookEntryCreate,
    "lorebook_entry_update": LorebookEntryUpdate,
    "mood_fragment_create": MoodFragmentCreate,
    "mood_fragment_update": MoodFragmentUpdate,
    "persona_create": UserPersonaCreate,
    "world_create": WorldCreate,
    "world_update": WorldUpdate,
}


async def _refusing(delete: Awaitable[bool]) -> bool:
    """*delete* awaited, with a fence's refusal as the feature's."""
    try:
        return await delete
    except HTTPException as exc:
        raise HostRefusal(str(exc.detail)) from None


class ApiHost:
    """The host every assistant request and Apply share; it holds no state of its own."""

    def chat_busy(self, cid: str) -> bool:
        return deps.conversation_busy(cid)

    def chat_deleting(self, cid: str) -> bool:
        return deps.resource_deleting(cid)

    @asynccontextmanager
    async def admitted(self, cid: str) -> AsyncIterator[None]:
        try:
            async with deps.idle_chat_admission(cid):
                yield
        except deps.ChatUnavailable as exc:
            raise ChatUnavailable("deleting" if exc.reason == "deleting" else "busy") from None

    def validated(
        self, schema: SchemaName, fields: Mapping[str, Any], *, exclude_unset: bool = False, exclude_none: bool = False
    ) -> dict[str, Any]:
        model = _SCHEMAS[schema]
        unknown = fields.keys() - model.model_fields.keys()
        if unknown:
            raise TypeError(f"{model.__name__} has no field {sorted(unknown)}")
        return model(**fields).model_dump(exclude_unset=exclude_unset, exclude_none=exclude_none)

    def literal_phrases(self, variants: Sequence[str]) -> list[str]:
        try:
            phrases, _ = deps.validate_phrase_group("literal", list(variants), "")
        except HTTPException as exc:
            raise HostRefusal(str(exc.detail)) from None
        return phrases

    def checked_fragment_write(self, payload: dict[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            return checked_interactive_write(payload, existing)
        except DecisionProblems as exc:
            raise HostRefusal(str(exc)) from None

    async def start_chat(self, title: str, card: Mapping[str, Any]) -> ConversationRow:
        return await start_solo_chat(title or DEFAULT_TITLE, card)

    async def checkpoint_chat(self, conv: ConversationRow, title: str) -> ConversationRow | None:
        return await checkpoint_conversation(conv["id"], checkpoint_title(conv, title))

    async def delete_character(self, character_id: str, with_chats: bool) -> bool:
        return await _refusing(delete_character_card(character_id, with_chats, idle_guard=deps.idle_chats_guard))

    async def delete_chat(self, cid: str) -> bool:
        return await _refusing(delete_chat(cid))

    async def delete_message(self, cid: str, message_id: int) -> bool:
        return await _refusing(delete_message_subtree(cid, message_id))

    async def delete_lore_entry(self, world: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
        return await lorebook.delete_entry(world, entry)

    async def context_size(self, conv: ConversationRow) -> Mapping[str, Any]:
        return await estimate_context_size(conv)

    async def chat_state(self, cid: str) -> Mapping[str, Any]:
        try:
            return await state_panel(cid)
        except StateWriteError as exc:
            raise HostRefusal(str(exc)) from None

    async def judge_configured(self, settings: Settings) -> bool:
        return (await resolve_judge_config(settings)).configured

    def definition_problems(self, row: Mapping[str, Any]) -> list[str]:
        return definition_problems(row)

    def workflows(self) -> Sequence[NamedWorkflow]:
        return list_workflows()

    async def set_workflow_enabled(self, workflow_id: str, on: bool) -> None:
        await set_workflow_toggle(workflow_id, on)

    async def set_workflows_enabled(self, on: bool) -> None:
        # The settings route's own writer: one column, so a concurrent change to any other key is untouched.
        await update_settings({"workflows_globally_enabled": int(on)})
        # The GPU swap plan is off while workflows are, so a render right after switching them on must not read a cached "off".
        gpu_swap_host.invalidate()

    def media_available(self, workflow_id: str) -> bool:
        return media.media_available(workflow_id)

    def message_deleting(self, message_id: int) -> bool:
        return deps.message_deleting(message_id)

    async def run_media(self, cid: str, workflow_id: str, noun: str, body: Mapping[str, Any], run: MediaRun) -> MediaOutcome:
        return await media.run_media(cid, workflow_id, noun, body, run)


HOST: AssistantHost = ApiHost()
