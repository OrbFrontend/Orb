"""The v1 permission vocabulary and the operation table it gates.

Two frozen tables live here:

* :data:`CAPABILITY_SPECS` -- every grant a package may request, with the
  parameter that scopes it, the consent copy the user reads, the class of data
  it exposes, and its prerequisites. Consent is shown per *grant* -- a
  ``(capability, parameter)`` pair -- so this table is also the UI's list of
  things a user is agreeing to.
* :data:`OPERATION_SPECS` -- every flow operation, the capability it requires,
  the hook contexts it may appear in, and the quota counter it charges.

The compiler derives a package's requirement set from ``OPERATION_SPECS`` by
walking the flows, and then checks that the manifest's declared ``permissions``
covers it. The manifest's claims never *grant* anything; they are checked
against what the code actually reaches. This is why an operation hidden behind
``when: false`` still appears in the consent diff -- validation is conservative
over all reachable branches, because a predicate's value is not knowable at
install time.

Everything the rest of the codebase needs to know about a grant is derived from
:data:`CAPABILITY_SPECS`: the Pydantic permission model's admissible parameters
(``manifest.py``), the resource-to-grant map (``components.py``), the consent
copy and emphasis and the combination banner's read set (``catalog.py``). Those
were once six independent tables, and nothing linked them -- a capability added
without its consent line degraded to a bare identifier in a dialog, and one
added without joining the read set silently disabled the combination banner.
Both failures were invisible until a user hit them. A required dataclass field
is a cheaper guarantee than a convention, so the facts live together and the
tables are computed.

**The parameter is part of the grant, not a detail of it.** ``state.write`` on
``conversation`` and ``state.write`` on ``character`` are two grants, and the
vocabulary says so with one capability and a scope rather than two capabilities.
That is why there is no ``context.draft.read`` here: reading the draft is
``context.read`` scoped to ``draft``, exactly as writing conversation state is
``state.write`` scoped to ``conversation``. Encoding a scope in the capability
*name* makes every new field a new enum entry, a new consent line, a new
membership in every classification set, and a new branch in the projection --
which is how the first vocabulary reached twenty-one entries for what is
structurally about a dozen decisions.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ....core import MAX_WRITER_TOOL_CALLS_PER_TURN

Requirement = tuple[str, str | None]
"""One grant as the runtime checks it: a capability and its scoping parameter.

