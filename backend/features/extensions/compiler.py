"""Reference-graph validation and compilation: package bytes -> immutable record.

The compiler is where the manifest's *claims* meet the package's *code*. It
walks every referenced flow, view, placement, asset, schema, secret use, and
contribution, derives the complete requirement set from what those files
actually reach, and then checks that the declared ``requires`` and
``permissions`` cover it. A declaration that omits something the code needs is
a validation error -- never a silently narrowed grant -- and a declared
requirement this build does not implement makes the revision *incompatible*
rather than invalid, because "update Orb" and "report a bug to the author" are
different user actions.

Three properties hold by construction:

* **Only referenced files are compiled.** :class:`~.sources.PackageSource`
  offers no enumeration, so the selected set is exactly the transitive closure
  of manifest references. An unreferenced README edit does not change the
  digest, because the digest is taken over the selected set.
* **Compilation is conservative over branches.** An operation behind
  ``when: false`` still contributes its capability. A consent diff that could
  be shrunk by adding an unsatisfiable predicate would not be a consent diff.
* **Compilation is total and repeatable.** The same bytes compile to the same
  digest and the same requirement set through an archive or through the
  content store, which is what lets startup reconciliation re-derive what the
  user consented to instead of trusting a stored summary.

Nothing here executes a flow, touches the database, publishes a record, or
reads a grant. It answers "what would this package need?", and the lifecycle
layer answers "may it have that?".
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .assets import assert_bytes_match, asset_media_type
from .contracts import (
    CONFIG_VIEW_ID,
    EXTENSION_API_VERSION,
    OPERATION_SPECS,
    RESOURCE_CAPABILITIES,
    Capability,
    Component,
    ExtensionManifest,
    Flow,
    OpContext,
    Requirement,
    StateSource,
    View,
    check_context,
    declared_operations,
    iter_components,
    iter_steps,
    parameter_values,
    parse_schema,
    permission_key,
    referenced_actions,
    referenced_assets,
    referenced_flow_assets,
    template_paths,
    used_components,
    with_prerequisites,
)
from .digest import PackageContent, canonical_json_bytes, content_digest
from .errors import PackageIncompatible, PackageValidationError
from .json_loader import load_json
from .limits import MAX_ASSET_BYTES, MAX_MANIFEST_BYTES, MAX_REFERENCED_BYTES_TOTAL
from .sources import MANIFEST_PATH, PackageSource


@dataclass(frozen=True, slots=True)
class DerivedRequirements:
    """Everything the package's own files reach, derived by walking them."""

    permissions: frozenset[Requirement]
    operations: frozenset[str]
    components: frozenset[str]
    secrets: frozenset[str]


@dataclass(frozen=True, slots=True)
class CompiledPackage:
    """One revision, fully validated, ready to persist and publish.

    ``files`` is the selected set the digest was taken over -- the manifest plus
    every referenced flow, view, and asset, and nothing else. ``unavailable``
    is non-empty when the package is well-formed but this build cannot run it;
    such a revision still installs, with the diagnostic, and publishes no entry
    points.
    """

    manifest: ExtensionManifest
    digest: str
    files: Mapping[str, PackageContent]
    flows: Mapping[str, Flow]
    views: Mapping[str, View]
    asset_types: Mapping[str, str]
    requirements: DerivedRequirements
    contract_fingerprint: str
    unavailable: tuple[str, ...] = ()

    @property
    def extension_id(self) -> str:
        return self.manifest.id

    def requested_permissions(self) -> list[dict[str, Any]]:
        """The manifest's permission requests as plain JSON, in declared order."""
        return [p.model_dump(mode="json", exclude_none=True) for p in self.manifest.permissions]

    def manifest_json(self) -> str:
        return canonical_json_bytes(self.manifest.model_dump(mode="json", by_alias=True, exclude_none=True)).decode("utf-8")


