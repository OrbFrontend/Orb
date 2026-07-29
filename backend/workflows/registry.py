"""Workflow registry, registration entry point, and per-workflow storage.

The registry has two bands with different lifetimes.

The **built-in base** is the process-local mapping of ids to ``Workflow``
records populated at import time by ``backend/workflows/__init__.py``.
Iteration order matches registration order: the dict preserves insertion
order, and reassignment to an existing key keeps the original position.
Hooks are attached separately through ``subscribe`` and live on the owner
record's ``subscriptions`` list.

The **community overlay** is a copy-on-write tuple of records compiled from
installed declarative packages, replaced wholesale by
``publish_community_overlay``. Community records never enter the base dict,
never reach ``register_tool``, and never claim a tool name -- ``Workflow``
validation rejects a community record that declares tools at all.

Readers do not consult either band directly. They capture a
:class:`RegistrySnapshot` (``current_snapshot()``) and resolve against it.
A turn captures exactly one snapshot before loading extension-sensitive
context and threads it through pre-hooks, fragment resolution, post-hooks,
and persistence, so an install that lands mid-turn cannot mix an old
pre-hook with a new post-hook. Snapshot resolution is a pure function of
(base, published), and a publish swaps one ``_Published`` reference carrying
both the overlay and its generation, so publishing never leaves a reader
looking at a half-updated registry or at records wearing the wrong
generation.

Storage wrappers (``get_workflow_state`` etc.) are thin awaiting wrappers
over ``backend.database`` so the toolkit has a single namespace for both
core reads and workflow-scoped reads. ``get_workflow_config`` is the one
exception that adds behavior: it falls back to the workflow's
``config_defaults`` when the DB slot is empty.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..core import (
    MAX_WRITER_TOOL_BLOB_BYTES,
    MAX_WRITER_TOOLS_PUBLISHED,
    WriterToolKey,
    wire_name,
)
from ..database import (
    get_workflow_character_state as _db_get_workflow_character_state,
)
from ..database import (
    get_workflow_config as _db_get_workflow_config,
)
from ..database import (
    get_workflow_message_state as _db_get_workflow_message_state,
)
from ..database import (
    get_workflow_state as _db_get_workflow_state,
)
from ..database import (
    set_workflow_character_state as _db_set_workflow_character_state,
)
from ..database import (
    set_workflow_config as _db_set_workflow_config,
)
from ..database import (
    set_workflow_message_state as _db_set_workflow_message_state,
)
from ..database import (
    set_workflow_state as _db_set_workflow_state,
)
from ..inference import (
    BUILTIN_TOOL_NAMES,
    STANDALONE_TOOLS,
    TOOLS,
    register_tool,
)
from .contracts import (
    AuditDetectorBinding,
    FrontendKind,
    HookStage,
    HookType,
    LoadStatus,
    ToolSpec,
    WorkflowSource,
    WriterToolBinding,
)
from .fragment_types import BUILTIN_FRAGMENT_TYPES, FragmentTypeDefinition

MAX_AUDIT_DETECTORS_PUBLISHED = 8
"""Contributed audit detectors across one registry snapshot.

Lives here rather than in ``core/`` because the registry is the only layer that
enforces it -- unlike ``MAX_WRITER_TOOLS_PUBLISHED``, whose core home was forced
by ``core/writer_tools.py`` owning the wire-name ABI that three layers must
agree on. A detector has no wire name and no ABI; it has a publish cap.