``None`` is the unparameterized form. A parameterized approval also satisfies
it -- see ``expand_permissions`` -- so a derivation that cannot pin a parameter
at compile time (a computed HTTP origin) still gates on *some* approval of that
capability, and the runtime re-checks the resolved value."""


class Capability(StrEnum):
    """A grant the user approves at install or update time."""

    CONTEXT_READ = "context.read"
    CONVERSATION_TREE_READ = "conversation.tree.read"
    CONVERSATION_BRANCH_ACTIVATE = "conversation.branch.activate"
    LIBRARY_CARDS_READ = "library.cards.read"
    LOREBOOK_READ = "lorebook.read"
    DIRECTION_NOTES_READ = "direction_notes.read"
    PROMPT_CONTEXT_APPEND = "prompt.context.append"
    DRAFT_REPLACE = "draft.replace"
    CARD_WRITE = "card.write"
    MODEL_CALL = "model.call"
    STATE_READ = "state.read"
    STATE_WRITE = "state.write"
    ARTIFACT_WRITE = "artifact.write"
    NETWORK_REQUEST = "network.request"
    UI_CONTRIBUTE = "ui.contribute"
    FRAGMENT_TYPE_CONTRIBUTE = "fragment_type.contribute"
    WRITER_TOOL_CONTRIBUTE = "writer.tool.contribute"
    AUDIT_DETECTOR_CONTRIBUTE = "audit.detector.contribute"


class GrantKind(StrEnum):
    """What a grant lets a package do, for grouping consent rows."""

    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"
    CONTRIBUTE = "contribute"


class DataClass(StrEnum):
    """The kind of user data a grant exposes.

    Membership -- not a hand-maintained list of capability names -- is what
    decides whether the consent combination banner fires. A new read grant that
    declares what it reads gets the banner for free; one that declares nothing
    is asserting it exposes no user data, which is a claim a reviewer can check
    against the projection.
    """

    CONVERSATION = "conversation"
    CHARACTER = "character"
    LOREBOOK = "lorebook"
    PERSONA = "persona"
    EXTENSION_STATE = "extension_state"


class Sensitivity(StrEnum):
    """How loudly the manager renders a grant.

    ``HIGH`` is for grants a user must not scroll past as one more checkbox:
    ones that spend money, reach the network, widen scope beyond the current
    conversation, or expose the user's own words back to a package.
    """

    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ValueSpec:
    """One admissible value of a parameterized capability.

    A value may say more than its capability does. ``state.read`` on ``config``
    is a package reading its own settings; on ``character`` it is a package
    reading a per-card record. Same capability, different sentence, and the
    consent row is per value -- so the copy is too.
    """

    copy: str
    reads: frozenset[DataClass] = frozenset()
    sensitivity: Sensitivity | None = None
    requires: Requirement | None = None
    """A grant this value is meaningless -- or dishonestly scoped -- without."""

    resource: str | None = None
    """The host resource this *value* gates, when the capability as a whole
    does not. Reading the persona is a ``context.read`` field and also the
    ``persona`` resource; reading tree structure is one value of
    ``conversation.tree.read`` while previews are another."""


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """The parameter that scopes a capability, and what it admits."""

    name: str
    """The manifest key carrying it (``scope``, ``lane``, ``slot``, ...)."""

    values: Mapping[str, ValueSpec] | None = None
    """The closed admissible set, or ``None`` when the value's own type
    validates it (a network origin is checked by the origin grammar, not by
    enumeration -- there is no finite list of hosts)."""

    multiple: bool = False
    """True when the manifest key holds a list and each member is its own
    grant, as ``prompt.context.append`` does with ``targets``."""

    weak_copy: str | None = None
    """Copy for values that warrant a stronger warning than the capability's
    own, recognised by inspection rather than by enumeration.

    An open parameter has no value table to hang a sentence on, but "reaches a
    server on your own machine" and "reaches a public API" are not the same
    consent decision, and both are ``network.request``. Setting this turns that
    distinction into copy the user reads instead of a nuance the origin string
    leaves them to notice."""


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """Every fact about one capability, in one place.

    ``copy`` is a required field rather than a lookup with a fallback: a grant
    whose consent line is missing would render as a bare identifier in the one
    dialog where a user must not have to guess, and a default that reads
    "unknown capability" turns that into a shipped string instead of a failed
    import.
    """

    copy: str
    kind: GrantKind
    parameter: ParameterSpec | None = None
    reads: frozenset[DataClass] = field(default_factory=frozenset)
    sensitivity: Sensitivity = Sensitivity.NORMAL
    resource: str | None = None
    """The host resource this grant gates, when it gates one."""


# Consent copy is second person and concrete about consequence, because the
# reader is deciding, not learning an API. "Read the reply Orb wrote, before you
# see it" says the same thing as "read the post-writer draft" to someone who
# knows the pipeline, and something to everyone else.
CAPABILITY_SPECS: Mapping[Capability, CapabilitySpec] = {
    Capability.CONTEXT_READ: CapabilitySpec(
        copy="Read part of the current conversation.",
        kind=GrantKind.READ,
        parameter=ParameterSpec(
            name="field",
            values={
                "input": ValueSpec(
                    copy="Read the message you send each turn.",
                    reads=frozenset({DataClass.CONVERSATION}),
                ),
                "draft": ValueSpec(
                    copy="Read the reply Orb wrote, before you see it.",
                    reads=frozenset({DataClass.CONVERSATION}),
                ),
                "history": ValueSpec(
                    copy="Read a recent window of this conversation's messages.",
                    reads=frozenset({DataClass.CONVERSATION}),
                ),
                "character": ValueSpec(
                    copy="Read the active character's text fields.",
                    reads=frozenset({DataClass.CHARACTER}),
                ),
                # The Director's decision for this turn, not the user's own
                # words: moods, scene direction, and the reduced fragment map.
                # Ordinary sensitivity for that reason -- it is Orb's staging of
                # the scene, which the character projection already implies.
                "direction": ValueSpec(
                    copy="Read the scene direction Orb set for this turn.",
                    reads=frozenset({DataClass.CONVERSATION}),
                ),
                # The user's own self-description, and the strongest trigger for
                # the combination banner.
                "persona": ValueSpec(
                    copy="Read your persona's name and description — your own self-description.",
                    reads=frozenset({DataClass.PERSONA}),
                    sensitivity=Sensitivity.HIGH,
                    resource="persona",
                ),
            },
        ),
    ),
    Capability.CONVERSATION_TREE_READ: CapabilitySpec(
        copy="Read this conversation's branch structure.",
        kind=GrantKind.READ,
        # Singular, so each field is its own consent row and previews stay
        # independently revocable. A list-valued parameter would make the whole
        # entry the unit of approval, and "previews are a separate conspicuous
        # grant" would quietly become "previews come with structure".
        parameter=ParameterSpec(
            name="field",
            values={
                "structure": ValueSpec(
                    copy="Read the structure of every branch in this conversation.",
                    reads=frozenset({DataClass.CONVERSATION}),
                    resource="conversation.tree",
                ),
                # Previews are strictly more than structure, so they are a
                # separate grant -- but a *value* of the same capability rather
                # than a capability of their own, which is what makes the
                # "previews implies structure" rule an ordinary prerequisite
                # instead of a special case in the enum.
                "preview": ValueSpec(
                    copy="Also read text previews from branches you are not currently on.",
                    reads=frozenset({DataClass.CONVERSATION}),
                    sensitivity=Sensitivity.HIGH,
                    requires=(Capability.CONVERSATION_TREE_READ.value, "structure"),
                ),
            },
        ),
    ),
    Capability.CONVERSATION_BRANCH_ACTIVATE: CapabilitySpec(
        copy="Switch which branch of the conversation is active.",
        kind=GrantKind.WRITE,
    ),
    # Deliberately its own capability rather than a value of a generic resource
    # read: enumeration is the reach grant. It also authorizes resolving a
    # package-named card into ``ctx.character``, which is more than any
    # projection this vocabulary otherwise hands out, and the consent line has
    # to be able to say so.
    Capability.LIBRARY_CARDS_READ: CapabilitySpec(
        copy="List your characters and read each one it is run against.",
        kind=GrantKind.READ,
        reads=frozenset({DataClass.CHARACTER}),
        sensitivity=Sensitivity.HIGH,
        resource="library.cards",
    ),
    # Untriggered lorebook entries are content the model may never have seen,
    # which makes this a stronger read than the history window.
    Capability.LOREBOOK_READ: CapabilitySpec(
        copy="Read this character's lorebook, including entries no message has triggered.",
        kind=GrantKind.READ,
        reads=frozenset({DataClass.LOREBOOK}),
        sensitivity=Sensitivity.HIGH,
        resource="lorebook.entries",
    ),
    Capability.DIRECTION_NOTES_READ: CapabilitySpec(
        copy="Read the direction notes on this branch.",
        kind=GrantKind.READ,
        reads=frozenset({DataClass.CONVERSATION}),
        resource="direction.notes",
    ),
    Capability.PROMPT_CONTEXT_APPEND: CapabilitySpec(
        copy="Add its own text to the prompt sent to the model each turn.",
        kind=GrantKind.WRITE,
        parameter=ParameterSpec(
            name="targets",
            multiple=True,
            values={
                "director": ValueSpec(copy="Add its own text to the Director's prompt each turn."),
                "writer": ValueSpec(copy="Add its own text to the Writer's prompt each turn."),
            },
        ),
    ),
    Capability.DRAFT_REPLACE: CapabilitySpec(
        copy="Rewrite the reply before it reaches you.",
        kind=GrantKind.WRITE,
    ),
    Capability.CARD_WRITE: CapabilitySpec(
        copy="Change a field on the character a command is run against.",
        kind=GrantKind.WRITE,
        parameter=ParameterSpec(
            name="field",
            values={
                # A tag write lands on ``ctx.character`` and nowhere else, so
                # without the projection that puts a card there it has no target
                # at all. Refusing it at parse time rather than at run time
                # keeps the consent line honest: "the character a command is run
                # against" is only true if the package can also see that
                # character.
                "tags": ValueSpec(
                    copy="Change the tags on the character a command is run against.",
                    requires=(Capability.CONTEXT_READ.value, "character"),
                ),
            },
        ),
    ),
    Capability.MODEL_CALL: CapabilitySpec(
        copy="Make its own model calls. This consumes tokens and may cost money.",
        kind=GrantKind.EXTERNAL,
        sensitivity=Sensitivity.HIGH,
        parameter=ParameterSpec(
            name="lane",
            values={
                "writer": ValueSpec(copy="Make its own model calls on your Writer endpoint. This may cost money."),
                "agent": ValueSpec(copy="Make its own model calls on your Agent endpoint. This may cost money."),
            },
        ),
    ),
    Capability.STATE_READ: CapabilitySpec(
        copy="Read data it has previously stored.",
        kind=GrantKind.READ,
        reads=frozenset({DataClass.EXTENSION_STATE}),
        parameter=ParameterSpec(
            name="scope",
            values={
                "config": ValueSpec(copy="Read its own settings."),
                "conversation": ValueSpec(copy="Read what it has stored about this conversation."),
                "message": ValueSpec(copy="Read what it has stored about individual messages."),
                "character": ValueSpec(copy="Read what it has stored about your characters."),
            },
        ),
    ),
    Capability.STATE_WRITE: CapabilitySpec(
        copy="Store data of its own.",
        kind=GrantKind.WRITE,
        parameter=ParameterSpec(
            name="scope",
            values={
                "config": ValueSpec(copy="Change its own settings."),
                "conversation": ValueSpec(copy="Store its own data on this conversation."),
                "message": ValueSpec(copy="Store its own data on individual messages."),
                "character": ValueSpec(copy="Store its own data on your characters."),
            },
        ),
    ),
    Capability.ARTIFACT_WRITE: CapabilitySpec(
        copy="Attach generated files to messages.",
        kind=GrantKind.WRITE,
    ),
    Capability.NETWORK_REQUEST: CapabilitySpec(
        copy="Send requests to an external server.",
        kind=GrantKind.EXTERNAL,
        sensitivity=Sensitivity.HIGH,
        parameter=ParameterSpec(
            name="origin",
            weak_copy=("Send requests to a server on your own machine or local network, or over an unencrypted connection."),
        ),
    ),
    Capability.UI_CONTRIBUTE: CapabilitySpec(
        copy="Add its own panels and menu entries to Orb.",
        kind=GrantKind.CONTRIBUTE,
        parameter=ParameterSpec(
            name="slot",
            values={
                "composer.menu": ValueSpec(copy="Add entries to the menu beside the message box."),
                "mobile.chat_actions": ValueSpec(copy="Add entries to the mobile chat action menu."),
                "tools": ValueSpec(copy="Add entries to the tools menu."),
                "inspector": ValueSpec(copy="Add its own panel to the inspector."),
                "message.toolbar": ValueSpec(copy="Add buttons to each message's toolbar."),
                "message.after": ValueSpec(copy="Add its own panel beneath each message."),
                "artifact.body": ValueSpec(copy="Render its own content inside artifacts."),
                "workspace": ValueSpec(copy="Open its own full-size workspace panel."),
                "library.card_actions": ValueSpec(copy="Add actions to each card in your character library."),
            },
        ),
    ),
    Capability.FRAGMENT_TYPE_CONTRIBUTE: CapabilitySpec(
        copy="Add new interactive-fragment types you can use in characters.",
        kind=GrantKind.CONTRIBUTE,
    ),
    # The only grant in the vocabulary that puts package-authored text into the
    # main pipeline's prompt *and* lets a package answer the Writer mid-reply.
    # Loud for both halves: the description ships every turn the tool is active
    # whether or not a call happens, and the result lands in the transcript the
    # Writer is continuing from.
    Capability.WRITER_TOOL_CONTRIBUTE: CapabilitySpec(
        copy=(
            "Add a callable tool and its instructions to the Writer. Its result can directly influence the reply "
            "even though the extension cannot write the reply itself. Orb may run it up to "
            f"{MAX_WRITER_TOOL_CALLS_PER_TURN} times while composing one reply."
        ),
        kind=GrantKind.CONTRIBUTE,
        sensitivity=Sensitivity.HIGH,
    ),
    # The Editor's mirror of the Writer tool, and loud for the same shape of
    # reason: a detector reads every draft before the user sees it, its findings
    # steer a rewrite the way the built-in scanners do, and -- since scoring slop
    # with a classifier is the use case -- it may send that draft to a model or
    # an origin.
    Capability.AUDIT_DETECTOR_CONTRIBUTE: CapabilitySpec(
        copy=(
            "Add its own check to the Output Auditor. It reads every reply before you see it and can make Orb "
            "rewrite parts of one."
        ),
        kind=GrantKind.CONTRIBUTE,
        sensitivity=Sensitivity.HIGH,
    ),
}

_MISSING_SPECS = set(Capability) - set(CAPABILITY_SPECS)
assert not _MISSING_SPECS, f"capabilities without a spec: {sorted(_MISSING_SPECS)}"


# ── derived views ────────────────────────────────────────────────────────────
# Everything below is computed. A table that had to be edited alongside the one
# above is a table that will eventually disagree with it, and the direction it
# disagreed in would decide whether an ungranted projection was served or a
# warning was shown.


def parameter_values(capability: Capability) -> frozenset[str]:
    """The closed admissible values of *capability*'s parameter, if it has one."""
    spec = CAPABILITY_SPECS[capability]
    if spec.parameter is None or spec.parameter.values is None:
        return frozenset()
    return frozenset(spec.parameter.values)


