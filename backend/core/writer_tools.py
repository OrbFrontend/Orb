"""The Writer-tool ABI: canonical values shared by three layers that cannot
import one another.

``workflows/`` binds a :class:`WriterToolSpec` to an async callable and carries
it on a registry snapshot; ``features/extensions/`` builds that callable from a
compiled package flow; ``pipeline/`` sends the spec's schema in the Writer's
tool blob and invokes the binding it captured. None of the three may import the
other two in the direction this contract needs, and all three must agree
byte-for-byte on the provider-facing function name and on the JSON the Writer
receives back -- which is the "one identity across owners that cannot legally
import one another" the core admission rule asks for.

What lives here is only that: immutable value contracts plus the pure
invariants over them. Core does not parse manifests, read grants, hold a
registry, know a flow path, or perform I/O. The built-in Writer-tool set is
empty and stays empty -- "no tools" is an empty binding collection on a registry
snapshot, never a module-global list an extension mutates.

The name derivation is the load-bearing invariant. A package never declares a
provider-facing function name: Orb derives one from the owner and local ids,
under a grammar strict enough for every supported provider, so no package
string can collide with a built-in tool name, shadow another package's tool, or
reach a provider as something other than an identifier.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

WRITER_TOOL_PREFIX = "orb_writer_"
"""Namespace every derived Writer-tool wire name carries.

A fixed prefix rather than a hash: the OOC policy quotes the name to the model,
a user reads it in a log, and "this is an Orb-supplied Writer tool" should be
legible from the name alone. It is also what keeps the derived namespace
disjoint from ``inference/tool_registry``'s built-ins without either side
consulting the other.
"""

MAX_WIRE_NAME_CHARS = 64
"""The strictest function-name length across supported providers.

OpenAI's documented limit; other chat providers are equal or laxer. Deriving
against the strictest one means a package that installs on any endpoint runs on
all of them, rather than failing at the first turn on the narrowest.
"""

_WIRE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

MAX_TOOL_CALL_ID_CHARS = 128
"""Cap on a provider-supplied ``tool_call.id`` Orb will echo back.

Echoed verbatim into an assistant message and a matching tool result, so it is
bounded and shape-checked before it is replayed. A call whose id fails this is
not replayable as a valid tool exchange -- the Writer loop recovers from a
clean branch rather than inventing one."""

_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class _FrozenMapping(Mapping[str, Any]):
    """A minimal deeply read-only mapping for the core value contract."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


MAX_WRITER_TOOLS_PUBLISHED = 32
"""Writer-tool bindings one registry snapshot may carry."""

MAX_WRITER_TOOL_BLOB_BYTES = 8 * 1024
"""Every published Writer-tool schema together, canonically encoded.

A snapshot-level cap rather than a package-level one, which is why it lives
beside the ABI instead of in the extension feature's limits table: the registry
is what refuses to publish over it, and the registry cannot import the feature.
At most one selected schema is ever sent, so this bounds what a future
multi-tool turn would cost -- and it bounds it at publish time, where the
failure is "this overlay was refused", not "this turn's prompt was too large".
"""


class WriterToolError(ValueError):
    """A Writer-tool value violated its ABI invariant."""


@dataclass(frozen=True, slots=True)
class WriterToolKey:
    """Which contribution a spec, a binding, and an invocation all mean.

    The owner id plus the contribution's local id, never the wire name: the
    wire name is derived *from* the key, so comparing keys cannot be fooled by
    two owners whose derived names happened to normalize together (they cannot,
    but that is a property of :func:`wire_name`, not something the identity
    type should depend on).
    """

    owner_id: str
    local_id: str

    def __str__(self) -> str:
        return f"{self.owner_id}:{self.local_id}"


