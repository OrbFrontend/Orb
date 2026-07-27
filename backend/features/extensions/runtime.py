"""Loading installed revisions and publishing them as one runtime overlay.

This module answers "what is installed, and what can it do right now?" by
compiling every installed package's active revision from the content store and
publishing the resulting records into the workflow registry as a single
immutable overlay. It is the seam between package *data* and Orb's *runtime*.

Three state axes stay separate all the way through, because collapsing any two
of them is how "your extension is disabled" starts being shown for "this
package needs a newer Orb":

* **installed** -- a row in ``extension_packages`` with an active digest.
* **load status** -- whether that revision is ``available``, ``incompatible``,
  ``invalid``, or ``missing_content``. Derived by recompiling, never trusted
  from the stored column.
* **enablement** -- the global master switch and
  ``settings.workflow_enabled[extension_id]``, which live in the settings row
  and survive uninstall.

Grants are a fourth, independent fact. A package may be installed, available,
and enabled and still publish nothing, because the entry point's derived
requirement set is not covered by what the user approved.

Phase 2 binds only compiled, available, enabled, and fully granted declarative
flows through the constrained interpreter. Unavailable or under-granted entry
points contribute catalog metadata and diagnostics but no executable binding,
so no lifecycle path can publish work the runtime cannot honor end to end.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...database import list_extension_packages
from ...database.models import ExtensionPackageRuntimeRow
from ...workflows.contracts import LoadStatus, WorkflowSource
from ...workflows.registry import (
    Workflow,
    _bind_subscription,
    current_snapshot,
    publish_community_overlay,
    runtime_generation,
)
from . import content_store
from .adapters import hook_bindings
from .compiler import (
    CompiledPackage,
    Requirement,
    compile_package,
    derive_flow_requirements,
    derive_view_runtime_requirements,
    expand_permissions,
)
from .contracts import referenced_actions
from .errors import PackageError, PackageIncompatible
from .interpreter import unimplemented_operations
from .sources import StoredSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InstalledExtension:
    """One installed package as the catalog and the publisher both see it.

    ``compiled`` is ``None`` exactly when ``load_status`` is not ``available``:
    there is no half-compiled state, because a revision that failed any part of
    compilation contributes no flows, views, or requirements at all.

    ``blocked`` names the entry points whose requirements the current grants do
    not cover. They are listed rather than silently dropped -- the design's
    "blocked hooks/actions/views are listed in its diagnostic; they do not fail
    halfway through ordinary use".
    """

    row: ExtensionPackageRuntimeRow
    load_status: LoadStatus
    diagnostic: str
    compiled: CompiledPackage | None = None
    blocked: tuple[str, ...] = ()
    granted: frozenset[Requirement] = field(default_factory=frozenset)

    @property
    def id(self) -> str:
        return self.row["id"]

    @property
    def digest(self) -> str:
        return self.row["active_digest"]

    @property
    def display_name(self) -> str:
        return self.compiled.manifest.name if self.compiled else self.row["id"]

    @property
    def version(self) -> str:
        return self.compiled.manifest.version if self.compiled else ""


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """The published catalog, labelled with the generation it was published at.

    Distinct from the registry's ``RegistrySnapshot``: that one is the
    execution-critical view a turn captures and threads through its hooks. This
    one is the *management* view -- manifests, grants, diagnostics, blocked
    entry points -- which no turn reads. Keeping them apart means the catalog
    can carry parsed manifests without putting them on the turn's hot path.
    """

    generation: int
    packages: Mapping[str, InstalledExtension]

    def get(self, extension_id: str) -> InstalledExtension | None:
        return self.packages.get(extension_id)

    def list(self) -> list[InstalledExtension]:
        return list(self.packages.values())


_STATE = RuntimeState(generation=runtime_generation(), packages={})


def current_state() -> RuntimeState:
    """The published extension catalog as it stands right now."""
    return _STATE


def load_installed(rows: Sequence[ExtensionPackageRuntimeRow]) -> list[InstalledExtension]:
    """Compile every installed package independently, isolating failures.

    Independently is the whole point: one package whose content vanished must
    not stop the others -- or the built-ins -- from loading. Each failure
    becomes that package's own sanitized diagnostic and an empty entry-point
    set.
    """
    return [load_one(row) for row in rows]


def load_one(row: ExtensionPackageRuntimeRow) -> InstalledExtension:
    """Compile one installed revision and derive its runtime state."""
    digest = row["active_digest"]
    granted = frozenset(expand_permissions(_decode_grants(row)))

    if not content_store.exists(digest):
        return InstalledExtension(
            row=row,
            load_status=LoadStatus.MISSING_CONTENT,
            diagnostic="package content is missing from this machine; reinstall the package to restore it",
            granted=granted,
        )
    try:
        compiled = compile_package(StoredSource(content_store.content_path(digest)))
    except PackageIncompatible as exc:
        return InstalledExtension(row=row, load_status=LoadStatus.INCOMPATIBLE, diagnostic=str(exc), granted=granted)
    except PackageError as exc:
        return InstalledExtension(row=row, load_status=LoadStatus.INVALID, diagnostic=str(exc), granted=granted)

    if compiled.digest != digest:
        # Content that no longer hashes to the digest the database recorded is
        # not a package Orb can honor: the user consented to a specific
        # revision, and this is a different one.
        return InstalledExtension(
            row=row,
            load_status=LoadStatus.INVALID,
            diagnostic="stored package content does not match its recorded digest",
            granted=granted,
        )
    recorded_fingerprint = row["active_contract_fingerprint"]
    if recorded_fingerprint is None:
        return InstalledExtension(
            row=row,
            load_status=LoadStatus.INVALID,
            diagnostic="the active revision's recorded consent contract is missing",
            granted=granted,
        )
    if compiled.contract_fingerprint != recorded_fingerprint:
        return InstalledExtension(
            row=row,
            load_status=LoadStatus.INCOMPATIBLE,
            diagnostic="the package contract changed since consent; inspect and update it to approve the new contract",
            granted=granted,
        )
    if compiled.unavailable:
        return InstalledExtension(
            row=row,
            load_status=LoadStatus.INCOMPATIBLE,
            diagnostic=f"this Orb build does not provide: {', '.join(compiled.unavailable)}",
            compiled=None,
            granted=granted,
        )

    blocked = tuple(blocked_entry_points(compiled, granted))
    diagnostic = ""
    if blocked:
        diagnostic = f"not published without the permissions they need: {', '.join(blocked)}"
    return InstalledExtension(
        row=row,
        load_status=LoadStatus.AVAILABLE,
        diagnostic=diagnostic,
        compiled=compiled,
        blocked=blocked,
        granted=granted,
    )


def blocked_entry_points(compiled: CompiledPackage, granted: frozenset[Requirement]) -> Iterator[str]:
    """Entry points whose derived requirement set the grants do not cover.

    Views inherit the requirements of the actions they dispatch, so a partially
    granted package cannot publish a view whose only button would fail. That
    transitivity is why this walks bindings rather than flow files: one flow
    reached from two bindings can be blocked for one and not the other.
    """
    manifest = compiled.manifest

    def covered(path: str) -> bool:
        """Publishable: grants cover it *and* this build can execute it.

        Both conditions produce the same outcome for the same reason -- an
        entry point Orb cannot honor end to end must not be published and then
        fail partway through ordinary use. A flow reaching an operation a later
        phase implements is blocked exactly like an under-granted one, and both
        say so in the package's diagnostic.
        """
        flow = compiled.flows.get(path)
        if flow is None or unimplemented_operations(flow):
            return False
        return derive_flow_requirements(flow) <= granted

    if manifest.hooks.pre_pipeline and not covered(manifest.hooks.pre_pipeline.flow):
        yield "hook pre_pipeline"
    if manifest.hooks.post_pipeline and not covered(manifest.hooks.post_pipeline.flow):
        yield "hook post_pipeline"
    for name in sorted(manifest.actions):
        if not covered(manifest.actions[name].flow):
            yield f"action {name!r}"
    for view_id, binding in sorted(manifest.views.items()):
        view = compiled.views.get(binding.source)
        if view is None:
            continue
        if derive_view_runtime_requirements(view) - granted or any(
            not covered(manifest.actions[a].flow) for a in referenced_actions(view) if a in manifest.actions
        ):
            yield f"view {view_id!r}"
    for placement in manifest.placements:
        if ("ui.contribute", placement.slot) not in granted:
            yield f"placement in slot {placement.slot!r}"
    if manifest.contributions.fragment_types and ("fragment_type.contribute", None) not in granted:
        yield "fragment type contributions"


def publish(installed: Sequence[InstalledExtension]) -> int:
    """Swap the whole community overlay and catalog, returning the generation.

    Wholesale replacement of both bands in one call so an install, update,
    rollback, enablement change, permission change, or uninstall is one
    observed transition. The registry generation comes first and labels the
    catalog, so a catalog read can never carry a generation the registry has
    not reached.
    """
    global _STATE
    records = [_record(entry) for entry in installed]
    generation = publish_community_overlay(records)
    _STATE = RuntimeState(generation=generation, packages={entry.id: entry for entry in installed})
    return generation


async def publish_from_database() -> int:
    """Recompile everything installed and publish it. The lifecycle's commit."""
    return publish(load_installed(await list_extension_packages()))