def parameter_name(capability: Capability) -> str | None:
    """The manifest key carrying *capability*'s parameter, if it takes one."""
    spec = CAPABILITY_SPECS[capability]
    return spec.parameter.name if spec.parameter is not None else None


PARAMETER_NAMES: frozenset[str] = frozenset(
    spec.parameter.name for spec in CAPABILITY_SPECS.values() if spec.parameter is not None
)
"""Every manifest key that can carry a grant's parameter, across all grants."""


RESOURCE_CAPABILITIES: Mapping[str, Requirement] = {
    **{spec.resource: (capability.value, None) for capability, spec in CAPABILITY_SPECS.items() if spec.resource},
    **{
        value_spec.resource: (capability.value, value)
        for capability, spec in CAPABILITY_SPECS.items()
        if spec.parameter is not None and spec.parameter.values
        for value, value_spec in spec.parameter.values.items()
        if value_spec.resource
    },
}
"""Which grant each host resource consumes.

Derived, so a resource cannot exist without a grant or name one that does not
exist. The compiler reads it when deriving a view's requirements and the
resource route reads it when refusing an ungranted request; two hand-written
tables would eventually disagree, and the direction they disagreed in would
decide whether an ungranted projection was served.

Note the baseline: a resource gated by a *value* names the projection everyone
granted that value may read. Anything beyond it -- tree previews -- is a further
grant the handler checks for itself, which is what lets one resource serve two
grant levels without two routes."""


