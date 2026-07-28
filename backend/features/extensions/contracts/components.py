"""The host-rendered component tree a package may declare.

Community UI is a validated tree that Orb renders with DOM creation and
``textContent``. No component accepts raw HTML, raw CSS, JavaScript, SVG,
iframe content, event-handler source, or an unrestricted URL -- there is no
field in this module whose value becomes markup, a selector, a module path, or
a callback name.

Two consequences of that shape are worth stating explicitly, because they are
what make the renderer safe rather than merely careful:

* **Styling is tokenized.** ``tone``, ``size``, ``density``, ``columns``,
  ``align``, and ``span`` are closed enumerations mapped to host classes. An
  unknown property is a validation error, so it can never survive as a DOM
  attribute -- which is the mechanism by which ``onerror`` or ``style`` would
  otherwise arrive.
* **Media is by reference, never by URL.** An image names a validated package
  asset or a workflow artifact; the host resolves both to its own routes. A
  package cannot point the browser at an arbitrary origin, so rendering a view
  is not a network beacon.

Buttons dispatch a *named action* declared in the same manifest. They do not
provide a URL, an HTTP method, a refetch callback, or a DOM target; the action
result travels back through the fixed effect envelope in :mod:`.effects`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args

from pydantic import AfterValidator, Field, model_validator

from ..limits import MAX_COMPONENT_DEPTH, MAX_COMPONENT_NODES, MAX_VIEW_DATA_SOURCES
from .capabilities import RESOURCE_CAPABILITIES
from .common import ExtModel, Label, LocalId, PackagePath, Predicate, Value

Tone = Literal["default", "muted", "accent", "success", "warning", "danger"]
Size = Literal["sm", "md", "lg"]
Density = Literal["compact", "comfortable"]
Align = Literal["start", "center", "end"]

_BIND_ROOTS = frozenset({"config", "state"})
_BIND_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATE_SCOPES = frozenset({"config", "conversation", "message", "character"})


def _validate_bind(path: Any) -> str:
    """Validate a form/binding path into the extension's own config or state.

    Bindings are the only writable surface a view has, and they address one
    extension's own slot: ``config.<key>`` or ``state.<scope>.<key>``. There is
    no binding that reaches another extension, a core table, or a settings
    column, so "render a view" and "write somewhere interesting" are not
    connected operations.
    """
    if not isinstance(path, str):
        raise ValueError(f"binding must be a string, got {type(path).__name__}")
    segments = path.split(".")
    if segments[0] not in _BIND_ROOTS:
        raise ValueError(f"binding {path!r} must start with 'config' or 'state'")
    if segments[0] == "config":
        if len(segments) != 2:
            raise ValueError(f"binding {path!r} must be 'config.<key>'")
    else:
        if len(segments) != 3 or segments[1] not in _STATE_SCOPES:
            raise ValueError(f"binding {path!r} must be 'state.<{'|'.join(sorted(_STATE_SCOPES))}>.<key>'")
    for segment in segments[1:]:
        if not _BIND_SEGMENT_RE.match(segment):
            raise ValueError(f"binding {path!r} has an invalid segment {segment!r}")
    return path


BindPath = Annotated[str, AfterValidator(_validate_bind)]


class _Node(ExtModel):
    """Fields every component shares.

    ``when`` reads the same predicate AST flows use, evaluated against the
    view's own data projection -- it selects between already-declared subtrees
    and cannot call an operation.
    """

    when: Predicate | None = None
    span: Annotated[int, Field(ge=1, le=6)] | None = None


# ── layout ───────────────────────────────────────────────────────────────────


class StackComponent(_Node):
    component: Literal["stack"]
    direction: Literal["vertical", "horizontal"] = "vertical"
    gap: Size = "md"
    align: Align = "start"
    children: list[Component] = Field(default_factory=list, max_length=MAX_COMPONENT_NODES)


class GridComponent(_Node):
    component: Literal["grid"]
    columns: Annotated[int, Field(ge=1, le=6)] = 2
    density: Density = "comfortable"
    children: list[Component] = Field(default_factory=list, max_length=MAX_COMPONENT_NODES)


class CardComponent(_Node):
    component: Literal["card"]
    title: Label | None = None
    tone: Tone = "default"
    children: list[Component] = Field(default_factory=list, max_length=MAX_COMPONENT_NODES)


class DividerComponent(_Node):
    component: Literal["divider"]


class TabItem(ExtModel):
    id: LocalId
    label: Label
    children: list[Component] = Field(default_factory=list, max_length=MAX_COMPONENT_NODES)


class TabsComponent(_Node):
    component: Literal["tabs"]
    tabs: list[TabItem] = Field(min_length=1, max_length=12)


# ── content ──────────────────────────────────────────────────────────────────


class TextComponent(_Node):
    component: Literal["text"]
    value: Value
    tone: Tone = "default"
    size: Size = "md"


class MarkdownComponent(_Node):
    component: Literal["markdown"]
    value: Value


class BadgeComponent(_Node):
    component: Literal["badge"]
    value: Value
    tone: Tone = "default"


class ListComponent(_Node):
    component: Literal["list"]
    items: Value
    empty_label: Label | None = None


class TableColumn(ExtModel):
    key: LocalId
    label: Label
    align: Align = "start"


class TableComponent(_Node):
    component: Literal["table"]
    columns: list[TableColumn] = Field(min_length=1, max_length=12)
    rows: Value
    density: Density = "comfortable"
    empty_label: Label | None = None


# ── inputs ───────────────────────────────────────────────────────────────────


class TextInputComponent(_Node):
    component: Literal["text-input"]
    bind: BindPath
    label: Label
    placeholder: Label | None = None


class TextareaComponent(_Node):
    component: Literal["textarea"]
    bind: BindPath
    label: Label
    rows: Annotated[int, Field(ge=2, le=24)] = 4
    # A list-shaped setting -- a vocabulary, an allow list -- is edited as one
    # line per member and stored as an array, so the flow reading it gets the
    # array the list operations require. Closed enumeration, host-implemented:
    # the package declares which shape it wants, never how the split happens.
    value_kind: Literal["text", "lines"] = "text"


class NumberInputComponent(_Node):
    component: Literal["number-input"]
    bind: BindPath
    label: Label
    minimum: int | None = None
    maximum: int | None = None


class SelectOption(ExtModel):
    value: str | int | bool
    label: Label


class SelectComponent(_Node):
    component: Literal["select"]
    bind: BindPath
    label: Label
    options: list[SelectOption] = Field(min_length=1, max_length=64)


class ToggleComponent(_Node):
    component: Literal["toggle"]
    bind: BindPath
    label: Label


class ButtonComponent(_Node):
    component: Literal["button"]
    label: Label
    action: LocalId
    input: Value | None = None
    tone: Tone = "default"


# ── status ───────────────────────────────────────────────────────────────────


class ProgressComponent(_Node):
    component: Literal["progress"]
    value: Value
    maximum: Value | None = None
    label: Label | None = None


class MeterComponent(_Node):
    component: Literal["meter"]
    value: Value
    minimum: Value
    maximum: Value
    label: Label | None = None
    tone: Tone = "default"


class EmptyStateComponent(_Node):
    component: Literal["empty-state"]
    title: Label
    description: Label | None = None


class ErrorComponent(_Node):
    component: Literal["error"]
    title: Label
    description: Label | None = None


# ── media ────────────────────────────────────────────────────────────────────


class AssetSource(ExtModel):
    """A file inside this package's validated, digest-owned content."""

    kind: Literal["asset"]
    path: PackagePath