def pinned_digests() -> set[str]:
    """Digests a live runtime snapshot or the published catalog still names.

    Content collection subtracts these on top of the database's references, so
    a directory an in-flight request still resolves assets from is never
    removed underneath it.
    """
    pinned = {entry.digest for entry in _STATE.packages.values()}
    pinned.update(digest for digest in current_snapshot().digests.values() if digest)
    return pinned


def _record(entry: InstalledExtension) -> Workflow:
    """Project one installed package into its community registry record.

    Available packages bind their unblocked hooks to generic interpreter
    adapters here. ``produces_artifacts`` stays False for every record until the
    phase that implements ``artifact.emit``: declaring the contract without the
    flows to honor it would trip the registry's artifact mandate and fail the
    *whole* overlay swap over one package that merely says it produces
    artifacts, which is exactly the isolation startup must preserve.
    """
    record = Workflow(
        id=entry.id,
        display_name=entry.display_name,
        source=WorkflowSource.COMMUNITY,
        extension_api=entry.compiled.manifest.extension_api if entry.compiled else None,
        content_digest=entry.digest,
        load_status=entry.load_status,
        diagnostic=entry.diagnostic,
    )
    if entry.compiled is not None and entry.load_status is LoadStatus.AVAILABLE:
        for hook_type, callable_, stage in hook_bindings(entry.compiled.manifest, entry.compiled.flows, entry.blocked):
            _bind_subscription(record, hook_type, callable_, priority=0, stage=stage)
    return record


def live_grants(extension_id: str) -> frozenset[Requirement]:
    """The currently approved requirement set for *extension_id*.

    Read from the published catalog on every call, never captured. This is the
    live grant view a privileged operation checks immediately before it runs, so
    revoking a permission stops the next operation of a flow that is already
    executing. An unknown or uninstalled id has no grants, which fails closed.
    """
    entry = _STATE.packages.get(extension_id)
    return entry.granted if entry is not None else frozenset()


def _decode_grants(row: ExtensionPackageRuntimeRow) -> list[dict[str, Any]]:
    """Decode ``approved_permissions``, degrading to "nothing granted".

    A corrupt grants column must not crash the catalog or, worse, be read as
    permissive. An empty grant set blocks every entry point and shows the
    package with a diagnostic, which is the recoverable failure.
    """
    try:
        decoded = json.loads(row["approved_permissions"] or "[]")
    except (TypeError, ValueError):
        logger.warning("extension %r has an undecodable approved_permissions column; treating as no grants", row["id"])
        return []
    if not isinstance(decoded, list):
        return []
    return [entry for entry in decoded if isinstance(entry, dict)]
