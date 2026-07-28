"""``orb-extension.json`` -- the installed contract, versioned by ``extension_api``.

The manifest declares identity, requirements, requested permissions, entry
points, UI placements, and domain contributions. What it does **not** do is
grant anything: the compiler derives the real requirement set by walking every
referenced flow, view, placement, asset, schema, secret use, and contribution,
and then checks that ``requires`` and ``permissions`` *cover* that derived set.
An omitted declaration is a validation error; an unknown declared requirement
makes the revision incompatible rather than invalid, because "this build is too
old" and "this package is malformed" call for different user actions.

Everything checkable from the manifest alone is checked here -- intra-manifest
references (a placement naming an undeclared view), the artifact mandate, slot
consent coverage, origin shape. Everything that needs file content is left to
the compiler, which has it.

``extension_api`` is the whole compatibility boundary. Orb exposes no product
version constant, so there is deliberately no ``minimum_orb_version`` field: a
field whose value cannot be evaluated reliably is worse than no field, because
authors would set it and believe it did something.

**Widening the version literal is not the same as widening the contract.**
:data:`SUPPORTED_EXTENSION_APIS` says which versions this build implements, and
:data:`CONTRIBUTION_MIN_API` says which version each contribution slot was
introduced in. A v1 manifest carrying a v2 contribution is *rejected* -- v1
still means exactly what it meant, and a package cannot acquire semantics its
author never declared by being parsed on a newer Orb.

The compiler reads the raw ``extension_api`` integer **before** strict parsing,
so a package from a future API is reported as incompatible ("update Orb")
rather than malformed ("this package is broken"). That ordering is the whole
reason API 2 exists as a version bump: every model here forbids extra fields,
so without it a v1-only build would call a v2 manifest broken.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, model_validator

from ....core import WriterToolError, WriterToolKey, wire_name
from ..limits import (
    MAX_ACTIONS,
    MAX_COMMANDS,
    MAX_DESCRIPTION_CHARS,
    MAX_FRAGMENT_TYPES,
    MAX_ORIGINS,
    MAX_PERMISSIONS,
    MAX_PLACEMENTS,
    MAX_SECRETS,
    MAX_VIEWS,
    MAX_WRITER_TOOL_DESCRIPTION_CHARS,
)
from .capabilities import (
    CAPABILITY_SPECS,
    OPERATION_NAMES,
    PARAMETER_NAMES,
    Capability,
    missing_prerequisites,
    parameter_name,
    parameter_values,
)
from .common import (
    ExtensionId,
    ExtModel,
    Label,
    LocalId,
    PackagePath,
    Predicate,
    validate_template,
)
from .components import COMPONENT_NAMES
from .schema_subset import PackageSchema, parse_schema

EXTENSION_API_VERSION = 1
"""The frozen v1 contract. Kept as its own name because "v1" is a thing the
architecture talks about, not merely the smallest supported number."""

UI_SLOTS: frozenset[str] = parameter_values(Capability.UI_CONTRIBUTE)
"""The named slots a package may place into.

Derived from ``ui.contribute``'s admissible parameter values rather than listed
here: a slot *is* a value of that grant, and a slot that existed without a
consent line -- or a consent line for a slot the renderer has no place for --
is exactly the drift the spec table exists to make unrepresentable.

Note ``library.card_actions`` among them. It is rendered per card in the library
browser, and the host resolves the clicked card into ``ctx.character`` and
rebinds the ``character`` state scope to it -- the same resolution an action
input's card identifier gets, minus ``library.cards.read``, because the
identifier is host-supplied from a user click and never package input. Reach
stays exactly one user-chosen card per invocation.
"""

CONFIG_VIEW_ID = "config"
"""The view id the Orb-owned manager renders in a package's detail panel.

A convention, not a slot: no placement and no ``ui.contribute`` grant is
involved, because the surface is the manager itself, which the user opens
deliberately. It closes a real gap -- extensions have config state and form
components but nowhere host-standard to surface settings -- without every
package burning its one workspace command on a settings page.

