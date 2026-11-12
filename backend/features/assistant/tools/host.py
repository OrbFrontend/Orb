"""What the assistant's tools take from the app above this layer: small protocols the API side implements for a request."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ....core.settings import Settings
from ....database.models import ConversationRow
from ....inference import AbortToken


class HostRefusal(Exception):
    """The host's own writer or check refused, with its reason as the message: a busy resource, an invalid value."""


class ChatUnavailable(Exception):
    """A short chat mutation was not admitted: the chat is being deleted, or a stream or another mutation holds its lane."""

    def __init__(self, reason: Literal["deleting", "busy"]):
        self.reason = reason
        super().__init__(reason)


class ChatGuards(Protocol):
    """A chat's lane and deletion fence, as the chat routes keep them."""

    def chat_busy(self, cid: str) -> bool:
        """Whether a stream runs in chat *cid* or a mutation holds its lane right now; a snapshot, not a reservation."""
        ...

    def chat_deleting(self, cid: str) -> bool: ...

    def admitted(self, cid: str) -> AbstractAsyncContextManager[None]:
        """Hold chat *cid*'s lane through one short mutation, or raise :class:`ChatUnavailable`."""
        ...


# The route schemas a create or setter validates its fields through.
SchemaName = Literal[
    "character_create",
    "conversation_update",
    "interactive_fragment_create",
    "interactive_fragment_update",
    "lorebook_entry_create",
    "lorebook_entry_update",
    "mood_fragment_create",
    "mood_fragment_update",
    "persona_create",
    "world_create",
    "world_update",
]


class Writes(Protocol):
    """The routes' own schemas and the writers above the database that the creates and setters share with them."""

    def validated(
        self, schema: SchemaName, fields: Mapping[str, Any], *, exclude_unset: bool = False, exclude_none: bool = False
    ) -> dict[str, Any]:
        """*fields* through the route schema *schema*, dumped as the route dumps it."""
        ...

    def literal_phrases(self, variants: Sequence[str]) -> list[str]:
        """A literal phrase group's variants as the phrase bank stores them; :class:`HostRefusal` when none is usable."""
        ...

    def checked_fragment_write(self, payload: dict[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """An interactive fragment write kept to its type's settings; :class:`HostRefusal` naming a decision's problems."""
        ...

    async def start_chat(self, title: str, card: Mapping[str, Any]) -> ConversationRow:
        """A new solo chat with *card*, its greeting as the first reply; an empty *title* takes the card's name."""
        ...

    async def checkpoint_chat(self, conv: ConversationRow, title: str) -> ConversationRow | None:
        """A copy of *conv*'s kept branch as a new chat; an empty *title* names it after the chat."""
        ...


class Deletes(Protocol):
    """The deletes Apply runs through the app's own paths and fences; each answers whether the row existed.

    A delete its fence refuses raises :class:`HostRefusal` with the fence's reason.
    """

    async def delete_character(self, character_id: str, with_chats: bool) -> bool: ...

    async def delete_chat(self, cid: str) -> bool: ...

    async def delete_message(self, cid: str, message_id: int) -> bool: ...

    async def delete_document(self, document_id: str) -> bool: ...

    async def delete_lore_entry(self, world: Mapping[str, Any], entry: Mapping[str, Any]) -> bool: ...


class AppReads(Protocol):
    """What the layers above know: a chat's context size and scene state, the Judge's readiness and a decision's problems."""

    async def context_size(self, conv: ConversationRow) -> Mapping[str, Any]: ...

    async def chat_state(self, cid: str) -> Mapping[str, Any]:
        """The Inspector's State tab for chat *cid*; :class:`HostRefusal` when it cannot be read."""
        ...

    async def judge_configured(self, settings: Settings) -> bool: ...

    def definition_problems(self, row: Mapping[str, Any]) -> list[str]:
        """Why a decision definition would not run; empty when it would."""
        ...


class NamedWorkflow(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...


class WorkflowSwitches(Protocol):
    """The installed workflows, and the switches whose writes run the workflows' runtime callbacks."""

    def workflows(self) -> Sequence[NamedWorkflow]:
        """Every registered workflow, in registration order."""
        ...

    async def set_workflow_enabled(self, workflow_id: str, on: bool) -> None: ...

    async def set_workflows_enabled(self, on: bool) -> None:
        """The Secondary workflows master switch."""
        ...


@dataclass(frozen=True)
class MediaRun:
    """What the media tools take from the run they belong to.

    *job* is the workflow job id their jobs are filed under, unique to the run's operation; *abort* is the run's Stop; *report*
    sends a progress label to the run's stream while a job renders.
    """

    job: str
    abort: AbortToken
    report: Callable[[str], None]


@dataclass
class MediaOutcome:
    """What one job reported: the attachments it saved, its error, and whether its hook reached its own end.

    *stopped* is the run's Stop and *cut* a job cancelled by someone else (a deletion).
    """

    ids: list[int] = field(default_factory=list)
    error: str = ""
    finished: bool = False
    stopped: bool = False
    cut: bool = False

    def saved(self, attachment: object) -> None:
        if type(attachment) is int and attachment not in self.ids:
            self.ids.append(attachment)


class MediaJobs(Protocol):
    """The media workflows' on-demand hooks, run as workflow jobs filed under the run and the message they render."""

    def media_available(self, workflow_id: str) -> bool:
        """Whether *workflow_id* is installed with an on-demand hook."""
        ...

    def message_deleting(self, message_id: int) -> bool: ...

    async def run_media(self, cid: str, workflow_id: str, noun: str, body: Mapping[str, Any], run: MediaRun) -> MediaOutcome:
        """Render *body*'s message and wait for the job or the run's Stop; :class:`HostRefusal` when a delete fences it.

        *noun* names what is made in the errors the job reports.
        """
        ...


class AssistantHost(ChatGuards, Writes, Deletes, AppReads, WorkflowSwitches, MediaJobs, Protocol):
    """Everything the tools take from the app for one request."""