Bounds the per-turn cost end to end: each invocation already carries the
interpreter's own caps (steps, model calls, HTTP requests), so the turn budget
is this count times those, with no separate budget object to keep in sync.
"""


@dataclass
class Workflow:
    """Per-id workflow metadata.

    ``produces_artifacts=True`` is a contract: the workflow MUST also
    ``subscribe`` to both ``REGENERATE`` and ``REROLL_GEN`` -- ``subscribe``
    blocks artifact-hook bindings on workflows that disclaim the contract,
    and ``finalize_registry`` blocks the process from starting when one
    side is declared without the other.

    ``config_normalizer`` is the workflow's own strict normalization of its
    global config slot, applied by the config GET/PUT routes. ``config_schema``
    is UI metadata and enforces nothing, so without this the API would persist
    and echo back a shape the workflow's own hooks then silently repair or drop
    on read -- leaving a settings panel showing entries the backend ignores.
    Optional: a workflow with no normalizer keeps the raw round-trip.

    ``source`` splits the two trust tiers. Everything below it describes a
    community record and stays at its default for a built-in: ``extension_api``
    (the package's declared compatibility boundary), ``content_digest`` (which
    revision this record was compiled from), and ``load_status`` /
    ``diagnostic`` (why an installed package publishes no entry points).
    ``frontend_kind`` is derived from ``source`` rather than stored, so no
    field exists that could put a community record into the band the frontend
    dynamically ``import()``s.
    """

    id: str
    display_name: str
    tools: list[ToolSpec] = field(default_factory=list)
    config_defaults: dict = field(default_factory=dict)
    config_schema: dict | None = None
    produces_artifacts: bool = False
    subscriptions: list[Subscription] = field(default_factory=list)
    config_normalizer: Callable[[Any], dict] | None = None
    source: WorkflowSource = WorkflowSource.BUILTIN
    extension_api: int | None = None
    content_digest: str | None = None
    load_status: LoadStatus = LoadStatus.AVAILABLE
    diagnostic: str = ""
    fragment_types: tuple[FragmentTypeDefinition, ...] = ()
    writer_tool_selected: bool = False
    """Whether the user selected this package as the active Writer resolver.

    Carried on the record rather than read from the database mid-turn, so
    availability *and* activation are decided by one captured generation. A turn
    that resolves its resolver from a snapshot cannot have the selection change
    underneath it, and cannot send one package's schema while invoking
    another's."""

    audit_detectors: tuple[AuditDetectorBinding, ...] = ()
    """The record's contributed audit detectors.

    Beside ``writer_tool`` and for the same reason: a separate mechanism from
    ``tools``, resolved only through a captured snapshot. The built-in band
    leaves it empty -- Orb's own scanners are ``AUDIT_TYPES`` in ``analysis/``,
    not bindings anything can append to."""

    writer_tool: WriterToolBinding | None = None
    """The record's contributed Writer tool, if it has one.

    A separate field from ``tools`` because it is a separate mechanism: this one
    never reaches ``register_tool``, never claims a name in the mutable
    inference registry, and is resolved only through a captured snapshot. The
    built-in band leaves it ``None`` -- Orb ships no Writer tool of its own, and
    "no Writer tools" is an empty snapshot mapping rather than a module global
    anything could append to."""

    @property
    def frontend_kind(self) -> FrontendKind:
        """Derived, never declared: only a built-in is a trusted module."""
        return FrontendKind.TRUSTED_MODULE if self.source is WorkflowSource.BUILTIN else FrontendKind.DECLARATIVE


@dataclass(frozen=True)
class Subscription:
    """A workflow's binding into one pipeline hook slot.

    ``priority`` only matters for fan-out slots (``PRE_PIPELINE``,
    ``POST_PIPELINE``); single-dispatch slots are resolved by workflow id
    and ignore it.

    ``stage`` applies to ``POST_PIPELINE`` only and is ignored elsewhere;
    ``source`` mirrors the owning record's tier so ordering can band without
    a second lookup.
    """

    hook_type: HookType
    callable: Callable
    priority: int = 0
    workflow_id: str = ""
    stage: HookStage = HookStage.TRANSFORM
    source: WorkflowSource = WorkflowSource.BUILTIN


class ToolNameCollision(Exception):
    """Raised when a workflow tries to claim a tool name that is already
    taken by a built-in pass or by another registered workflow."""


class WorkflowMandateError(ValueError):
    """Raised by ``finalize_registry`` when a ``produces_artifacts=True``
    workflow lacks ``REGENERATE`` and/or ``REROLL_GEN``. Failing at import
    rather than on the first regen click avoids shipping a half-bound
    artifact workflow into production."""


class WorkflowDeclarationError(ValueError):
    """Raised when a workflow's static declaration is internally invalid."""


_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_COMMUNITY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""The lowercase fragment/card grammar, reused for community extension ids.

Deliberately stricter than ``_WORKFLOW_ID_RE``, which admits uppercase for the
built-ins. A community id is also a state-slot key, a namespaced fragment-type
prefix, and a content-store path component, so ``scene-meter`` and
``Scene-Meter`` must not be two installable packages: on a case-folding
filesystem they are one, and on Linux they are two, and the difference would
decide whose conversation state one of them reads. Matches
``features/extensions/contracts/common.EXTENSION_ID_PATTERN``, which the
manifest parser enforces on the other side of the same boundary.
"""

RESERVED_WORKFLOW_IDS: frozenset[str] = frozenset({"macros"})
"""Ids a community package may not claim.