GRANT_PREREQUISITES: Mapping[Requirement, frozenset[Requirement]] = {
    (capability.value, value): frozenset({spec.requires})
    for capability, capability_spec in CAPABILITY_SPECS.items()
    if capability_spec.parameter is not None and capability_spec.parameter.values
    for value, spec in capability_spec.parameter.values.items()
    if spec.requires is not None
}
"""Grants that are meaningless -- or dishonestly scoped -- without another.

Keyed by the full ``(capability, parameter)`` grant rather than by capability,
because both v1 cases are per value: ``card.write`` needs the character
projection only for its ``tags`` field, and tree previews need tree structure
without any *other* ``conversation.tree.read`` value needing anything.

Checked against the manifest's *requests* at parse time and against the live
grant set at publish time, so revoking a prerequisite blocks the dependent entry
point rather than leaving an operation that can never succeed."""


def with_prerequisites(grants: Iterable[Requirement]) -> set[Requirement]:
    """*grants* plus every grant they transitively require.

    Used by the compiler so a derivation states only what an operation directly
    reaches and gets the rest from the table. ``card.tags.set`` needs
    ``('card.write', 'tags')``; that its target has to be readable is a fact
    about the grant, not about the operation, and stating it in both places is
    how the two drift.
    """
    resolved = set(grants)
    pending = list(resolved)
    while pending:
        for prerequisite in GRANT_PREREQUISITES.get(pending.pop(), frozenset()):
            if prerequisite not in resolved:
                resolved.add(prerequisite)
                pending.append(prerequisite)
    return resolved