class ArtifactSource(ExtModel):
    """A workflow attachment this extension produced, addressed by id."""

    kind: Literal["artifact"]
    attachment_id: Value


MediaSource = Annotated[AssetSource | ArtifactSource, Field(discriminator="kind")]


class ImageComponent(_Node):
    component: Literal["image"]
    source: MediaSource
    alt: Label
    size: Size = "md"


class AudioComponent(_Node):
    component: Literal["audio"]
    source: MediaSource
    label: Label | None = None


class VideoComponent(_Node):
    component: Literal["video"]
    source: MediaSource
    label: Label | None = None


# ── structured views ─────────────────────────────────────────────────────────


class LibrarySweepComponent(_Node):
    """A host-driven loop: one invocation of ``action`` per card in the library.

    The loop lives here, in the host, rather than in the flow language -- which
    is the whole reason a library-wide feature fits inside a 128-step, 2-model-
    call per-invocation budget. Each dispatch classifies one card and commits
    independently, so a sweep interrupted at card 87 leaves 86 written and 214
    untouched, and the next run resumes from the remainder. There is no partial
    commit to reconcile because there was never one transaction.

    The package supplies an action id and a label. It does not supply the page
    size, the cursor, the concurrency, the stop condition, or the progress
    display: those are the parts that make an O(n) loop safe, and a package that
    could choose them could choose badly.
    """

    component: Literal["library-sweep"]
    action: LocalId
    label: Label
    unclassified_key: LocalId | None = None
    """When set, a card whose own state slot has this key truthy is skipped.

    How "cards not yet classified" is computed without an invocation per card:
    the library resource already projects the extension's own slot, so the host
    filters against it before dispatching anything.
    """


class TreeComponent(_Node):
    component: Literal["tree"]
    nodes: Value
    select_action: LocalId | None = None
    empty_label: Label | None = None