``macros`` is the reserved per-message state slot; a package owning it would
namespace its state onto Orb's own key. Built-in ids are reserved implicitly by
being present in the base.
"""


_WORKFLOWS_BY_ID: dict[str, Workflow] = {}
"""The built-in base. Populated at import time; never holds a community record."""


def _declared_function_name(payload: object) -> object:
    if not isinstance(payload, Mapping):
        return None
    function = payload.get("function")
    return function.get("name") if isinstance(function, Mapping) else None


def register_workflow(w: Workflow) -> None:
    """Register or replace a workflow. Idempotent on ``w.id``.

    Tool diff against any prior registration of the same id:
      - names in new only -> ``register_tool`` is called fresh
      - names in both -> ``register_tool`` overwrites schema/choice and
        toggles the standalone bit symmetrically
      - names in old only -> removed from ``TOOLS`` and
        ``STANDALONE_TOOLS`` so the workflow's declaration stays the
        single source of truth for what it owns

    Validation order: declaration shape (id grammar, unique tool names,
    schema/choice name symmetry), built-in name reservation, then
    cross-workflow collision on names this registration is newly claiming.
    Every check runs before state mutation, so a rejected call leaves the
    registry, ``TOOLS``, and ``STANDALONE_TOOLS`` exactly as they were.
    Re-registration preserves the original insertion position so manifest
    ordering stays stable across reloads.
    """
    if not isinstance(w.id, str) or _WORKFLOW_ID_RE.fullmatch(w.id) is None:
        raise WorkflowDeclarationError(
            f"workflow id {w.id!r} must be 1-64 ASCII letters, digits, underscores, or hyphens and start with a letter or digit"
        )
    if w.source is not WorkflowSource.BUILTIN:
        raise WorkflowDeclarationError(
            f"workflow {w.id!r} declares source={w.source.value!r}; community records enter through "
            f"publish_community_overlay, not register_workflow"
        )

    seen_tool_names: set[str] = set()
    for spec in w.tools:
        if not isinstance(spec.name, str) or _TOOL_NAME_RE.fullmatch(spec.name) is None:
            raise WorkflowDeclarationError(
                f"workflow {w.id!r} tool name {spec.name!r} must be 1-64 ASCII letters, digits, underscores, or hyphens"
            )
        if spec.name in seen_tool_names:
            raise WorkflowDeclarationError(f"workflow {w.id!r} declares duplicate tool name {spec.name!r}")
        seen_tool_names.add(spec.name)

        schema_name = _declared_function_name(spec.schema)
        if schema_name != spec.name:
            raise WorkflowDeclarationError(
                f"workflow {w.id!r} tool {spec.name!r} schema function name must match the declared name"
            )
        choice_name = _declared_function_name(spec.choice)
        if choice_name != spec.name:
            raise WorkflowDeclarationError(
                f"workflow {w.id!r} tool {spec.name!r} choice function name must match the declared name"
            )

        if spec.name in BUILTIN_TOOL_NAMES:
            raise ToolNameCollision(f"workflow {w.id!r} cannot claim built-in tool name {spec.name!r}")

    old = _WORKFLOWS_BY_ID.get(w.id)
    old_tool_names = {t.name for t in old.tools} if old else frozenset()
    new_tool_names = {t.name for t in w.tools}

    newly_claimed = new_tool_names - old_tool_names
    for name in newly_claimed:
        for other in _WORKFLOWS_BY_ID.values():
            if other.id == w.id:
                continue
            if any(t.name == name for t in other.tools):
                raise ToolNameCollision(f"workflow {w.id!r} cannot claim tool name {name!r} owned by workflow {other.id!r}")

    for spec in w.tools:
        register_tool(spec.name, spec.schema, spec.choice, standalone=spec.standalone)

    for orphan in old_tool_names - new_tool_names:
        TOOLS.pop(orphan, None)
        STANDALONE_TOOLS.discard(orphan)

    _WORKFLOWS_BY_ID[w.id] = w


def subscribe(
    workflow_id: str,
    hook_type: HookType,
    fn: Callable,
    *,
    priority: int = 0,
    stage: HookStage = HookStage.TRANSFORM,
) -> None:
    record = _WORKFLOWS_BY_ID.get(workflow_id)
    if record is None:
        raise LookupError(f"subscribe: workflow {workflow_id!r} not registered")
    _bind_subscription(record, hook_type, fn, priority=priority, stage=stage)


def _bind_subscription(
    record: Workflow,
    hook_type: HookType,
    fn: Callable,
    *,
    priority: int,
    stage: HookStage,
) -> None:
    """Append one validated subscription to *record*.

    Shared by ``subscribe`` (built-in base) and the community overlay builder,
    so both tiers enforce the same one-binding-per-hook rule and the same
    artifact mandate. The overlay cannot reach ``subscribe`` itself because its
    records are deliberately absent from the base dict.
    """
    if any(s.hook_type is hook_type for s in record.subscriptions):
        raise ValueError(f"workflow {record.id!r} already has a {hook_type.value} subscription")
    if hook_type in (HookType.REGENERATE, HookType.REROLL_GEN) and not record.produces_artifacts:
        raise ValueError(f"workflow {record.id!r} cannot subscribe to {hook_type.value} without produces_artifacts=True")
    record.subscriptions.append(Subscription(hook_type, fn, priority, record.id, stage, record.source))


def workflow_has_hook(w: Workflow, hook_type: HookType) -> bool:
    return any(s.hook_type is hook_type for s in w.subscriptions)


def _assert_artifact_mandate(workflows: Iterable[Workflow]) -> None:
    for w in workflows:
        if not w.produces_artifacts:
            continue
        missing = [
            name
            for name, hook in (
                ("regenerate", HookType.REGENERATE),
                ("reroll_gen", HookType.REROLL_GEN),
            )
            if not workflow_has_hook(w, hook)
        ]
        if missing:
            raise WorkflowMandateError(
                f"workflow {w.id!r} declares produces_artifacts=True but lacks subscriptions: {', '.join(missing)}"
            )


def _assert_writer_tool_bindings(records: Iterable[Workflow]) -> None:
    """Validate every Writer-tool binding before the overlay swaps.

    Five properties, all checked here because the snapshot is the last place
    they are all visible at once:

    * the binding, its schema, its digest, and its extension id belong to **one**
      compiled revision -- a spec whose digest disagrees with its record is a
      binding that could execute a different revision than the schema described;
    * the wire name is the one Orb derives, not one a package chose;
    * names are globally unique, so a provider-returned name resolves to exactly
      one binding;
    * the count and the aggregate encoded blob fit the host caps.

    Entirely before the swap, like every other publish check, so a rejected
    overlay leaves the prior Writer tool active rather than none.
    """
    seen: dict[str, str] = {}
    selected: list[str] = []
    total_bytes = 0
    for record in records:
        binding = record.writer_tool
        if binding is None:
            if record.writer_tool_selected:
                raise WorkflowDeclarationError(
                    f"community workflow {record.id!r} is selected as the Writer resolver but publishes no binding"
                )
            continue
        if record.writer_tool_selected:
            selected.append(record.id)
        spec = binding.spec
        if spec.key.owner_id != record.id:
            raise WorkflowDeclarationError(
                f"Writer tool {spec.wire_name!r} is owned by {spec.key.owner_id!r} but published by workflow {record.id!r}"
            )
        expected = wire_name(WriterToolKey(owner_id=record.id, local_id=spec.key.local_id))
        if spec.wire_name != expected:
            raise WorkflowDeclarationError(
                f"Writer tool for workflow {record.id!r} publishes wire name {spec.wire_name!r}, expected {expected!r}"
            )
        if spec.content_digest != record.content_digest:
            raise WorkflowDeclarationError(
                f"Writer tool {spec.wire_name!r} does not belong to workflow {record.id!r}'s compiled revision"
            )
        declared = _declared_function_name(spec.schema)
        if declared != spec.wire_name:
            raise WorkflowDeclarationError(
                f"Writer tool {spec.wire_name!r} carries a schema whose function name is {declared!r}"
            )
        if spec.wire_name in seen:
            raise WorkflowDeclarationError(
                f"Writer tool name {spec.wire_name!r} is claimed by both {seen[spec.wire_name]!r} and {record.id!r}"
            )
        seen[spec.wire_name] = record.id
        total_bytes += len(
            json.dumps(spec.provider_schema(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
    if len(selected) > 1:
        raise WorkflowDeclarationError(
            f"{len(selected)} extensions claim the active Writer resolver ({', '.join(sorted(selected))}); "
            f"the selection is exclusive"
        )
    if len(seen) > MAX_WRITER_TOOLS_PUBLISHED:
        raise WorkflowDeclarationError(
            f"{len(seen)} Writer tools exceeds the limit of {MAX_WRITER_TOOLS_PUBLISHED} per registry snapshot"
        )
    if total_bytes > MAX_WRITER_TOOL_BLOB_BYTES:
        raise WorkflowDeclarationError(
            f"published Writer tool schemas total {total_bytes} bytes, over the {MAX_WRITER_TOOL_BLOB_BYTES} byte limit"
        )


def _assert_audit_detector_bindings(records: Iterable[Workflow]) -> None:
    """Validate every audit-detector binding before the overlay swaps.

    The same properties ``_assert_writer_tool_bindings`` checks, minus the ones
    that are about a provider-facing name (a detector has none) and the tool
    blob (a detector adds no prompt bytes): the binding, its key, and its digest
    belong to one compiled revision; keys are globally unique; and the count fits
    the host cap. Entirely before the swap, so a rejected overlay leaves the
    prior detector set active rather than none.
    """
    seen: dict[str, str] = {}
    for record in records:
        for binding in record.audit_detectors:
            spec = binding.spec
            if spec.owner_id != record.id:
                raise WorkflowDeclarationError(
                    f"audit detector {spec.key!r} is owned by {spec.owner_id!r} but published by workflow {record.id!r}"
                )
            if spec.content_digest != record.content_digest:
                raise WorkflowDeclarationError(
                    f"audit detector {spec.key!r} does not belong to workflow {record.id!r}'s compiled revision"
                )
            if spec.key in seen:
                raise WorkflowDeclarationError(
                    f"audit detector key {spec.key!r} is claimed by both {seen[spec.key]!r} and {record.id!r}"
                )
            seen[spec.key] = record.id
    if len(seen) > MAX_AUDIT_DETECTORS_PUBLISHED:
        raise WorkflowDeclarationError(
            f"{len(seen)} audit detectors exceeds the limit of {MAX_AUDIT_DETECTORS_PUBLISHED} per registry snapshot"
        )


def finalize_registry() -> None:
    """Validate that every ``produces_artifacts=True`` workflow has both
    ``REGENERATE`` and ``REROLL_GEN`` subscriptions.

    Invoke at the bottom of any module that completes a workflow's wiring
    -- this is the only hook that fails import on a partially-bound
    artifact workflow rather than deferring the crash to the first regen
    click. The same validation runs again before every community overlay
    publish, so an installed package cannot smuggle in the half-bound state
    the import-time check exists to prevent.
    """
    _assert_artifact_mandate(_WORKFLOWS_BY_ID.values())


# ═══════════════════════════════════════════════════════════════════════════
# Immutable snapshots: built-in base + community overlay
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """One resolved, ordered view of the registry at a point in time.

    Readers capture a snapshot once and resolve everything against it. The
    resolved record set and the per-hook ordering cannot change underneath an
    in-flight turn, which is the guarantee the design calls for: an update
    cannot mix an old pre-hook with a new post-hook, or an old fragment schema
    with a new reducer.

    ``generation`` increases on every lifecycle mutation (install, update,
    rollback, enablement change, permission change, uninstall). The frontend
    uses it to discard stale catalog and view responses; callers use it to
    assert that two reads came from the same registry.

    ``digests`` maps each community id to the content digest its record was
    compiled from -- the reference that keeps content garbage collection from
    deleting a directory an in-flight invocation still describes.
    """

    generation: int
    workflows: Mapping[str, Workflow]
    by_hook: Mapping[HookType, tuple[Subscription, ...]]
    digests: Mapping[str, str]
    fragment_types: Mapping[str, FragmentTypeDefinition]
    active_writer_tool: str | None = None
    """The wire name of the one selected resolver, or ``None``.

    Availability is not activation: :attr:`writer_tools` holds every eligible
    contribution, and this names the single one a turn may actually send and
    invoke. Both come from the same captured generation, so "the schema I sent"
    and "the binding I ran" cannot disagree."""

    audit_detectors: Mapping[str, AuditDetectorBinding] = field(default_factory=lambda: MappingProxyType({}))
    """Every published audit-detector binding, keyed by ``"<ext>:<local>"``.

    Deterministic insertion order (extension id, then local id) for the same
    reason the fragment-type and Writer-tool loops sort: two users with the same
    packages must get the same turn regardless of install order. Availability,
    not activation -- ``settings.editor_audit_toggles`` decides which of these a
    turn actually runs, and it defaults a contributed key to *off*."""

    writer_tools: Mapping[str, WriterToolBinding] = field(default_factory=lambda: MappingProxyType({}))
    """Every eligible Writer-tool binding, keyed by its derived wire name.

    Insertion order is deterministic (extension id, then local tool id), so two
    users with the same packages assemble the same tool blob regardless of
    install order -- the same rule the hook bands follow, and for the same
    reason: prompt bytes must not depend on history.

    Availability, not activation. Every eligible contribution is here; at most
    one of them enters a turn's tool blob, and which one is a local selection
    the pipeline resolves against this mapping."""

    def get(self, workflow_id: str) -> Workflow | None:
        return self.workflows.get(workflow_id)

    def writer_tool(self, wire: str | None) -> WriterToolBinding | None:
        """Resolve a provider-returned name through the captured mapping only.

        Never a global lookup: a turn may execute exactly the binding it
        published a schema for, and going through the snapshot is what makes a
        name from a newer or older revision resolve to nothing rather than to
        the wrong flow."""
        return self.writer_tools.get(wire) if wire else None

    def list(self) -> list[Workflow]:
        """Records in manifest order: built-ins first, then community ids."""
        return list(self.workflows.values())

    def subscriptions(self, hook_type: HookType) -> tuple[Subscription, ...]:
        return self.by_hook.get(hook_type, ())

    def subscription(self, workflow_id: str, hook_type: HookType) -> Subscription | None:
        record = self.workflows.get(workflow_id)
        if record is None:
            return None
        return next((s for s in record.subscriptions if s.hook_type is hook_type), None)

    def fragment_type(self, type_id: str) -> FragmentTypeDefinition | None:
        return self.fragment_types.get(type_id)


@dataclass(frozen=True, slots=True)
class _Published:
    """The community half of the registry, as one indivisible value.

    Generation and overlay live in the same object rather than in two module
    globals because they are one fact. Two globals can only be written one at a
    time, and a reader landing between the writes would build a snapshot
    labelled with the new generation but carrying the old records -- a stale
    record set wearing a fresh label, which is precisely what the generation
    exists to let callers detect.
    """

    generation: int
    overlay: tuple[Workflow, ...] = ()


_GENERATION_COUNTER = itertools.count(1)
_PUBLISHED = _Published(generation=next(_GENERATION_COUNTER))


def _ordered_for_hook(
    base: Mapping[str, Workflow],
    overlay: Sequence[Workflow],
    hook_type: HookType,
) -> tuple[Subscription, ...]:
    """Resolve one hook slot's execution order.

    Stage precedes source band. Within a band, built-ins keep their declared
    priority and, on a tie, their registration order; community entries sort by
    priority then extension id. Community ordering is deliberately independent
    of installation time -- two users with the same set of packages must get
    the same turn.

    The two bands are sorted separately and concatenated rather than sorted by
    one composite key, because their tie-breakers are different types and a
    single key would only be well-ordered by accident.
    """
    stages: tuple[HookStage | None, ...] = (
        (HookStage.TRANSFORM, HookStage.OBSERVE) if hook_type is HookType.POST_PIPELINE else (None,)
    )
    insertion = {wid: index for index, wid in enumerate(base)}
    ordered: list[Subscription] = []
    for stage in stages:

        def matches(sub: Subscription, stage: HookStage | None = stage) -> bool:
            return sub.hook_type is hook_type and (stage is None or sub.stage is stage)

        builtin = [s for w in base.values() for s in w.subscriptions if matches(s)]
        builtin.sort(key=lambda s: (s.priority, insertion.get(s.workflow_id, 0)))
        community = [s for w in overlay for s in w.subscriptions if matches(s)]
        community.sort(key=lambda s: (s.priority, s.workflow_id))
        ordered.extend(builtin)
        ordered.extend(community)
    return tuple(ordered)


def current_snapshot() -> RegistrySnapshot:
    """Capture the registry as it stands right now.

    Call this **once** per turn, request, or lifecycle operation and thread the
    result through everything that follows. Re-reading it mid-turn is the bug
    the snapshot exists to prevent: two reads can straddle a publish and
    disagree about which revision the turn is running.

    Resolution is a pure function of the base dict and one ``_Published``
    reference, both read once here, so a concurrent publish either lands
    entirely before this call or entirely after it. Reading the generation off
    that same reference -- not off a second global -- is what makes "entirely"
    true of the label as well as the records.
    """
    base = _WORKFLOWS_BY_ID
    published = _PUBLISHED
    overlay = published.overlay
    workflows: dict[str, Workflow] = dict(base)
    for record in overlay:
        workflows[record.id] = record
    fragment_types = dict(BUILTIN_FRAGMENT_TYPES)
    writer_tools: dict[str, WriterToolBinding] = {}
    audit_detectors: dict[str, AuditDetectorBinding] = {}
    active_writer_tool: str | None = None
    for record in sorted(overlay, key=lambda item: item.id):
        for descriptor in sorted(record.fragment_types, key=lambda item: item.local_id):
            fragment_types[descriptor.type_id] = descriptor
        for detector in sorted(record.audit_detectors, key=lambda item: item.key):
            audit_detectors[detector.key] = detector
        if record.writer_tool is not None:
            writer_tools[record.writer_tool.wire_name] = record.writer_tool
            if record.writer_tool_selected:
                active_writer_tool = record.writer_tool.wire_name
    return RegistrySnapshot(
        generation=published.generation,
        workflows=MappingProxyType(workflows),
        by_hook=MappingProxyType({hook: _ordered_for_hook(base, overlay, hook) for hook in HookType}),
        digests=MappingProxyType({r.id: r.content_digest for r in overlay if r.content_digest}),
        fragment_types=MappingProxyType(fragment_types),
        active_writer_tool=active_writer_tool,
        writer_tools=MappingProxyType(writer_tools),
        audit_detectors=MappingProxyType(audit_detectors),
    )


def runtime_generation() -> int:
    """The current runtime generation, without building a snapshot."""
    return _PUBLISHED.generation


def publish_community_overlay(records: Sequence[Workflow]) -> int:
    """Replace the whole community overlay and return the new generation.

    Wholesale replacement, not incremental patching: a lifecycle mutation
    recompiles every installed package's record set and swaps one immutable
    tuple in. That is what makes "install/update/rollback publish content, DB
    metadata, grants, enablement, and runtime generation as one observed
    transition" true -- there is no window in which half the overlay is new.

    Validation runs entirely before the swap, so a rejected publish leaves the
    prior overlay, its commands, and its fragment descriptors active.
    """
    seen: set[str] = set()
    seen_fragment_types: set[str] = set(BUILTIN_FRAGMENT_TYPES)
    for record in records:
        if record.source is not WorkflowSource.COMMUNITY:
            raise WorkflowDeclarationError(f"overlay record {record.id!r} must declare source=COMMUNITY")
        if not isinstance(record.id, str) or _COMMUNITY_ID_RE.fullmatch(record.id) is None:
            raise WorkflowDeclarationError(
                f"community workflow id {record.id!r} does not match the lowercase extension id grammar"
            )
        if record.id in RESERVED_WORKFLOW_IDS:
            raise WorkflowDeclarationError(f"community workflow id {record.id!r} is reserved")
        if record.id in _WORKFLOWS_BY_ID:
            raise WorkflowDeclarationError(f"community workflow id {record.id!r} collides with a built-in workflow")
        if record.id in seen:
            raise WorkflowDeclarationError(f"duplicate community workflow id {record.id!r}")
        # Community packages do not add tools to Director, Writer, or Editor.
        # Enabling an ordinary extension must not change the main tool blob, so
        # the declaration is rejected here rather than filtered downstream.
        if record.tools:
            raise WorkflowDeclarationError(
                f"community workflow {record.id!r} declares tools; extension flows make their own bounded "
                f"model calls and never enter the shared tool registry"
            )
        # "Publishes no entry points" has to cover produces_artifacts too, not
        # just subscriptions. It is an entry-point declaration -- it is what puts
        # regenerate/reroll buttons on an attachment -- and leaving it set on an
        # unavailable record would trip the artifact mandate below, failing the
        # *whole* overlay swap over one broken package. Startup must isolate a
        # bad package, not let it block every other extension and the built-ins.
        if record.load_status is not LoadStatus.AVAILABLE and (
            record.subscriptions
            or record.produces_artifacts
            or record.fragment_types
            or record.writer_tool
            or record.audit_detectors
        ):
            raise WorkflowDeclarationError(
                f"community workflow {record.id!r} is {record.load_status.value} but published entry points; "
                f"an unavailable record must carry no subscriptions, fragment types, Writer tool, audit detectors, "
                f"or artifact production"
            )
        for descriptor in record.fragment_types:
            expected = f"{record.id}:{descriptor.local_id}"
            if descriptor.owner_id != record.id or descriptor.type_id != expected:
                raise WorkflowDeclarationError(
                    f"fragment type {descriptor.type_id!r} does not belong to community workflow {record.id!r}"
                )
            if descriptor.content_digest != record.content_digest:
                raise WorkflowDeclarationError(
                    f"fragment type {descriptor.type_id!r} does not belong to workflow {record.id!r}'s compiled revision"
                )
            if descriptor.type_id in seen_fragment_types:
                raise WorkflowDeclarationError(f"duplicate fragment type id {descriptor.type_id!r}")
            seen_fragment_types.add(descriptor.type_id)
        seen.add(record.id)
    _assert_artifact_mandate(records)
    _assert_writer_tool_bindings(records)
    _assert_audit_detector_bindings(records)

    global _PUBLISHED
    _PUBLISHED = _Published(generation=next(_GENERATION_COUNTER), overlay=tuple(records))
    return _PUBLISHED.generation


def bump_generation() -> int:
    """Advance the runtime generation without changing published records.

    Enablement and permission changes do not recompile anything, but they do
    change what the frontend should be showing, and a stale catalog response
    that arrives after one of them must still be discardable.
    """
    global _PUBLISHED
    _PUBLISHED = _Published(generation=next(_GENERATION_COUNTER), overlay=_PUBLISHED.overlay)
    return _PUBLISHED.generation


# ── snapshot-aware resolvers ────────────────────────────────────────────────
# Each takes an explicit snapshot. Passing None captures one, which is correct
# for a single lookup at the top of a request but wrong inside a turn -- the
# turn already holds one and must use it.


def iter_subscriptions(hook_type: HookType, *, snapshot: RegistrySnapshot | None = None) -> list[Subscription]:
    """Return subscriptions of ``hook_type`` in resolved execution order."""
    return list((snapshot or current_snapshot()).subscriptions(hook_type))


def get_subscription(workflow_id: str, hook_type: HookType, *, snapshot: RegistrySnapshot | None = None) -> Subscription | None:
    """Return the workflow's subscription for ``hook_type``, or None.

    Collapses "unregistered" and "no binding" into one None -- the routes
    that use this (regenerate, reroll_gen) treat both as 404 anyway.
    """
    return (snapshot or current_snapshot()).subscription(workflow_id, hook_type)


def list_workflows(*, snapshot: RegistrySnapshot | None = None) -> list[Workflow]:
    """Return the resolved record list: built-ins in registration order,
    then community records."""
    return (snapshot or current_snapshot()).list()


def get_workflow(workflow_id: str, *, snapshot: RegistrySnapshot | None = None) -> Workflow | None:
    """Look up a workflow by id, or None if not registered."""
    return (snapshot or current_snapshot()).get(workflow_id)


async def get_workflow_state(conv_id: str, workflow_id: str) -> dict | None:
    """Return the workflow's per-conversation slot, or None if empty."""
    return await _db_get_workflow_state(conv_id, workflow_id)