def missing_prerequisites(
    granted: Iterable[Requirement],
) -> list[tuple[Requirement, Requirement]]:
    """Every ``(grant, unmet prerequisite)`` pair in *granted*."""
    held = set(granted)
    return sorted(
        (grant, prerequisite)
        for grant, required in GRANT_PREREQUISITES.items()
        if grant in held
        for prerequisite in required
        if prerequisite not in held
    )


def reads_user_data(granted: Iterable[Requirement]) -> frozenset[DataClass]:
    """Every class of user data *granted* exposes.

    One half of the combination the consent banner exists for: reading is not
    the new risk, reading *combined with* network access is.
    """
    classes: set[DataClass] = set()
    for capability_value, parameter in granted:
        spec = _spec_for(capability_value)
        if spec is None:
            continue
        classes |= spec.reads
        value_spec = _value_spec(spec, parameter)
        if value_spec is not None:
            classes |= value_spec.reads
    return frozenset(classes)


@dataclass(frozen=True, slots=True)
class GrantDescription:
    """One grant as the manager renders it."""

    copy: str
    kind: GrantKind
    sensitivity: Sensitivity
    reads: frozenset[DataClass]


UNKNOWN_GRANT_COPY = "Use a capability this Orb build does not describe."


def describe(capability_value: str, parameter: str | None = None) -> GrantDescription:
    """The consent copy and emphasis for one ``(capability, parameter)`` grant.

    Parameter-aware, because the parameter is part of the grant: "Store data of
    its own" said the same thing for a package's own settings and for a write
    onto every character in the library, and those are not the same decision.
    """
    spec = _spec_for(capability_value)
    if spec is None:
        return GrantDescription(UNKNOWN_GRANT_COPY, GrantKind.READ, Sensitivity.HIGH, frozenset())

    value_spec = _value_spec(spec, parameter)
    sensitivity = spec.sensitivity
    copy = spec.copy
    if value_spec is not None:
        copy = value_spec.copy
        if value_spec.sensitivity is not None:
            sensitivity = value_spec.sensitivity
    elif spec.parameter is not None and spec.parameter.weak_copy and parameter and is_weak_origin(parameter):
        copy = spec.parameter.weak_copy
        sensitivity = Sensitivity.HIGH

    return GrantDescription(
        copy=copy,
        kind=spec.kind,
        sensitivity=sensitivity,
        reads=spec.reads | (value_spec.reads if value_spec is not None else frozenset()),
    )


