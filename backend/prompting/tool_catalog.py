"""Ordered lookup and registration for model-facing tool contracts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Set
from copy import deepcopy
from dataclasses import dataclass

from .tool_schemas import (
    EDITOR_APPLY_PATCH_TOOL,
    EDITOR_REWRITE_TOOL,
    GIVE_FEEDBACK_CHOICE,
    PROPOSE_WORLD_CHANGES_CHOICE,
    PROPOSE_WORLD_CHANGES_TOOL,
    RECORD_DIRECTION_NOTE_CHOICE,
    SELECT_LOREBOOK_CHOICE,
    SELECT_LOREBOOK_TOOL,
    build_direct_scene_tool,
    build_direction_note_tool,
    build_feedback_tool,
)

BUILTIN_TOOL_ORDER = (
    "direct_scene",
    "editor_apply_patch",
    "editor_rewrite",
    "give_feedback",
    "record_direction_note",
    "select_lorebook",
    "propose_world_changes",
)
BUILTIN_TOOL_NAMES = frozenset(BUILTIN_TOOL_ORDER)

_tools: dict[str, dict] = {
    "direct_scene": {
        "choice": {"type": "function", "function": {"name": "direct_scene"}},
        "schema": build_direct_scene_tool([]),
    },
    "editor_apply_patch": {
        "choice": {"type": "function", "function": {"name": "editor_apply_patch"}},
        "schema": deepcopy(EDITOR_APPLY_PATCH_TOOL),
    },
    "editor_rewrite": {
        "choice": {"type": "function", "function": {"name": "editor_rewrite"}},
        "schema": deepcopy(EDITOR_REWRITE_TOOL),
    },
    "give_feedback": {
        "choice": deepcopy(GIVE_FEEDBACK_CHOICE),
        "schema": build_feedback_tool([]),
    },
    "record_direction_note": {
        "choice": deepcopy(RECORD_DIRECTION_NOTE_CHOICE),
        "schema": build_direction_note_tool([]),
    },
    "select_lorebook": {
        "choice": deepcopy(SELECT_LOREBOOK_CHOICE),
        "schema": deepcopy(SELECT_LOREBOOK_TOOL),
    },
    "propose_world_changes": {
        "choice": deepcopy(PROPOSE_WORLD_CHANGES_CHOICE),
        "schema": deepcopy(PROPOSE_WORLD_CHANGES_TOOL),
    },
}
assert tuple(_tools) == BUILTIN_TOOL_ORDER

_standalone_tools: set[str] = set()


class _LiveSetView(Set[str]):
    """Read-only set interface over mutable catalog-owned membership."""

    def __contains__(self, value: object) -> bool:
        return value in _standalone_tools

    def __iter__(self) -> Iterator[str]:
        return iter(_standalone_tools)

    def __len__(self) -> int:
        return len(_standalone_tools)


class _LiveToolsView(Mapping[str, dict]):
    """Read-only live catalog view that does not expose mutable internals."""

    def __getitem__(self, name: str) -> dict:
        return deepcopy(_tools[name])

    def __iter__(self) -> Iterator[str]:
        return iter(_tools)

    def __len__(self) -> int:
        return len(_tools)


TOOLS: Mapping[str, dict] = _LiveToolsView()
STANDALONE_TOOLS: Set[str] = _LiveSetView()


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Opaque state snapshot used by isolated registration fixtures."""

    tools: tuple[tuple[str, dict], ...]
    standalone_tools: frozenset[str]


def get_tool(name: str) -> dict | None:
    """Return a registered tool specification, if present."""
    tool = _tools.get(name)
    return deepcopy(tool) if tool is not None else None


def require_tool(name: str) -> dict:
    """Return a registered tool specification or raise ``KeyError``."""
    return deepcopy(_tools[name])


def has_tool(name: str) -> bool:
    return name in _tools


def is_standalone_tool(name: str) -> bool:
    return name in _standalone_tools


def register_tool(name: str, schema: dict, choice: dict, *, standalone: bool = False) -> None:
    """Register or replace a tool while preserving an existing position."""
    _tools[name] = {"schema": deepcopy(schema), "choice": deepcopy(choice)}
    if standalone:
        _standalone_tools.add(name)
    else:
        _standalone_tools.discard(name)


def remove_tool(name: str) -> None:
    """Remove a workflow tool from the catalog."""
    if name in BUILTIN_TOOL_NAMES:
        raise ValueError(f"cannot remove built-in tool {name!r}")
    _tools.pop(name, None)
    _standalone_tools.discard(name)


def snapshot_catalog() -> CatalogSnapshot:
    return CatalogSnapshot(
        tuple((name, deepcopy(tool)) for name, tool in _tools.items()),
        frozenset(_standalone_tools),
    )


def restore_catalog(snapshot: CatalogSnapshot) -> None:
    """Restore a snapshot without exposing mutable catalog internals."""
    _tools.clear()
    _tools.update((name, deepcopy(tool)) for name, tool in snapshot.tools)
    _standalone_tools.clear()
    _standalone_tools.update(snapshot.standalone_tools)


def enabled_schemas(
    enabled_tools: Mapping[str, bool] | None,
    overrides: Mapping[str, dict] | None = None,
) -> list[dict]:
    """Return enabled, non-standalone schemas in catalog order."""
    overrides = overrides or {}
    eligible = [name for name in _tools if name not in _standalone_tools]
    if enabled_tools is not None:
        eligible = [name for name in eligible if enabled_tools.get(name, False)]
    return [deepcopy(schema) for name in eligible if (schema := overrides.get(name, _tools[name]["schema"])) is not None]