async def set_workflow_state(conv_id: str, workflow_id: str, payload: dict | None) -> None:
    """Write the workflow's per-conversation slot. None removes it."""
    await _db_set_workflow_state(conv_id, workflow_id, payload)


async def get_workflow_message_state(message_id: int, workflow_id: str) -> dict | None:
    """Return the workflow's per-message slot, or None if empty."""
    return await _db_get_workflow_message_state(message_id, workflow_id)


async def set_workflow_message_state(message_id: int, workflow_id: str, payload: dict | None) -> None:
    """Write the workflow's per-message slot. None removes it."""
    await _db_set_workflow_message_state(message_id, workflow_id, payload)


async def get_workflow_character_state(character_id: str, workflow_id: str) -> dict | None:
    return await _db_get_workflow_character_state(character_id, workflow_id)


async def set_workflow_character_state(character_id: str, workflow_id: str, payload: dict | None) -> None:
    await _db_set_workflow_character_state(character_id, workflow_id, payload)


async def get_workflow_config(workflow_id: str) -> dict:
    """Return the workflow's global config slot.

    Falls back to the workflow's ``config_defaults`` (fresh copy) when the
    persisted slot is empty so callers can read into a populated dict
    without an existence check. An unregistered ``workflow_id`` with an
    empty slot returns an empty dict.

    Callers doing read-then-write must hold ``workflow_config_lock()``
    across the full RMW window so the value observed here -- whether from
    the persisted slot or the ``config_defaults`` fallback -- still
    matches the slot when ``set_workflow_config`` writes it back.
    """
    raw = await _db_get_workflow_config(workflow_id)
    if raw:
        return raw
    w = get_workflow(workflow_id)
    if w is not None:
        return dict(w.config_defaults)
    return {}