def compile_package(source: PackageSource) -> CompiledPackage:
    """Validate and compile one package, or raise.

    Raises :class:`PackageIncompatible` for an ``extension_api`` this build does
    not implement -- checked against the raw JSON before the strict models,
    because a manifest declaring API 2 is a package from the future, not a
    malformed one, and the Pydantic ``Literal[1]`` cannot tell the difference.
    """
    manifest_bytes = source.read(MANIFEST_PATH, max_bytes=MAX_MANIFEST_BYTES)
    raw = load_json(manifest_bytes, what=MANIFEST_PATH, max_bytes=MAX_MANIFEST_BYTES)
    _assert_api_version(raw)
    manifest = _parse_manifest(raw)

    files: dict[str, PackageContent] = {MANIFEST_PATH: PackageContent.json(raw)}
    flows = _load_flows(source, manifest, files)
    views = _load_views(source, manifest, files)
    asset_types = _load_assets(source, flows, views, files)

    _assert_flow_contexts(manifest, flows)
    _assert_view_actions(manifest, views)
    _assert_config_view_scope(manifest, views)
    _assert_fragment_view_profiles(manifest, views)
    _assert_artifact_declaration(manifest, flows)
    requirements = _derive_requirements(manifest, flows, views)
    _assert_declarations_cover(manifest, requirements)

    unavailable = tuple(manifest.requires.unknown())
    return CompiledPackage(
        manifest=manifest,
        digest=content_digest(files),
        files=files,
        flows=flows,
        views=views,
        asset_types=asset_types,
        requirements=requirements,
        contract_fingerprint=_fingerprint(manifest, requirements),
        unavailable=unavailable,
    )


# ── parsing ─────────────────────────────────────────────────────────────────