@dataclass(frozen=True, slots=True)
class WriterToolSpec:
    """One Writer tool as the model sees it, plus the identity behind it.

    ``schema`` is the complete provider tool entry (``{"type": "function",
    "function": {...}}``), built once by the owner that compiled it and then
    treated as immutable bytes: the pipeline serializes it into the shared tool
    blob without editing it, because every pass in a single-model turn has to
    receive the same bytes.

    ``content_digest`` pins the spec to the revision that produced it. A turn
    invokes only the binding captured with the exact schema generation it sent,
    and this is the field that makes "exact" checkable rather than assumed.
    """

    key: WriterToolKey
    wire_name: str
    label: str
    schema: Mapping[str, Any]
    content_digest: str = ""

    def __post_init__(self) -> None:
        # ``frozen=True`` protects only the field reference. Freeze every nested
        # JSON container too, or a caller holding the compiler's original dict
        # could mutate the provider bytes after a registry snapshot captured it.
        object.__setattr__(self, "schema", _freeze_json(self.schema))

    @property
    def owner_id(self) -> str:
        return self.key.owner_id

    def provider_schema(self) -> dict[str, Any]:
        """Return a mutable JSON copy suitable for provider serialization."""
        return _plain_json(self.schema)


def _freeze_json(value: Any) -> Any:
    """Recursively freeze a JSON-like value without changing its semantics."""
    if isinstance(value, Mapping):
        return _FrozenMapping({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    """Undo :func:`_freeze_json` into ordinary provider-serializable values."""
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class WriterToolInvocation:
    """One validated call the Writer made, as the executing owner receives it.

    ``draft`` is supplied by the host from the prose already streamed, never by
    the model: the package cannot require the model to echo the draft into its
    arguments, and a model that tried could not redirect the invocation by
    doing so.
    """

    key: WriterToolKey
    call_id: str
    arguments: Mapping[str, Any]
    draft: str
    conversation_id: str | None = None
    turn_seed: str = ""


@dataclass(frozen=True, slots=True)
class WriterToolResult:
    """What a successful Writer-tool binding returns.

    A JSON value and nothing else -- no diagnostics, no timings, no extension
    error text. Everything the Writer sees is built from this by
    :func:`writer_tool_ok`, so there is no field through which an owner could
    put its own prose in front of the model.
    """

    value: Any


# ── error vocabulary ─────────────────────────────────────────────────────────
# Fixed codes, not messages. The Writer is a model reading a transcript, and an
# internal exception string in that transcript is both an information leak and
# an instruction the host did not author.

RESOLVER_UNAVAILABLE = "resolver_unavailable"
"""The tool could not run or its result was unusable. One code covers timeout,
revoked permission, invalid output, and a sanitized flow error on purpose: the
Writer's next move is the same for all of them, and distinguishing them would
describe Orb's internals to a package's prompt. Turn cancellation emits no tool
result and starts no continuation."""

INVALID_ARGUMENTS = "invalid_arguments"
"""The model's arguments did not parse or did not match the declared schema."""

TOOL_NOT_AVAILABLE = "tool_not_available"
"""The model named a tool that is not this turn's active Writer tool."""

WRITER_TOOL_ERROR_CODES: frozenset[str] = frozenset({RESOLVER_UNAVAILABLE, INVALID_ARGUMENTS, TOOL_NOT_AVAILABLE})


def wire_name(key: WriterToolKey) -> str:
    """The provider-facing function name for *key*.

    Derived, never declared. Ordinary ids use the readable
    ``orb_writer_<owner>--<local>`` form. The id grammar also permits ``--``
    *inside* either half, so those uncommon keys use a reserved length-prefixed
    form, ``orb_writer__<owner-length>_<owner>_<local>``. The reserved form starts
    with ``_`` where an owner id must start alphanumeric, and the owner length
    makes the remaining split injective.

    Raises :class:`WriterToolError` when the result would not be a legal
    function name under the strictest supported provider grammar, including
    when the combined length overflows. That is a compile-time failure for the
    package, not a runtime surprise: a name that cannot be sent is a
    contribution that cannot be published.
    """
    owner, local = key.owner_id, key.local_id
    for part, what in ((owner, "extension id"), (local, "Writer tool id")):
        if not isinstance(part, str) or _ID_RE.fullmatch(part) is None:
            raise WriterToolError(f"Writer tool {what} {part!r} does not match the lowercase id grammar")
    if "--" in owner or "--" in local:
        name = f"{WRITER_TOOL_PREFIX}_{len(owner)}_{owner}_{local}"
    else:
        name = f"{WRITER_TOOL_PREFIX}{owner}--{local}"
    if len(name) > MAX_WIRE_NAME_CHARS:
        raise WriterToolError(
            f"Writer tool name for {key} is {len(name)} characters, over the {MAX_WIRE_NAME_CHARS} character "
            f"limit the strictest supported provider allows; shorten the extension or tool id"
        )
    if _WIRE_NAME_RE.fullmatch(name) is None:  # pragma: no cover - unreachable given the grammar above
        raise WriterToolError(f"derived Writer tool name {name!r} is not a valid provider function name")
    return name


def is_writer_tool_name(name: object) -> bool:
    """Whether *name* has the shape :func:`wire_name` produces.

    The *shape*, not merely the prefix: a bare ``orb_writer_`` has no owner and
    no local id, so treating it as "in the namespace" would let a string that
    can never name a real tool pass a namespace check.

    Used to keep the derived namespace disjoint from built-in tool names without
    either registry importing the other. It says nothing about whether the tool
    exists, is published, is active, or may be called -- those are the captured
    allowlist's business, and this function is deliberately unable to answer
    them.
    """
    if (
        not isinstance(name, str)
        or len(name) > MAX_WIRE_NAME_CHARS
        or _WIRE_NAME_RE.fullmatch(name) is None
        or not name.startswith(WRITER_TOOL_PREFIX)
    ):
        return False
    suffix = name[len(WRITER_TOOL_PREFIX) :]
    if suffix.startswith("_"):
        match = re.match(r"^_([1-9][0-9]?)_", suffix)
        if match is None:
            return False
        owner_length = int(match.group(1))
        rest = suffix[match.end() :]
        if len(rest) <= owner_length or rest[owner_length] != "_":
            return False
        owner, local = rest[:owner_length], rest[owner_length + 1 :]
        if "--" not in owner and "--" not in local:
            return False
    else:
        if suffix.count("--") != 1:
            return False
        owner, local = suffix.split("--", 1)
    try:
        return wire_name(WriterToolKey(owner, local)) == name
    except WriterToolError:
        return False


def valid_call_id(call_id: object) -> bool:
    """Whether a provider-supplied tool-call id can be replayed safely.

    A call Orb cannot echo back is not a tool exchange it can complete. The
    Writer loop's recovery for a bad id is a clean branch with no tool
    messages, because fabricating an id would put a call in the transcript that
    the provider never made.
    """
    return isinstance(call_id, str) and _CALL_ID_RE.fullmatch(call_id) is not None


def writer_tool_ok(value: Any) -> dict[str, Any]:
    """The canonical success payload the tool-role message carries."""
    return {"status": "ok", "result": value}


def writer_tool_error(code: str) -> dict[str, Any]:
    """The canonical failure payload, from the fixed code vocabulary.

    An unknown code collapses to :data:`RESOLVER_UNAVAILABLE` rather than
    reaching the model: the point of a closed vocabulary is that a caller
    cannot widen it by passing a string, and raising here would turn a
    recoverable extension failure into a turn failure.
    """
    return {"status": "error", "code": code if code in WRITER_TOOL_ERROR_CODES else RESOLVER_UNAVAILABLE}


__all__ = [
    "INVALID_ARGUMENTS",
    "MAX_TOOL_CALL_ID_CHARS",
    "MAX_WIRE_NAME_CHARS",
    "MAX_WRITER_TOOLS_PUBLISHED",
    "MAX_WRITER_TOOL_BLOB_BYTES",
    "RESOLVER_UNAVAILABLE",
    "TOOL_NOT_AVAILABLE",
    "WRITER_TOOL_ERROR_CODES",
    "WRITER_TOOL_PREFIX",
    "WriterToolError",
    "WriterToolInvocation",
    "WriterToolKey",
    "WriterToolResult",
    "WriterToolSpec",
    "is_writer_tool_name",
    "valid_call_id",
    "wire_name",
    "writer_tool_error",
    "writer_tool_ok",
]