def _spec_for(capability_value: str) -> CapabilitySpec | None:
    try:
        return CAPABILITY_SPECS[Capability(capability_value)]
    except ValueError:
        return None


def _value_spec(spec: CapabilitySpec, parameter: str | None) -> ValueSpec | None:
    if parameter is None or spec.parameter is None or spec.parameter.values is None:
        return None
    return spec.parameter.values.get(parameter)


def is_weak_origin(origin: str) -> bool:
    """Plain HTTP and loopback/private hosts warrant the stronger warning.

    A hostname is not resolved here -- resolution happens in the network client
    at request time, where it is revalidated on every redirect. This is the
    consent-time signal from the origin string alone, which is what the user is
    reading.
    """
    if not origin.startswith(("http://", "https://")):
        return False
    if origin.startswith("http://"):
        return True
    host = origin.split("://", 1)[1].split("/", 1)[0]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 and not host.startswith("[") else host
    host = host.strip("[]").split("]", 1)[0].lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


class OpContext(StrEnum):
    """Where a flow runs, which decides what it may do.

    ``POST_OBSERVE`` is not ``POST_TRANSFORM`` with a flag: an observer sees the
    final immutable draft, so ``draft.replace`` is absent from its allowlist
    rather than checked at runtime. ``REDUCER`` is the strictest profile -- a
    fragment reducer is a pure function from (config, previous, director
    output) to the next value.

    ``RECOVERY`` is the regenerate/reroll pair an artifact producer declares. It
    is not ``ACTION``: the framework already knows which attachment is being
    rebuilt, so a recovery flow names no target message -- and it must not
    activate a branch or rewrite a card's tags on the way, which is a guarantee
    worth having from the allowlist rather than from a reviewer.

    ``WRITER_TOOL`` is the API 2 Writer contribution, and it is deliberately not
    ``ACTION``. An action runs against a finished conversation with a user
    watching it; a Writer tool runs *inside* an unfinished model turn, where
    there is no assistant row to attach to, no draft to replace (the Writer owns
    the prose), no UI surface listening for a toast, and no user click to
    justify a first-party mutation. Reusing ``ACTION`` would admit every one of
    those by accident.
    """

    PRE_PIPELINE = "pre_pipeline"
    POST_TRANSFORM = "post_transform"
    POST_OBSERVE = "post_observe"
    ACTION = "action"
    RECOVERY = "recovery"
    REDUCER = "reducer"
    WRITER_TOOL = "writer_tool"
    DETECTOR = "detector"