def _assert_api_version(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise PackageValidationError(f"{MANIFEST_PATH} must contain a JSON object")
    declared = raw.get("extension_api")
    if not isinstance(declared, int) or isinstance(declared, bool):
        raise PackageValidationError(f"{MANIFEST_PATH}: 'extension_api' must be an integer")
    if declared != EXTENSION_API_VERSION:
        raise PackageIncompatible(
            f"package declares extension_api {declared}; this Orb build implements extension_api {EXTENSION_API_VERSION}"
        )


def _parse_manifest(raw: Any) -> ExtensionManifest:
    try:
        return ExtensionManifest.model_validate(raw)
    except Exception as exc:
        raise PackageValidationError(f"{MANIFEST_PATH}: {_first_error(exc)}") from None


def _load_flows(source: PackageSource, manifest: ExtensionManifest, files: dict[str, PackageContent]) -> dict[str, Flow]:
    flows: dict[str, Flow] = {}
    for path in sorted(manifest.referenced_flow_paths()):
        raw = load_json(
            source.read(path, max_bytes=MAX_REFERENCED_BYTES_TOTAL),
            what=path,
            max_bytes=MAX_REFERENCED_BYTES_TOTAL,
        )
        try:
            flows[path] = Flow.model_validate(raw)
        except Exception as exc:
            raise PackageValidationError(f"{path}: {_first_error(exc)}") from None
        files[path] = PackageContent.json(raw)
    return flows


def _load_views(source: PackageSource, manifest: ExtensionManifest, files: dict[str, PackageContent]) -> dict[str, View]:
    views: dict[str, View] = {}
    for path in sorted(manifest.referenced_view_paths()):
        raw = load_json(
            source.read(path, max_bytes=MAX_REFERENCED_BYTES_TOTAL),
            what=path,
            max_bytes=MAX_REFERENCED_BYTES_TOTAL,
        )
        try:
            view = View.model_validate(raw)
            view.node_count()  # walks the tree, enforcing the depth bound
        except Exception as exc:
            raise PackageValidationError(f"{path}: {_first_error(exc)}") from None
        views[path] = view
        files[path] = PackageContent.json(raw)
    return views


def _load_assets(
    source: PackageSource,
    flows: Mapping[str, Flow],
    views: Mapping[str, View],
    files: dict[str, PackageContent],
) -> dict[str, str]:
    """Read every asset the package can render or attach, checking its bytes.

    Assets are discovered from the *parsed* flows and views, not from the
    manifest: only the compiler knows which files a component tree or an
    ``artifact.emit`` step reaches, which is exactly why the digest cannot be
    computed before they are parsed. Both sources feed one set, so an asset a
    view renders and a flow attaches is read, checked, and hashed once.
    """
    asset_types: dict[str, str] = {}
    paths = sorted(
        {path for view in views.values() for path in referenced_assets(view)}
        | {path for flow in flows.values() for path in referenced_flow_assets(flow)}
    )
    for path in paths:
        if path in files:
            raise PackageValidationError(f"asset {path!r} is also referenced as a flow or view")
        media_type = asset_media_type(path)
        data = source.read(path, max_bytes=MAX_ASSET_BYTES)
        assert_bytes_match(path, media_type, data)
        asset_types[path] = media_type
        files[path] = PackageContent.binary(data)
    return asset_types


# ── reference-graph checks ──────────────────────────────────────────────────


def _assert_flow_contexts(manifest: ExtensionManifest, flows: Mapping[str, Flow]) -> None:
    """Every flow must be legal in the context its binding puts it in.

    A flow bound to two contexts (an action that is also a reducer) must satisfy
    both. Checking per binding rather than per file is what makes that true
    without the package having to say which binding it "really" meant.
    """
    for path, context in _flow_contexts(manifest):
        problems = check_context(flows[path], context)
        if problems:
            raise PackageValidationError(f"{path}: {problems[0]}")


def _flow_contexts(manifest: ExtensionManifest) -> Iterator[tuple[str, OpContext]]:
    if manifest.hooks.pre_pipeline:
        yield manifest.hooks.pre_pipeline.flow, OpContext.PRE_PIPELINE
    if manifest.hooks.post_pipeline:
        stage = manifest.hooks.post_pipeline.stage
        yield (
            manifest.hooks.post_pipeline.flow,
            OpContext.POST_TRANSFORM if stage == "transform" else OpContext.POST_OBSERVE,
        )
    for binding in manifest.actions.values():
        yield binding.flow, OpContext.ACTION
    if manifest.artifact_flows is not None:
        yield manifest.artifact_flows.regenerate, OpContext.RECOVERY
        yield manifest.artifact_flows.reroll_gen, OpContext.RECOVERY
    for descriptor in manifest.contributions.fragment_types:
        yield descriptor.reduce_flow, OpContext.REDUCER


def _assert_view_actions(manifest: ExtensionManifest, views: Mapping[str, View]) -> None:
    for path, view in views.items():
        for name in sorted(referenced_actions(view)):
            if name not in manifest.actions:
                raise PackageValidationError(f"{path}: dispatches undeclared action {name!r}")


def _assert_artifact_declaration(manifest: ExtensionManifest, flows: Mapping[str, Flow]) -> None:
    """A flow that emits artifacts must be an artifact producer.

    The manifest validator already enforces the other direction (declaring
    ``produces_artifacts`` requires the flows and the grant). This is the one
    the manifest cannot see: only the compiler knows a flow reaches
    ``artifact.emit``. Without it the package would install, the registry would
    bind no regenerate/reroll pair, and the bridge would silently drop every
    attachment -- a working-looking package whose output never appears.
    """
    if manifest.produces_artifacts:
        return
    for path, flow in sorted(flows.items()):
        if any(step.op == "artifact.emit" for step in iter_steps(flow.steps)):
            raise PackageValidationError(
                f"{path}: uses 'artifact.emit' but the manifest does not set produces_artifacts=true "
                f"with its 'artifact_flows' regenerate/reroll_gen pair"
            )


def _assert_config_view_scope(manifest: ExtensionManifest, views: Mapping[str, View]) -> None:
    """A config view may bind only the ``config`` scope.

    The manager renders this view in the package's detail panel with no slot,
    no placement, and no ``ui.contribute`` grant, because the surface is the
    manager itself. That exemption is only defensible while the view is a
    *settings* form: a config view that could bind conversation or character
    state would be a placement-free write into per-entity data, reachable from
    a panel the user opened to read a description.
    """
    binding = manifest.views.get(CONFIG_VIEW_ID)
    if binding is None:
        return
    view = views.get(binding.source)
    if view is None:
        return
    for node in iter_components(view.root):
        bind = getattr(node, "bind", None)
        if isinstance(bind, str) and not bind.startswith("config."):
            raise PackageValidationError(
                f"{binding.source}: the '{CONFIG_VIEW_ID}' view binds {bind!r}; a config view may bind only 'config.<key>'"
            )
    for name, source in sorted(view.data.items()):
        if isinstance(source, StateSource) and source.scope != "config":
            raise PackageValidationError(
                f"{binding.source}: the '{CONFIG_VIEW_ID}' view reads {source.scope!r} state through {name!r}; "
                f"a config view may read only 'config' state"
            )


def _assert_fragment_view_profiles(manifest: ExtensionManifest, views: Mapping[str, View]) -> None:
    """Keep descriptor views inside the fragment lifecycle.

    A config view edits the host-owned per-instance ``type_config`` object, not
    extension state.  A value view is display-only.  Neither surface may
    smuggle an action or resource read into a contribution that is consented as
    a fragment type rather than as a placed UI.
    """
    for descriptor in manifest.contributions.fragment_types:
        config_keys = set(parse_schema(descriptor.config_schema).schema.get("properties", {}))
        if descriptor.config_view is not None:
            view = views[descriptor.config_view]
            if view.data:
                raise PackageValidationError(f"{descriptor.config_view}: fragment config views may not declare data sources")
            if referenced_actions(view):
                raise PackageValidationError(f"{descriptor.config_view}: fragment config views may not dispatch actions")
            for node in iter_components(view.root):
                bind = getattr(node, "bind", None)
                if isinstance(bind, str) and not bind.startswith("config."):
                    raise PackageValidationError(
                        f"{descriptor.config_view}: fragment config binding {bind!r} must address 'config.<key>'"
                    )
                if isinstance(bind, str) and bind.split(".", 1)[1] not in config_keys:
                    raise PackageValidationError(
                        f"{descriptor.config_view}: fragment config binding {bind!r} names an undeclared config key"
                    )
                for path in _paths_in(node.model_dump(mode="python")):
                    segments = path.split(".")
                    if len(segments) >= 2 and segments[0] == "config" and segments[1] not in config_keys:
                        raise PackageValidationError(
                            f"{descriptor.config_view}: fragment config reference {path!r} names an undeclared config key"
                        )
        if descriptor.value_view is not None:
            view = views[descriptor.value_view]
            if view.data:
                raise PackageValidationError(f"{descriptor.value_view}: fragment value views may not declare data sources")
            if referenced_actions(view):
                raise PackageValidationError(f"{descriptor.value_view}: fragment value views may not dispatch actions")
            if any(isinstance(getattr(node, "bind", None), str) for node in iter_components(view.root)):
                raise PackageValidationError(f"{descriptor.value_view}: fragment value views are display-only")
            for node in iter_components(view.root):
                for path in _paths_in(node.model_dump(mode="python")):
                    segments = path.split(".")
                    if len(segments) >= 2 and segments[0] == "config" and segments[1] not in config_keys:
                        raise PackageValidationError(
                            f"{descriptor.value_view}: fragment config reference {path!r} names an undeclared config key"
                        )


# ── requirement derivation ──────────────────────────────────────────────────

CTX_FIELDS: frozenset[str] = parameter_values(Capability.CONTEXT_READ)
"""The ``ctx.*`` fields a grant can unlock, derived from ``context.read``.

Identity fields (``ctx.extension_id``, ``ctx.conversation.id``, ``ctx.hook``)
carry no grant and are absent here: they tell a flow who and where it is, which
it necessarily already knows.

Deriving this from the capability's parameter values rather than listing it is
what keeps the projection and the vocabulary in step. A field added to one and
forgotten in the other used to fail in whichever direction the omission ran --
an unreachable grant, or a projected field no consent line covered."""


def _derive_requirements(
    manifest: ExtensionManifest,
    flows: Mapping[str, Flow],
    views: Mapping[str, View],
) -> DerivedRequirements:
    permissions: set[Requirement] = set()
    operations: set[str] = set()
    components: set[str] = set()
    secrets: set[str] = set()

    for flow in flows.values():
        operations |= declared_operations(flow)
        for step in iter_steps(flow.steps):
            permissions |= _step_permissions(step)
            secrets |= _step_secrets(step)
            for path in _value_paths(step):
                requirement = _context_requirement(path)
                if requirement is not None:
                    permissions.add(requirement)

    placed_view_paths = {binding.source for binding in manifest.views.values()}
    fragment_view_paths = {
        path
        for descriptor in manifest.contributions.fragment_types
        for path in (descriptor.config_view, descriptor.value_view)
        if path is not None
    }
    for path, view in views.items():
        components |= used_components(view)
        # Fragment views receive their host-owned per-instance ``config`` and
        # ``data.fragment`` namespaces from the fragment lifecycle. They do
        # not read or write the extension's state slots, so ordinary view
        # permission derivation would ask for authority they never exercise.
        # A path also published as a normal view retains normal semantics.
        if path not in fragment_view_paths or path in placed_view_paths:
            permissions |= _view_source_permissions(view)
            permissions |= derive_view_runtime_requirements(view)

    # An action whose validated input declares a card identifier resolves that
    # card into ``ctx.character`` and rebinds the ``character`` state scope to
    # it. That is the only place package input selects an entity, so it is
    # gated by two grants rather than one: enumeration is not the only way to
    # reach a card, and ``context.read`` scoped to ``character`` alone must
    # never leave the current conversation.
    for binding in manifest.actions.values():
        if _declares_card_input(binding.input_schema):
            permissions.add((Capability.CONTEXT_READ.value, "character"))
            permissions.add((Capability.LIBRARY_CARDS_READ.value, None))

    # Placements and contributions are manifest-level reaches. The manifest
    # validator already proved each has its matching declaration; including
    # them here keeps the derived set complete, so the consent diff shows every
    # grant the package uses rather than only the ones a flow happens to touch.
    for placement in manifest.placements:
        permissions.add((Capability.UI_CONTRIBUTE.value, placement.slot))
        if placement.slot == LIBRARY_CARD_ACTIONS_SLOT:
            # The host resolves the clicked card into ``ctx.character``, so the
            # projection grant is required -- but not ``library.cards.read``:
            # the identifier is host-supplied from a user click, never package
            # input, so the reach is still exactly one user-chosen card.
            permissions.add((Capability.CONTEXT_READ.value, "character"))
    if manifest.contributions.fragment_types:
        permissions.add((Capability.FRAGMENT_TYPE_CONTRIBUTE.value, None))
    if manifest.produces_artifacts:
        permissions.add((Capability.ARTIFACT_WRITE.value, None))

    return DerivedRequirements(
        permissions=frozenset(permissions),
        operations=frozenset(operations),
        components=frozenset(components),
        secrets=frozenset(secrets),
    )


def _step_permissions(step: Any) -> set[Requirement]:
    """The grants one operation consumes, including its scoping parameter.

    Table-driven off ``OPERATION_SPECS``: the operation names its capability
    and says whether the parameter is pinned by the operation or chosen by the
    step, and prerequisites come from the grant table. This used to be a ladder
    of eleven ``if op ==`` branches, which meant a new operation could silently
    derive nothing -- the failure mode where a package reaches a host capability
    that never appears in its consent diff.
    """
    spec = OPERATION_SPECS.get(step.op)
    if spec is None or spec.capability is None:
        return set()

    if spec.parameter is not None:
        parameters: list[str | None] = [spec.parameter]
    elif spec.parameter_field is None:
        parameters = [None]
    else:
        raw = getattr(step, spec.parameter_field, None)
        # A URL is the one parameter the step does not carry literally: the
        # grant is over the *origin*, and a computed URL has none at compile
        # time. `_literal_origin` returns None there, which derives the
        # unparameterized requirement, and the runtime client re-checks the
        # resolved URL against the granted origins before connecting.
        if spec.capability is Capability.NETWORK_REQUEST:
            parameters = [_literal_origin(raw)]
        elif isinstance(raw, list):
            parameters = list(raw)
        else:
            parameters = [raw]

    return with_prerequisites((spec.capability.value, parameter) for parameter in parameters)


def _literal_origin(url: Any) -> str | None:
    """The origin of a literal URL, or ``None`` for a computed one.

    A computed URL cannot be pinned to an origin at compile time, so it derives
    the unparameterized requirement: the package needs *some* origin grant, and
    the runtime client checks the resolved URL against the granted set before
    connecting. Returning a guess here would be worse than returning nothing --
    it would put an origin in the consent diff that the request may not use.
    """
    if not isinstance(url, str):
        return None
    scheme, _, rest = url.partition("://")
    if scheme not in ("http", "https") or not rest:
        return None
    authority = rest.split("/", 1)[0]
    return f"{scheme}://{authority}" if authority else None


def _step_secrets(step: Any) -> set[str]:
    names: set[str] = set()
    for field in ("headers", "body"):
        if hasattr(step, field):
            names |= _secret_names(getattr(step, field))
    return names


def _secret_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"$secret"} and isinstance(value["$secret"], str):
            return {value["$secret"]}
        return {name for item in value.values() for name in _secret_names(item)}
    if isinstance(value, list):
        return {name for item in value for name in _secret_names(item)}
    return set()