Hardening lives in the compiler: a config view may bind form controls only to
the ``config`` state scope.
"""

ICON_NAMES: frozenset[str] = frozenset(
    {
        "activity",
        "bookmark",
        "chart",
        "check",
        "clock",
        "download",
        "eye",
        "flag",
        "git-branch",
        "globe",
        "image",
        "info",
        "link",
        "list",
        "map",
        "message",
        "meter",
        "pin",
        "plug",
        "refresh",
        "search",
        "settings",
        "sparkle",
        "star",
        "tag",
        "upload",
        "wand",
        "warning",
    }
)

Slot = Annotated[str, Field(pattern=r"^[a-z][a-z_.]{0,31}$")]
Icon = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
Version = Annotated[str, Field(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}(-[0-9A-Za-z.-]{1,32})?$")]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION_CHARS)]
HomepageUrl = Annotated[str, Field(pattern=r"^https://[^\s\"'<>]{1,300}$")]


_ORIGIN_RE = re.compile(
    r"(?P<scheme>https?)://"
    r"(?:\[(?P<v6>[0-9A-Fa-f:.]{2,45})\]|(?P<host>[^/?#@:\[\]\s]+))"
    r"(?::(?P<port>\d{1,5}))?"
)
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


def _validate_origin(raw: str) -> str:
    """Validate an exact ``scheme://host[:port]`` network origin.

    No wildcard host, no wildcard port, no userinfo, no path, no query, no
    fragment, no ``file:``, no Unix socket. An origin is the unit of consent,
    so it has to be the thing the user reads: ``https://api.example.com`` is
    reviewable, ``https://*.example.com`` is not.

    IPv6 literals must be bracketed (``http://[::1]:8188``). Unbracketed is
    ambiguous with the port separator, and an origin that two parsers read
    differently is not a grant anyone can honor.
    """
    match = _ORIGIN_RE.fullmatch(raw)
    if match is None:
        raise ValueError(f"origin {raw!r} must be exactly 'scheme://host' or 'scheme://host:port' (http or https only)")
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError(f"origin {raw!r} has a port outside 1-65535")

    literal = match.group("v6")
    if literal is not None:
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            raise ValueError(f"origin {raw!r} does not contain a valid IPv6 literal") from None
        return raw

    host = match.group("host")
    if "*" in host:
        raise ValueError(f"origin {raw!r} uses a wildcard host; v1 grants are exact origins")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME_RE.fullmatch(host):
            raise ValueError(f"origin {raw!r} does not name a valid host") from None
    return raw


Origin = Annotated[str, AfterValidator(_validate_origin)]


# ── permissions ──────────────────────────────────────────────────────────────
# Each requested grant is one entry, shown to the user individually. A grant
# that carries a parameter (a lane, a scope, an origin, a slot, a field) is a
# *different* grant per parameter value: approving "state.write on conversation"
# is not approving "state.write on character".
#
# One model, not one class per parameter shape. The admissible parameter of each
# capability is a fact about that capability, and it already lives in
# CAPABILITY_SPECS -- so this model asks the spec rather than restating it in a
# discriminated union that would have to be edited in lockstep. Adding a
# capability, or a value to an existing one, is a spec entry and nothing here.


class Permission(ExtModel):
    """One requested grant: a capability and the parameter that scopes it.

    Exactly one parameter field may be set, and only the one its capability
    declares. The fields are declared rather than accepted as a free-form dict
    so each keeps its own validator -- an ``origin`` is checked by the origin
    grammar here, not by a later consumer that hopes it was checked.
    """

    capability: str
    field: str | None = None
    scope: str | None = None
    lane: str | None = None
    slot: Slot | None = None
    origin: Origin | None = None
    targets: list[str] | None = Field(default=None, min_length=1, max_length=8)

    @model_validator(mode="after")
    def _matches_its_spec(self):
        try:
            capability = Capability(self.capability)
        except ValueError:
            raise ValueError(
                f"unknown capability {self.capability!r}; expected one of {sorted(c.value for c in Capability)}"
            ) from None

        parameter = CAPABILITY_SPECS[capability].parameter
        supplied = {name for name in PARAMETER_NAMES if self._parameter(name) is not None}

        if parameter is None:
            if supplied:
                raise ValueError(f"permission {self.capability!r} takes no parameter, got {sorted(supplied)}")
            return self

        expected = parameter.name
        if supplied != {expected}:
            got = sorted(supplied) or "none"
            raise ValueError(f"permission {self.capability!r} requires the {expected!r} parameter, got {got}")

        raw = self._parameter(expected)
        values = raw if isinstance(raw, list) else [raw]
        if not parameter.multiple and isinstance(raw, list):
            raise ValueError(f"permission {self.capability!r} takes a single {expected!r}, not a list")
        if parameter.multiple and not isinstance(raw, list):
            raise ValueError(f"permission {self.capability!r} takes a list of {expected!r}")

        admissible = parameter.values
        if admissible is not None:
            for value in values:
                if value not in admissible:
                    raise ValueError(
                        f"permission {self.capability!r} does not admit {expected}={value!r}; "
                        f"expected one of {sorted(admissible)}"
                    )
        if len(set(values)) != len(values):
            raise ValueError(f"permission {self.capability!r} repeats a {expected!r} value")
        return self

    def _parameter(self, name: str):
        return getattr(self, name, None)

    def parameter_values(self) -> tuple[str, ...]:
        """Every value this entry approves, or ``()`` for an unparameterized grant."""
        capability = Capability(self.capability)
        name = parameter_name(capability)
        if name is None:
            return ()
        raw = self._parameter(name)
        return tuple(raw) if isinstance(raw, list) else ((raw,) if raw is not None else ())


def permission_key(permission: Permission) -> tuple[str, ...]:
    """A permission's identity for diffing and de-duplication.

    Consent, revocation, and the update diff all compare these tuples rather
    than display strings, so a reworded consent line can never silently widen
    or narrow what was approved.

    ``exclude_none`` is load-bearing: the model declares every parameter field
    so each can carry its own validator, and an unset one must be absent rather
    than present-and-null. A key that carried ``None`` for the six parameters a
    grant does not use would not match the same grant read back from stored
    JSON, and the mismatch would read as "this permission was never approved".
    """
    data = permission.model_dump(mode="json", exclude_none=True)
    capability = data.pop("capability")
    return (capability, *(str(data[key]) for key in sorted(data)))


# ── entry points ─────────────────────────────────────────────────────────────


class PreHookBinding(ExtModel):
    flow: PackagePath


class PostHookBinding(ExtModel):
    """A post-pipeline binding, with its stage declared rather than inferred.

    ``transform`` sees the current draft and may replace it once; ``observe``
    sees the final immutable draft. The stage is part of the manifest because
    it determines ordering against *other* extensions, and ordering that
    depended on installation time would make one user's turn differ from
    another's for the same set of packages.
    """

    flow: PackagePath
    stage: Literal["transform", "observe"]


class Hooks(ExtModel):
    pre_pipeline: PreHookBinding | None = None
    post_pipeline: PostHookBinding | None = None


class ActionBinding(ExtModel):
    """A named on-demand entry point a view's button or a command dispatches."""

    flow: PackagePath
    label: Label | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None

    @model_validator(mode="after")
    def _schemas_in_subset(self):
        for name in ("input_schema", "output_schema"):
            raw = getattr(self, name)
            if raw is not None:
                parse_schema(raw, what=name)
        return self


class ArtifactFlows(ExtModel):
    """The regenerate/reroll pair an artifact producer must supply.

    The existing ``WorkflowMandateError`` invariant, restated for packages: a
    workflow that emits artifacts and cannot regenerate them ships a UI button
    that 404s. Declaring one side without the other is rejected at install
    rather than on the first click.
    """

    regenerate: PackagePath
    reroll_gen: PackagePath
    recovery_input_schema: dict | None = None

    @model_validator(mode="after")
    def _schema_in_subset(self):
        if self.recovery_input_schema is not None:
            parse_schema(self.recovery_input_schema, what="recovery_input_schema")
        return self


class ViewBinding(ExtModel):
    source: PackagePath
    label: Label | None = None


class Placement(ExtModel):
    """Where a view or command appears. Exactly one of the two."""

    slot: Slot
    view: LocalId | None = None
    command: LocalId | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if self.slot not in UI_SLOTS:
            raise ValueError(f"unknown UI slot {self.slot!r}; expected one of {sorted(UI_SLOTS)}")
        if (self.view is None) == (self.command is None):
            raise ValueError("a placement must name exactly one of 'view' or 'command'")
        return self


class Command(ExtModel):
    """An invocable entry separated from where it is placed.

    ``opens`` names a view the host renders in a workspace; ``action`` names a
    flow the host dispatches. Availability (``when``) reads a small host state
    projection and cannot call a flow operation -- a menu that had to run
    package code to decide whether to render itself would put package code on
    every repaint.
    """

    id: LocalId
    label: Label
    icon: Icon | None = None
    opens: LocalId | None = None
    action: LocalId | None = None
    when: Predicate | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if (self.opens is None) == (self.action is None):
            raise ValueError("a command must name exactly one of 'opens' or 'action'")
        if self.icon is not None and self.icon not in ICON_NAMES:
            raise ValueError(f"unknown icon {self.icon!r}; icons are Orb-owned symbolic names, not asset paths")
        return self


class SecretDeclaration(ExtModel):
    """A named secret the user fills in through an Orb-owned, write-only form."""

    name: LocalId
    label: Label
    description: Description | None = None


# ── domain contributions ─────────────────────────────────────────────────────


class FragmentTypeDescriptor(ExtModel):
    """One extension-defined interactive-fragment type.

    A descriptor participates in a fixed lifecycle and nothing else: configure,
    contribute one property schema to the existing ``direct_scene`` tool, add
    prior state to the Director's trailing request, reduce the Director output
    to the next state, persist it on the assistant message, render it into the
    Writer's Scene Direction context, and display it. It cannot add a model
    tool, a pipeline pass, a database query, or a persistence location.

    ``prior_context`` deliberately renders into the *trailing* request rather
    than the tool schema, so the schema stays byte-stable across turns and all
    three passes keep sharing one cached tools blob.
    """

    id: LocalId
    label: Label
    description: Description | None = None
    storage: Literal["assistant_progressive"]
    config_schema: dict
    director_schema: dict
    prior_context: dict | None = None
    reduce_flow: PackagePath
    writer_context: dict | None = None
    config_view: PackagePath | None = None
    value_view: PackagePath | None = None

    @model_validator(mode="after")
    def _schemas_and_templates(self):
        config = parse_schema(self.config_schema, what=f"fragment_type {self.id!r} config_schema")
        director = parse_schema(
            self.director_schema,
            allow_config_templates=True,
            what=f"fragment_type {self.id!r} director_schema",
        )
        # A $config bound may only name a config key that exists and is an
        # integer: the substitution happens while building the per-turn tool
        # blob, where a missing or non-numeric key would produce a schema the
        # model provider rejects for the whole turn.
        properties = config.schema.get("properties", {})
        required = set(config.schema.get("required", ()))
        for key in sorted(director.config_keys):
            declared = properties.get(key)
            if declared is None:
                raise ValueError(f"fragment_type {self.id!r} director_schema references undeclared config key {key!r}")
            if declared["type"] != "integer":
                raise ValueError(f"fragment_type {self.id!r} config key {key!r} must be an integer to fill a numeric bound")
            if key not in required:
                raise ValueError(f"fragment_type {self.id!r} config key {key!r} must be required to fill a numeric bound")
        for name in ("prior_context", "writer_context"):
            template = getattr(self, name)
            if template is None:
                continue
            if set(template) != {"$template"}:
                raise ValueError(f"fragment_type {self.id!r} {name} must be a single '$template' object")
            validate_template(template["$template"], what=f"fragment_type {self.id!r} {name}")
        return self


WriterToolDescription = Annotated[str, Field(min_length=1, max_length=MAX_WRITER_TOOL_DESCRIPTION_CHARS)]

MAX_WRITER_TOOL_SCHEMA_PROPERTIES = 8
"""Top-level properties of a Writer tool's input schema.

Tighter than the general subset's 64. The tool asks the Writer to describe one
situation, and a form with fifty fields is a sign the package is trying to
extract the draft, the conversation, or the entity graph through arguments the
host would otherwise have to supply."""

MAX_WRITER_TOOL_SCHEMA_DEPTH = 4
"""Nesting of either Writer-tool schema. Deep argument trees cost prompt bytes
on every turn the tool is active and buy nothing a flat request cannot say."""

RESERVED_WRITER_TOOL_INPUT_PROPERTIES: frozenset[str] = frozenset(
    {
        "draft",
        "conversation_id",
        "conversation",
        "character_id",
        "card_id",
        "message_id",
        "extension_id",
        "turn_id",
        "history",
        "persona",
    }
)
"""Input property names a Writer tool may not declare.

The host supplies draft and entity identity; a model argument must never be
able to redirect the invocation to another conversation, card, message,
package, or flow. Refusing the *names* at parse time is what makes that
statement checkable by reading the manifest, rather than a promise about how
carefully the adapter ignores unexpected keys. ``draft`` is listed for the
narrower reason that a package must not be able to make the model re-emit the
prose Orb already has -- that would double the turn's cost and let a truncated
echo become the resolver's view of the scene."""


class WriterToolDescriptor(ExtModel):
    """The one Writer tool an API 2 package may contribute.

    ``description`` is required and bounded, because it is *model input*: it
    ships in the Writer's tool blob every turn the tool is active and can steer
    generation even when no call ever happens. That is why the grant behind this
    contribution is conspicuous, and why the copy the user consents to has to
    mention influence rather than only invocation.

    There is no provider-facing name field. Orb derives one from the extension
    id and this ``id`` (see :func:`~backend.core.writer_tools.wire_name`), so a
    package cannot claim a built-in tool's name, shadow another package's tool,
    or ship a string that reaches a provider as something other than an
    identifier.
    """

    id: LocalId
    label: Label
    description: WriterToolDescription
    flow: PackagePath
    input_schema: dict
    output_schema: dict

    @model_validator(mode="after")
    def _schemas_are_bounded_objects(self):
        for name in ("input_schema", "output_schema"):
            what = f"writer_tool {self.id!r} {name}"
            parsed = parse_schema(getattr(self, name), what=what)
            if parsed.schema.get("type") != "object":
                raise ValueError(f"{what} must declare an object")
            _assert_writer_schema_bounds(parsed, what=what)
        properties = parse_schema(self.input_schema, what="input_schema").schema.get("properties", {})
        if len(properties) > MAX_WRITER_TOOL_SCHEMA_PROPERTIES:
            raise ValueError(
                f"writer_tool {self.id!r} input_schema declares {len(properties)} properties, "
                f"limit is {MAX_WRITER_TOOL_SCHEMA_PROPERTIES}"
            )
        reserved = sorted(set(properties) & RESERVED_WRITER_TOOL_INPUT_PROPERTIES)
        if reserved:
            raise ValueError(
                f"writer_tool {self.id!r} input_schema declares host-supplied propert(ies) {reserved}; "
                f"the draft and every entity identity come from Orb, never from the model's arguments"
            )
        return self


def _assert_writer_schema_bounds(parsed: PackageSchema, *, what: str, depth: int = 0) -> None:
    """Walk a normalized Writer-tool schema for depth and description bounds.

    Descriptions are checked at every level, not only at the root: a package
    that split 20 KiB of instructions across forty property descriptions would
    otherwise put exactly as much prompt text in front of the model as one long
    tool description it is not allowed to write.
    """
    _walk_writer_schema(parsed.schema, what=what, depth=depth)


def _walk_writer_schema(schema: Mapping[str, Any], *, what: str, depth: int) -> None:
    if depth > MAX_WRITER_TOOL_SCHEMA_DEPTH:
        raise ValueError(f"{what} nests deeper than {MAX_WRITER_TOOL_SCHEMA_DEPTH}")
    description = schema.get("description")
    if isinstance(description, str) and len(description) > MAX_WRITER_TOOL_DESCRIPTION_CHARS:
        raise ValueError(f"{what} has a {len(description)}-character description, limit is {MAX_WRITER_TOOL_DESCRIPTION_CHARS}")
    for sub in (schema.get("properties") or {}).values():
        _walk_writer_schema(sub, what=what, depth=depth + 1)
    items = schema.get("items")
    if isinstance(items, Mapping):
        _walk_writer_schema(items, what=what, depth=depth + 1)


class Contributions(ExtModel):
    """Every domain contribution slot, across every supported API version.

    One model rather than one per version, with :data:`CONTRIBUTION_MIN_API`
    saying when each slot became legal. The alternative -- a subclass per
    version overriding the field -- makes "which version am I" a type question
    that every consumer of a manifest has to answer, and buys nothing: the
    rejection a v1 package needs is "``writer_tool`` requires extension_api 2",
    which is a better error than "extra fields not permitted" and is the same
    refusal.

    ``writer_tool`` holds one tool, not a list. The user selects a single active
    Writer resolver, so a package offering three would ship two schemas that can
    never be sent; one per package keeps "availability is not activation" a
    statement about packages rather than about a package's internal menu.
    """

    fragment_types: list[FragmentTypeDescriptor] = Field(default_factory=list, max_length=MAX_FRAGMENT_TYPES)
    writer_tool: WriterToolDescriptor | None = None


class Requires(ExtModel):
    """Precise feature detection, checked against this build's capabilities.

    An unknown entry leaves the extension installed but *unavailable* with a
    diagnostic -- the package is fine, this Orb is too old. That is a different
    outcome from an invalid package, and the user's next step differs too
    (update Orb, versus report a bug to the author).
    """

    operations: list[str] = Field(default_factory=list, max_length=64)
    components: list[str] = Field(default_factory=list, max_length=64)

    def unknown(self) -> list[str]:
        return sorted(
            [f"operation {name!r}" for name in self.operations if name not in OPERATION_NAMES]
            + [f"component {name!r}" for name in self.components if name not in COMPONENT_NAMES]
        )


class ExtensionManifest(ExtModel):
    """The complete manifest, for any supported API version.

    ``extension_api`` is a plain integer bounded by
    :data:`SUPPORTED_EXTENSION_APIS` rather than a per-version literal, and
    :func:`_assert_version_contract` is what keeps each version's *meaning*
    frozen: a slot introduced in a later API is refused on an earlier one.
    """

    extension_api: int
    id: ExtensionId
    name: Label
    version: Version
    author: Label | None = None
    description: Description | None = None
    homepage: HomepageUrl | None = None
    schema_url: str | None = Field(default=None, alias="$schema")

    requires: Requires = Field(default_factory=Requires)
    permissions: list[Permission] = Field(default_factory=list, max_length=MAX_PERMISSIONS)
    secrets: list[SecretDeclaration] = Field(default_factory=list, max_length=MAX_SECRETS)

    hooks: Hooks = Field(default_factory=Hooks)
    actions: dict[LocalId, ActionBinding] = Field(default_factory=dict, max_length=MAX_ACTIONS)
    views: dict[LocalId, ViewBinding] = Field(default_factory=dict, max_length=MAX_VIEWS)
    placements: list[Placement] = Field(default_factory=list, max_length=MAX_PLACEMENTS)
    commands: list[Command] = Field(default_factory=list, max_length=MAX_COMMANDS)

    produces_artifacts: bool = False
    artifact_flows: ArtifactFlows | None = None
    contributions: Contributions = Field(default_factory=Contributions)

    @model_validator(mode="after")
    def _intra_manifest_references(self):
        _assert_version_contract(self)
        _assert_unique_permissions(self.permissions)
        _assert_capability_prerequisites(self)
        _assert_origin_budget(self.permissions)
        _assert_unique_secrets(self.secrets)
        _assert_unique_commands(self.commands)
        _assert_command_targets(self)
        _assert_placement_targets(self)
        _assert_slot_consent(self)
        _assert_artifact_mandate(self)
        _assert_fragment_type_consent(self)
        _assert_writer_tool_consent(self)
        return self

    # ── derived views the compiler and installer both read ───────────────────

    @property
    def writer_tool(self) -> WriterToolDescriptor | None:
        """The declared Writer tool, or ``None``.

        An accessor rather than four reaches through ``contributions``: the
        compiler, the runtime, the catalog, and the tests all ask the same
        question, and a v1 manifest answers ``None`` because
        :func:`_assert_version_contract` refused to let it answer anything else.
        """
        return self.contributions.writer_tool

    def writer_tool_wire_name(self) -> str | None:
        """The derived provider-facing name of this manifest's Writer tool."""
        declared = self.writer_tool
        if declared is None:
            return None
        return wire_name(WriterToolKey(owner_id=self.id, local_id=declared.id))

    def granted(self) -> set[tuple[str, ...]]:
        return {permission_key(p) for p in self.permissions}

    def capabilities(self) -> set[Capability]:
        return {Capability(p.capability) for p in self.permissions}

    def requested_grants(self) -> set[tuple[str, str | None]]:
        """Every ``(capability, parameter)`` grant the manifest asks for.

        The unit the prerequisite check and the consent diff both work in: a
        capability alone cannot answer "was the ``tags`` field requested", and
        every parameterized grant in this vocabulary has a rule that turns on
        the parameter.
        """
        grants: set[tuple[str, str | None]] = set()
        for permission in self.permissions:
            values = permission.parameter_values()
            if values:
                grants.update((permission.capability, value) for value in values)
            else:
                grants.add((permission.capability, None))
        return grants

    def origins(self) -> list[str]:
        return sorted({p.origin for p in self.permissions if p.origin is not None})

    def referenced_flow_paths(self) -> set[str]:
        paths: set[str] = set()
        if self.hooks.pre_pipeline:
            paths.add(self.hooks.pre_pipeline.flow)
        if self.hooks.post_pipeline:
            paths.add(self.hooks.post_pipeline.flow)
        paths.update(binding.flow for binding in self.actions.values())
        if self.artifact_flows is not None:
            paths.update({self.artifact_flows.regenerate, self.artifact_flows.reroll_gen})
        paths.update(descriptor.reduce_flow for descriptor in self.contributions.fragment_types)
        if self.writer_tool is not None:
            paths.add(self.writer_tool.flow)
        return paths

    def referenced_view_paths(self) -> set[str]:
        paths = {binding.source for binding in self.views.values()}
        for descriptor in self.contributions.fragment_types:
            paths.update(p for p in (descriptor.config_view, descriptor.value_view) if p is not None)
        return paths

    def referenced_paths(self) -> set[str]:
        """Every package file the manifest reaches, excluding view-nested assets.

        Assets referenced *inside* a view are added by the compiler once it has
        parsed that view; the manifest alone cannot see them.
        """
        return self.referenced_flow_paths() | self.referenced_view_paths()


SUPPORTED_EXTENSION_APIS: frozenset[int] = frozenset({1, 2})
"""Every API version this build implements.

The compiler checks the raw ``extension_api`` integer against this set *before*
strict parsing, so an unknown version is
:class:`~..errors.PackageIncompatible` ("update Orb") rather than
:class:`~..errors.PackageValidationError` ("this package is broken")."""

MAX_EXTENSION_API_VERSION = max(SUPPORTED_EXTENSION_APIS)

CONTRIBUTION_MIN_API: Mapping[str, int] = {"writer_tool": 2}
"""The API version each ``contributions`` slot became legal in.

This is what "v1 still means v1" reduces to. One model carries every slot, and
a package may only use the ones its declared version introduced -- so a v1
manifest cannot acquire a Writer tool by being parsed on a build that
understands them. Adding an API 3 slot is one entry here, not a new model
class every consumer of a manifest would have to discriminate on."""


def _assert_version_contract(manifest: ExtensionManifest) -> None:
    """Refuse a contribution the manifest's declared API version predates.

    The version is checked here too, not only in the compiler: the compiler's
    raw-integer check exists to distinguish incompatible from invalid *before*
    parsing, and a model that would validate a version this build cannot honor
    would leave that distinction depending on the caller.
    """
    if manifest.extension_api not in SUPPORTED_EXTENSION_APIS:
        raise ValueError(
            f"extension_api {manifest.extension_api} is not implemented by this Orb build "
            f"(supported: {sorted(SUPPORTED_EXTENSION_APIS)})"
        )
    for slot, minimum in CONTRIBUTION_MIN_API.items():
        if getattr(manifest.contributions, slot, None) is not None and manifest.extension_api < minimum:
            raise ValueError(
                f"contributions.{slot!r} requires extension_api {minimum}; this manifest declares {manifest.extension_api}"
            )


def _assert_unique_permissions(permissions: list) -> None:
    seen: set[tuple[str, ...]] = set()
    for permission in permissions:
        key = permission_key(permission)
        if key in seen:
            raise ValueError(f"duplicate permission request {key[0]!r}")
        seen.add(key)


def _assert_capability_prerequisites(manifest: ExtensionManifest) -> None:
    for grant, prerequisite in missing_prerequisites(manifest.requested_grants()):
        raise ValueError(f"permission {_grant_label(grant)} also requires {_grant_label(prerequisite)}")


def _grant_label(grant: tuple[str, str | None]) -> str:
    capability, parameter = grant
    if parameter is None:
        return repr(capability)
    return f"{capability!r} for {parameter!r}"


def _assert_origin_budget(permissions: list) -> None:
    origins = [p for p in permissions if p.origin is not None]
    if len(origins) > MAX_ORIGINS:
        raise ValueError(f"{len(origins)} network origins exceeds the limit of {MAX_ORIGINS}")


def _assert_unique_secrets(secrets: list) -> None:
    names = [s.name for s in secrets]
    if len(set(names)) != len(names):
        raise ValueError("duplicate secret name")


def _assert_unique_commands(commands: list) -> None:
    ids = [c.id for c in commands]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate command id")


def _assert_command_targets(manifest: ExtensionManifest) -> None:
    for command in manifest.commands:
        if command.opens is not None and command.opens not in manifest.views:
            raise ValueError(f"command {command.id!r} opens undeclared view {command.opens!r}")
        if command.action is not None and command.action not in manifest.actions:
            raise ValueError(f"command {command.id!r} dispatches undeclared action {command.action!r}")


def _assert_placement_targets(manifest: ExtensionManifest) -> None:
    for placement in manifest.placements:
        if placement.view is not None and placement.view not in manifest.views:
            raise ValueError(f"placement in slot {placement.slot!r} names undeclared view {placement.view!r}")
        if placement.command is not None and placement.command not in {c.id for c in manifest.commands}:
            raise ValueError(f"placement in slot {placement.slot!r} names undeclared command {placement.command!r}")


def _assert_slot_consent(manifest: ExtensionManifest) -> None:
    """Every occupied slot must have its own ``ui.contribute`` request.

    Consent is per slot, so a package that asked to contribute to ``inspector``
    cannot also appear in ``composer.menu`` on the strength of that approval.
    """
    granted = {p.slot for p in manifest.permissions if p.capability == Capability.UI_CONTRIBUTE and p.slot}
    for placement in manifest.placements:
        if placement.slot not in granted:
            raise ValueError(
                f"placement uses slot {placement.slot!r} without requesting "
                f"the matching 'ui.contribute' permission for that slot"
            )


def _assert_artifact_mandate(manifest: ExtensionManifest) -> None:
    if manifest.produces_artifacts:
        if manifest.artifact_flows is None:
            raise ValueError("produces_artifacts=true requires 'artifact_flows' with both regenerate and reroll_gen")
        if Capability.ARTIFACT_WRITE not in manifest.capabilities():
            raise ValueError("produces_artifacts=true requires the 'artifact.write' permission")
    elif manifest.artifact_flows is not None:
        raise ValueError("'artifact_flows' is only meaningful with produces_artifacts=true")


def _assert_writer_tool_consent(manifest: ExtensionManifest) -> None:
    """A Writer tool needs its own grant, and a name a provider will accept.

    The name check runs here rather than at publish time because it depends on
    both ids together: either alone satisfies the id grammar, and only their
    combination can overflow the strictest provider's length limit. Failing at
    parse time means the author sees it, instead of a user seeing a
    contribution that installs and then never appears in a turn.
    """
    declared = manifest.writer_tool
    if declared is None:
        return
    if Capability.WRITER_TOOL_CONTRIBUTE not in manifest.capabilities():
        raise ValueError("contributing a Writer tool requires the 'writer.tool.contribute' permission")
    try:
        wire_name(WriterToolKey(owner_id=manifest.id, local_id=declared.id))
    except WriterToolError as exc:
        raise ValueError(str(exc)) from None


def _assert_fragment_type_consent(manifest: ExtensionManifest) -> None:
    types = manifest.contributions.fragment_types
    if not types:
        return
    if Capability.FRAGMENT_TYPE_CONTRIBUTE not in manifest.capabilities():
        raise ValueError("contributing fragment types requires the 'fragment_type.contribute' permission")
    ids = [descriptor.id for descriptor in types]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate fragment type id")


def namespaced_fragment_type(extension_id: str, local_id: str) -> str:
    """The stored ``field_type`` for an extension-contributed fragment type.

    Namespacing is what lets an uninstalled provider's fragments stay stored
    and inert instead of being coerced into a core type -- the ``:`` says the
    value belongs to someone, and to whom.
    """
    return f"{extension_id}:{local_id}"


_NAMESPACED_RE = re.compile(r"^([a-z0-9][a-z0-9_-]{0,63}):([a-z0-9][a-z0-9_-]{0,63})$")


def split_fragment_type(field_type: str) -> tuple[str, str] | None:
    """Split a namespaced fragment type, or ``None`` for a core type.

    A core type (``string``, ``array``, ``progressive``, ``feedback``,
    ``direction_note``) has no colon and returns ``None``. A malformed
    namespaced value also returns ``None``, which keeps the existing
    fallback-to-``string`` behavior for legacy card values while namespaced
    unknowns stay preserved.
    """
    match = _NAMESPACED_RE.match(field_type)
    return (match.group(1), match.group(2)) if match else None