class ConversationTreeComponent(_Node):
    """The higher-level branch map: the host computes layout and connectors.

    The package supplies nodes, the active path, and a named select action; it
    does not draw, position, or choose the fields. That is what keeps every
    package's tree the same widget rather than 20 partial reimplementations
    with 20 different escaping bugs.
    """

    component: Literal["conversation-tree"]
    nodes: Value
    active_path: Value
    select_action: LocalId
    show_previews: bool = False
    empty_label: Label | None = None


Component = Annotated[
    StackComponent
    | GridComponent
    | CardComponent
    | DividerComponent
    | TabsComponent
    | TextComponent
    | MarkdownComponent
    | BadgeComponent
    | ListComponent
    | TableComponent
    | TextInputComponent
    | TextareaComponent
    | NumberInputComponent
    | SelectComponent
    | ToggleComponent
    | ButtonComponent
    | ProgressComponent
    | MeterComponent
    | EmptyStateComponent
    | ErrorComponent
    | ImageComponent
    | AudioComponent
    | VideoComponent
    | LibrarySweepComponent
    | TreeComponent
    | ConversationTreeComponent,
    Field(discriminator="component"),
]

_COMPONENT_MODELS = get_args(get_args(Component)[0])
COMPONENT_NAMES: frozenset[str] = frozenset(
    get_args(model.model_fields["component"].annotation)[0] for model in _COMPONENT_MODELS
)
"""Every component name this build renders -- the answer to a package's
``requires.components`` feature check, derived from the union rather than
maintained beside it."""

StackComponent.model_rebuild()
GridComponent.model_rebuild()
CardComponent.model_rebuild()
TabItem.model_rebuild()
TabsComponent.model_rebuild()


# ── view data sources ────────────────────────────────────────────────────────
# A view names *what* it reads, never *how*. There is no URL, no query, no
# method, and no field selection: the host owns the projection, the bounds, and
# the grant check, and the package gets whatever that projection contains under
# a name of its choosing in the ``data`` namespace.


class ResourceSource_(ExtModel):
    """A named host resource, served as a bounded, allowlisted projection.

    The admissible names come from ``RESOURCE_CAPABILITIES``, which is itself
    derived from the capability table -- so a resource cannot be nameable here
    without a grant that gates it.
    """

    kind: Literal["resource"]
    resource: str
    previews: bool = False
    """Only meaningful for ``conversation.tree``. Requests inactive-branch text
    previews, which are a separate conspicuous grant -- the server still derives
    inclusion from the live grant, so asking without it yields no previews
    rather than an error."""

    @model_validator(mode="after")
    def _known_resource(self):
        if self.resource not in RESOURCE_CAPABILITIES:
            raise ValueError(f"unknown host resource {self.resource!r}; expected one of {sorted(RESOURCE_CAPABILITIES)}")
        return self


class StateSource(ExtModel):
    """This extension's own namespaced slot in one scope."""

    kind: Literal["state"]
    scope: Literal["config", "conversation", "message", "character"]


ViewSource = Annotated[ResourceSource_ | StateSource, Field(discriminator="kind")]


class View(ExtModel):
    """One declarative view: a component tree plus the data it reads."""

    view_version: Literal[1]
    data: dict[LocalId, ViewSource] = Field(default_factory=dict, max_length=MAX_VIEW_DATA_SOURCES)
    root: Component

    def node_count(self) -> int:
        return sum(1 for _ in iter_components(self.root))


def iter_components(node: Any, depth: int = 1):
    """Yield every component in a tree, depth-first, enforcing the depth bound."""
    if depth > MAX_COMPONENT_DEPTH:
        raise ValueError(f"component tree nests deeper than {MAX_COMPONENT_DEPTH}")
    yield node
    for child in getattr(node, "children", ()) or ():
        yield from iter_components(child, depth + 1)
    for tab in getattr(node, "tabs", ()) or ():
        for child in tab.children:
            yield from iter_components(child, depth + 1)


def used_components(view: View) -> set[str]:
    """The component names a view uses, for the ``requires.components`` check."""
    return {node.component for node in iter_components(view.root)}


def referenced_actions(view: View) -> set[str]:
    """Every action a view can dispatch, from buttons and tree selection."""
    names: set[str] = set()
    for node in iter_components(view.root):
        action = getattr(node, "action", None) or getattr(node, "select_action", None)
        if isinstance(action, str):
            names.add(action)
    return names


def referenced_assets(view: View) -> set[str]:
    """Every package asset path a view can render."""
    paths: set[str] = set()
    for node in iter_components(view.root):
        source = getattr(node, "source", None)
        if isinstance(source, AssetSource):
            paths.add(source.path)
    return paths