def _value_paths(step: Any) -> Iterator[str]:
    """Every ``$ref`` / template path any of a step's value fields reads."""
    for field in type(step).model_fields:
        if field in ("id", "op", "on_error", "then", "otherwise"):
            continue
        yield from _paths_in(getattr(step, field))


def _paths_in(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            yield value["$ref"]
            return
        if set(value) == {"$template"} and isinstance(value["$template"], str):
            yield from template_paths(value["$template"])
            return
        for item in value.values():
            yield from _paths_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _paths_in(item)


def _context_requirement(path: str) -> Requirement | None:
    segments = path.split(".")
    if segments[0] != "ctx" or len(segments) < 2 or segments[1] not in CTX_FIELDS:
        return None
    return (Capability.CONTEXT_READ.value, segments[1])


CARD_INPUT_PROPERTY = "card_id"
"""The reserved action-input property that names a character card.

A fixed name rather than a per-package convention: the host has to recognise
"this action operates on a card the user picked" before it validates the input,
because that recognition is what decides which grants the invocation needs."""

LIBRARY_CARD_ACTIONS_SLOT = "library.card_actions"


def _declares_card_input(schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(schema, Mapping):
        return False
    properties = schema.get("properties")
    return isinstance(properties, Mapping) and CARD_INPUT_PROPERTY in properties


def _view_source_permissions(view: View) -> set[Requirement]:
    """The grants a view's declared data sources consume.

    A view inherits the requirements of every host resource it reads, which is
    what makes "a partially granted package publishes only the entry points it
    can honor" true of views and not just of flows.
    """
    permissions: set[Requirement] = set()
    for source in view.data.values():
        if isinstance(source, StateSource):
            permissions.add((Capability.STATE_READ.value, source.scope))
        else:
            permissions.add(RESOURCE_CAPABILITIES[source.resource])
            if source.resource == "conversation.tree" and source.previews:
                permissions.add((Capability.CONVERSATION_TREE_READ.value, "preview"))
    return permissions


def _view_path_requirement(path: str) -> Requirement | None:
    """A config/state path inside a view consumes the matching read grant.

    Displaying a stored value is a read even when no form control binds it, so
    a view that only *shows* its conversation state still derives the grant.
    """
    segments = path.split(".")
    if segments[0] == "config" and len(segments) >= 2:
        return (Capability.STATE_READ.value, "config")
    if segments[0] == "state" and len(segments) >= 3:
        return (Capability.STATE_READ.value, segments[1])
    return None


def _component_permissions(node: Component) -> set[Requirement]:
    if getattr(node, "component", None) == "library-sweep":
        # The sweep enumerates the library to find its cards, and resolves each
        # one into ``ctx.character`` before dispatching. Deriving both grants
        # from the *component* is what keeps a package from placing a sweep and
        # only asking for the write.
        permissions: set[Requirement] = {
            (Capability.LIBRARY_CARDS_READ.value, None),
            (Capability.CONTEXT_READ.value, "character"),
        }
        # A named bookkeeping key changes the loop from "visit every card" to
        # "skip cards this extension already completed". Without this read
        # grant the resource deliberately omits every card's state; treating
        # absence as false would silently turn a resumable paid sweep into a
        # full rewrite after permission revocation.
        if getattr(node, "unclassified_key", None) is not None:
            permissions.add((Capability.STATE_READ.value, "character"))
        return permissions

    """A bound form control reaches this extension's own config or state.

    Rendering a bound control implies reading the value; submitting implies
    writing it. Both are derived, because a view that could write a slot the
    user never granted would make "merely rendering a view never writes state"
    a promise about timing rather than about authority.
    """
    bind = getattr(node, "bind", None)
    if not isinstance(bind, str):
        return set()
    segments = bind.split(".")
    scope = "config" if segments[0] == "config" else segments[1]
    return {(Capability.STATE_READ.value, scope), (Capability.STATE_WRITE.value, scope)}


def derive_view_runtime_requirements(view: View) -> frozenset[Requirement]:
    """Grants required for the view tree itself to behave as declared.

    Host-resource sources may fail independently and render a per-source error,
    but a component whose behavior depends on a grant must not remain live
    after that grant is revoked. In particular, ``library-sweep`` without its
    character-state read would reinterpret "already done" as "not done" and
    repeat paid writes across the whole library.
    """
    permissions: set[Requirement] = set()
    for node in iter_components(view.root):
        permissions |= _component_permissions(node)
        for path in _paths_in(node.model_dump(mode="json")):
            requirement = _view_path_requirement(path)
            if requirement is not None:
                permissions.add(requirement)
    return frozenset(permissions)


# ── declaration coverage ────────────────────────────────────────────────────


def _assert_declarations_cover(manifest: ExtensionManifest, derived: DerivedRequirements) -> None:
    declared = _declared_permissions(manifest)
    for capability, parameter in sorted(derived.permissions, key=lambda r: (r[0], r[1] or "")):
        if (capability, parameter) in declared:
            continue
        detail = f"{capability!r}" if parameter is None else f"{capability!r} for {parameter!r}"
        raise PackageValidationError(
            f"package uses capability {detail} but does not request it in 'permissions'; "
            f"every derived requirement must be declared"
        )

    missing_ops = sorted(derived.operations - set(manifest.requires.operations))
    if missing_ops:
        raise PackageValidationError(f"package uses operation(s) {missing_ops} not listed in 'requires.operations'")
    missing_components = sorted(derived.components - set(manifest.requires.components))
    if missing_components:
        raise PackageValidationError(f"package uses component(s) {missing_components} not listed in 'requires.components'")

    declared_secrets = {s.name for s in manifest.secrets}
    missing_secrets = sorted(derived.secrets - declared_secrets)
    if missing_secrets:
        raise PackageValidationError(f"package references undeclared secret(s) {missing_secrets}")


def _declared_permissions(manifest: ExtensionManifest) -> set[Requirement]:
    return expand_permissions(p.model_dump(mode="json", exclude_none=True) for p in manifest.permissions)


def expand_permissions(entries: Iterable[Mapping[str, Any]]) -> set[Requirement]:
    """Expand permission entries into the requirements they satisfy.

    Every parameterized grant also satisfies the unparameterized form: asking
    for ``state.read`` on ``conversation`` covers a derived
    ``("state.read", None)``. The reverse is deliberately not true -- approving
    ``state.write`` on ``conversation`` is not approving it on ``character``.

    Takes plain dicts rather than models so the same expansion serves both
    sides of the gate: the manifest's *requests* at compile time, and the
    stored *grants* at publish time. Two expansions would eventually disagree,
    and the direction they disagreed in would decide whether an ungranted
    capability published an entry point.
    """
    expanded: set[Requirement] = set()
    for entry in entries:
        capability = entry.get("capability")
        if not isinstance(capability, str):
            continue
        expanded.add((capability, None))
        for key, value in entry.items():
            if key == "capability":
                continue
            values = value if isinstance(value, list) else [value]
            expanded.update((capability, str(item)) for item in values)
    return expanded


def grant_key(entry: Mapping[str, Any]) -> tuple:
    """A permission's identity for diffing, de-duplication, and coverage.

    The normalized *value*, never a display string: consent, revocation, the
    update diff, and the stored grant set all compare these, so a reworded
    consent line can never silently widen or narrow what was approved. Lists are
    tupled so a multi-valued parameter (``targets``) hashes.
    """
    return tuple(sorted((key, tuple(value) if isinstance(value, list) else value) for key, value in entry.items()))


def derive_flow_requirements(flow: Flow) -> set[Requirement]:
    """The requirements one flow reaches, for per-entry-point grant checks.

    A partially granted package publishes only the entry points whose own
    requirement set is satisfied, so the unit of the check is a flow, not the
    package. Same derivation the whole-package walk uses -- shared rather than
    re-implemented, because a per-entry-point check that under-derived would be
    a hole with exactly the shape of the feature it enables.
    """
    requirements: set[Requirement] = set()
    for step in iter_steps(flow.steps):
        requirements |= _step_permissions(step)
        for path in _value_paths(step):
            requirement = _context_requirement(path)
            if requirement is not None:
                requirements.add(requirement)
    return requirements


def _fingerprint(manifest: ExtensionManifest, derived: DerivedRequirements) -> str:
    """A hash of what this build compiled the revision *into*.

    The content digest identifies the bytes; this identifies the meaning Orb
    assigned them. A stored fingerprint that no longer matches a recompile is
    how a contract change between Orb versions becomes visible instead of
    silently re-deriving different requirements under the same consent.
    """
    payload = {
        "extension_api": manifest.extension_api,
        "permissions": sorted([capability, parameter or ""] for capability, parameter in derived.permissions),
        "operations": sorted(derived.operations),
        "components": sorted(derived.components),
        "secrets": sorted(derived.secrets),
        "requested": sorted(permission_key(p) for p in manifest.permissions),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _first_error(exc: Exception) -> str:
    """One sanitized reason from a Pydantic (or plain) validation failure.

    One, not all: this string reaches an install diagnostic and the manager UI,
    and enumerating every error of a hostile manifest is work proportional to
    the attacker's input. The location is included so an author can find the
    field; the offending *value* is not, so package content never round-trips
    into Orb's UI.
    """
    if isinstance(exc, ValidationError):
        reported = exc.errors()
        if reported:
            location = ".".join(str(part) for part in reported[0].get("loc", ())) or "<root>"
            return f"{location}: {reported[0].get('msg', 'invalid value')}"
    return str(exc) or type(exc).__name__