async def set_workflow_config(workflow_id: str, payload: dict) -> None:
    """Write the workflow's global config slot. Empty dict clears it.

    Caller must hold ``workflow_config_lock()`` across the read-then-write
    the payload was computed from. Direct use without the lock is safe for
    blind-replace writes; RMW sequences (``get_workflow_config`` -> mutate
    -> ``set_workflow_config``) silently lose writes under contention
    because the read happens in a separate transaction outside the lock.
    """
    await _db_set_workflow_config(workflow_id, payload)


def overlay_enable_tools(
    base: Mapping[str, bool],
    contribution: set[str] | Mapping[str, bool] | None,
) -> dict[str, bool]:
    """Return a fresh mutable dict copy of *base* with *contribution*'s
    True entries merged in. Mirrors the orchestrator merge semantics:
    True wins, False is ignored. None or an empty contribution returns a
    fresh copy of *base* unchanged.

    Accepts any ``Mapping`` for *base* including a ``MappingProxyType``;
    the return is always a plain ``dict`` the caller may mutate freely
    (e.g. to pass to ``forced_tool_call``'s ``enabled_tools=`` argument).
    Contribution may be a ``set`` (presence = enable) or a ``Mapping``
    (True entries kept, False entries dropped). The orchestrator does the
    logged-warning at its own merge site; this helper trusts the caller.
    """
    result = dict(base)
    if contribution is None:
        return result
    if isinstance(contribution, (set, frozenset)):
        for name in contribution:
            result[name] = True
    else:
        for name, enabled in contribution.items():
            if enabled:
                result[name] = True
    return result