HOOK_CONTEXTS = frozenset({OpContext.PRE_PIPELINE, OpContext.POST_TRANSFORM, OpContext.POST_OBSERVE})
IMPURE_CONTEXTS = frozenset(HOOK_CONTEXTS | {OpContext.ACTION, OpContext.RECOVERY})
"""Contexts with a user-facing surface and a persistable target.

The UI operations key off this set rather than off "may cause side effects",
which is why adding ``WRITER_TOOL`` to it would have been wrong: a Writer tool
does cause side effects, but it has no toast to raise and no view to
invalidate. Its progress is the host's fixed status channel."""

EXTERNAL_CONTEXTS = frozenset(IMPURE_CONTEXTS | {OpContext.WRITER_TOOL, OpContext.DETECTOR})
"""Contexts that may read and write namespaced state and call out to a model or
an HTTP origin. ``REDUCER`` is excluded because a reducer is a pure function."""

ALL_CONTEXTS = frozenset(OpContext)
PURE_CONTEXTS = frozenset(EXTERNAL_CONTEXTS | {OpContext.REDUCER})


class Quota(StrEnum):
    """An operation counter, local to each invocation and optionally turn-wide."""

    MODEL_CALL = "model_calls"
    HTTP_REQUEST = "http_requests"
    STATE_WRITE = "state_writes"
    CONTEXT_BLOCK = "context_blocks"
    DRAFT_REPLACE = "draft_replacements"
    BRANCH_ACTIVATE = "branch_activations"
    ARTIFACT = "artifacts"
    CARD_TAGS = "card_tag_writes"


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """Static facts about one operation, fixed at contract-freeze time."""

    capability: Capability | None
    contexts: frozenset[OpContext]
    produces_output: bool
    quota: Quota | None = None
    stages_effect: bool = False
    """True when the operation's result is staged until the flow returns
    successfully, rather than applied where it appears."""

    parameter: str | None = None
    """The fixed grant parameter this operation consumes, when the operation
    itself pins it. ``card.tags.set`` always writes the ``tags`` field, so the
    grant it needs is ``('card.write', 'tags')`` and nothing about the step
    varies that."""

    parameter_field: str | None = None
    """The *step* attribute carrying the grant parameter, when the step chooses
    it: ``state.set`` scopes by ``scope``, ``model.text`` by ``lane``. A list
    attribute yields one grant per member, as ``context.append`` does with its
    targets. Mutually exclusive with :attr:`parameter` -- an operation either
    pins its parameter or reads it, never both."""


def _spec(
    capability: Capability | None,
    contexts: frozenset[OpContext],
    *,
    output: bool = False,
    quota: Quota | None = None,
    staged: bool = False,
    parameter: str | None = None,
    parameter_field: str | None = None,
) -> OperationSpec:
    assert parameter is None or parameter_field is None, "an operation pins its parameter or reads it, never both"
    return OperationSpec(capability, contexts, output, quota, staged, parameter, parameter_field)


