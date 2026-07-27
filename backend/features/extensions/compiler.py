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
    EXTENSION_API_VERSION,
    Capability,
    Component,
    ExtensionManifest,
    Flow,
    OpContext,
    View,
    check_context,
    declared_operations,
    iter_components,
    iter_steps,
    permission_key,
    referenced_actions,
    referenced_assets,
    template_paths,
    used_components,
)
from .digest import PackageContent, canonical_json_bytes, content_digest
from .errors import PackageIncompatible, PackageValidationError
from .json_loader import load_json
from .limits import MAX_ASSET_BYTES, MAX_MANIFEST_BYTES, MAX_REFERENCED_BYTES_TOTAL
from .sources import MANIFEST_PATH, PackageSource

# One requirement is a capability plus the parameter that scopes it. ``None``
# means "this capability, any parameter": an ``http.request`` whose URL is not a
# literal needs *some* origin grant, and the runtime origin check does the rest.
Requirement = tuple[str, str | None]


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
        return [p.model_dump(mode="json") for p in self.manifest.permissions]

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
    asset_types = _load_assets(source, views, files)

    _assert_flow_contexts(manifest, flows)
    _assert_view_actions(manifest, views)
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
    views: Mapping[str, View],
    files: dict[str, PackageContent],
) -> dict[str, str]:
    """Read every asset a view can render, checking type and leading bytes.

    Assets are discovered from the *parsed* views, not from the manifest: only
    the compiler knows which files a component tree reaches, which is exactly
    why the digest cannot be computed before the views are parsed.
    """
    asset_types: dict[str, str] = {}
    paths = sorted({path for view in views.values() for path in referenced_assets(view)})
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
        yield manifest.artifact_flows.regenerate, OpContext.ACTION
        yield manifest.artifact_flows.reroll_gen, OpContext.ACTION
    for descriptor in manifest.contributions.fragment_types:
        yield descriptor.reduce_flow, OpContext.REDUCER


def _assert_view_actions(manifest: ExtensionManifest, views: Mapping[str, View]) -> None:
    for path, view in views.items():
        for name in sorted(referenced_actions(view)):
            if name not in manifest.actions:
                raise PackageValidationError(f"{path}: dispatches undeclared action {name!r}")


# ── requirement derivation ──────────────────────────────────────────────────

# Which capability a read of a ``ctx.*`` path consumes. Identity fields
# (``ctx.extension_id``, ``ctx.conversation.id``, ``ctx.hook``) carry no grant:
# they tell a flow who and where it is, which it necessarily already knows.
_CTX_CAPABILITIES: dict[str, Capability] = {
    "input": Capability.CONTEXT_INPUT_READ,
    "draft": Capability.CONTEXT_DRAFT_READ,
    "history": Capability.CONTEXT_HISTORY_READ,
    "character": Capability.CONTEXT_CHARACTER_READ,
}


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

    for view in views.values():
        components |= used_components(view)
        for node in iter_components(view.root):
            permissions |= _component_permissions(node)

    # Placements and contributions are manifest-level reaches. The manifest
    # validator already proved each has its matching declaration; including
    # them here keeps the derived set complete, so the consent diff shows every
    # grant the package uses rather than only the ones a flow happens to touch.
    for placement in manifest.placements:
        permissions.add((Capability.UI_CONTRIBUTE.value, placement.slot))
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
    """The grants one operation consumes, including its scoping parameter."""
    op = step.op
    if op == "state.get":
        return {(Capability.STATE_READ.value, step.scope)}
    if op in ("state.set", "state.delete"):
        # A write to a slot the flow cannot read is legal but unusual; deriving
        # only the write keeps the consent line honest about what it is.
        return {(Capability.STATE_WRITE.value, step.scope)}
    if op in ("model.text", "model.structured"):
        return {(Capability.MODEL_CALL.value, step.lane)}
    if op == "http.request":
        return {(Capability.NETWORK_REQUEST.value, _literal_origin(step.url))}
    if op == "context.append":
        return {(Capability.PROMPT_CONTEXT_APPEND.value, target) for target in step.targets}
    if op == "draft.replace":
        return {(Capability.DRAFT_REPLACE.value, None)}
    if op == "artifact.emit":
        return {(Capability.ARTIFACT_WRITE.value, None)}
    if op == "conversation.branch.activate":
        return {(Capability.CONVERSATION_BRANCH_ACTIVATE.value, None)}
    if op == "ui.invalidate":
        return {(Capability.UI_CONTRIBUTE.value, None)}
    return set()


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
    if segments[0] != "ctx" or len(segments) < 2:
        return None
    capability = _CTX_CAPABILITIES.get(segments[1])
    return (capability.value, None) if capability is not None else None


def _component_permissions(node: Component) -> set[Requirement]:
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
    return expand_permissions(p.model_dump(mode="json") for p in manifest.permissions)


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