OPERATION_SPECS: dict[str, OperationSpec] = {
    # ── data and deterministic transforms ────────────────────────────────────
    "state.get": _spec(Capability.STATE_READ, EXTERNAL_CONTEXTS, output=True, parameter_field="scope"),
    "state.set": _spec(
        Capability.STATE_WRITE,
        EXTERNAL_CONTEXTS,
        quota=Quota.STATE_WRITE,
        staged=True,
        parameter_field="scope",
    ),
    "state.delete": _spec(
        Capability.STATE_WRITE,
        EXTERNAL_CONTEXTS,
        quota=Quota.STATE_WRITE,
        staged=True,
        parameter_field="scope",
    ),
    "text.concat": _spec(None, PURE_CONTEXTS, output=True),
    "text.replace_literal": _spec(None, PURE_CONTEXTS, output=True),
    "json.pick": _spec(None, PURE_CONTEXTS, output=True),
    "json.merge": _spec(None, PURE_CONTEXTS, output=True),
    # The only two list operations in v1, and deliberately not the seed of a
    # collection library: both are single bounded set/fold operations over a
    # capped array, with no per-element package logic. `map`/`filter`/`reduce`
    # stay excluded because they would evaluate package-supplied work per
    # element, which is the unbounded iteration the flow language excludes.
    "list.intersect": _spec(None, PURE_CONTEXTS, output=True),
    "list.join": _spec(None, PURE_CONTEXTS, output=True),
    "math.add": _spec(None, PURE_CONTEXTS, output=True),
    "math.subtract": _spec(None, PURE_CONTEXTS, output=True),
    "math.negate": _spec(None, PURE_CONTEXTS, output=True),
    "math.clamp": _spec(None, PURE_CONTEXTS, output=True),
    # Randomness is host-owned and per-invocation seeded, so it is deterministic
    # for a given invocation but not for a reducer, which must replay identically
    # when a branch is rewound.
    "random.integer": _spec(None, EXTERNAL_CONTEXTS, output=True),
    "random.choice": _spec(None, EXTERNAL_CONTEXTS, output=True),
    "if": _spec(None, PURE_CONTEXTS),
    "return": _spec(None, PURE_CONTEXTS),
    # ── host capabilities ────────────────────────────────────────────────────
    "model.text": _spec(
        Capability.MODEL_CALL,
        EXTERNAL_CONTEXTS,
        output=True,
        quota=Quota.MODEL_CALL,
        parameter_field="lane",
    ),
    "model.structured": _spec(
        Capability.MODEL_CALL,
        EXTERNAL_CONTEXTS,
        output=True,
        quota=Quota.MODEL_CALL,
        parameter_field="lane",
    ),
    "http.request": _spec(
        Capability.NETWORK_REQUEST,
        EXTERNAL_CONTEXTS,
        output=True,
        quota=Quota.HTTP_REQUEST,
        parameter_field="url",
    ),
    "context.append": _spec(
        Capability.PROMPT_CONTEXT_APPEND,
        frozenset({OpContext.PRE_PIPELINE}),
        quota=Quota.CONTEXT_BLOCK,
        staged=True,
        parameter_field="targets",
    ),
    "draft.replace": _spec(
        Capability.DRAFT_REPLACE,
        frozenset({OpContext.POST_TRANSFORM}),
        quota=Quota.DRAFT_REPLACE,
        staged=True,
    ),
    "artifact.emit": _spec(
        Capability.ARTIFACT_WRITE,
        frozenset(
            {
                OpContext.POST_TRANSFORM,
                OpContext.POST_OBSERVE,
                OpContext.ACTION,
                OpContext.RECOVERY,
            }
        ),
        quota=Quota.ARTIFACT,
        staged=True,
    ),
    # Action-only, and it takes no card identifier: it writes the card already
    # in the invocation's context. A per-turn hook silently rewriting library
    # metadata shared across every conversation is not a behavior a user can
    # supervise, and a card argument would make the grant library-wide.
    "card.tags.set": _spec(
        Capability.CARD_WRITE,
        frozenset({OpContext.ACTION}),
        quota=Quota.CARD_TAGS,
        staged=True,
        parameter="tags",
    ),
    "conversation.branch.activate": _spec(
        Capability.CONVERSATION_BRANCH_ACTIVATE,
        frozenset({OpContext.ACTION}),
        quota=Quota.BRANCH_ACTIVATE,
        staged=True,
    ),
    "ui.status": _spec(None, IMPURE_CONTEXTS),
    "ui.toast": _spec(None, IMPURE_CONTEXTS, staged=True),
    "ui.invalidate": _spec(Capability.UI_CONTRIBUTE, IMPURE_CONTEXTS, staged=True),
}

OPERATION_NAMES: frozenset[str] = frozenset(OPERATION_SPECS)


EXTERNAL_EFFECT_OPS: frozenset[str] = frozenset({"model.text", "model.structured", "http.request"})
"""Operations whose side effects leave Orb and cannot be rolled back.

A flow that also activates a branch may not use any of them: branch activation
holds the conversation stream lock, and the pipeline takes that lock before the
workflow locks, so a slow model or HTTP call inside that window is the shape of
a stream/workflow deadlock.
"""
