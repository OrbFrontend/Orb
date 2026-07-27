"""Failure vocabulary for package reading, parsing, and compilation.

Every error carries a *sanitized* message: it may name the package path, the
limit that was exceeded, and the JSON location, but never raw package content,
a secret value, or a host filesystem path. Callers surface ``str(exc)`` straight
into an install diagnostic and into the extension manager UI, so anything
unsafe to show a user must not reach the message in the first place.

``PackageError`` is the one type an installer needs to catch. The subclasses
exist so the API layer can map a rejection to the right HTTP status and the
right consent-flow step without string matching.
"""

from __future__ import annotations


class PackageError(Exception):
    """Base: this package cannot be installed or loaded as presented."""


class PackageLimitExceeded(PackageError):
    """A declared package limit was exceeded (section 4 / section 5 bounds).

    Raised at the streaming boundary wherever possible, so the process never
    holds the oversized value it is rejecting.
    """


class PackageParseError(PackageError):
    """A package file is not strictly-valid JSON under Orb's parse rules.

    Duplicate object keys, non-finite numbers, invalid Unicode, trailing data,
    and depth/size violations all land here.
    """


class PackageValidationError(PackageError, ValueError):
    """A package file parsed but violates the v1 contract.

    Unknown fields, malformed references, an undeclared derived requirement, a
    forward step reference, an operation used in a hook stage that forbids it.

    Also a ``ValueError`` so it survives a Pydantic ``AfterValidator``: Pydantic
    converts ``ValueError`` into a field error and lets anything else propagate
    as a crash. Path and reference validators run inside models, so without this
    a malformed package path would surface as a 500 instead of the field-level
    rejection every other malformed value gets.
    """


class FlowError(Exception):
    """One flow invocation failed. Not a ``PackageError``: the package is fine.

    Raised by the interpreter for a quota, a type mismatch, a revoked grant, a
    schema violation, or a cancelled turn. The distinction from
    :class:`PackageError` is operational, not cosmetic -- a package that fails
    an invocation stays installed and available, whereas a package that fails
    to *compile* does not publish entry points at all.

    Messages are sanitized the same way: a limit, an operation name, a step id,
    or a schema location, never a resolved value, a model response, or a secret.
    A hook failure is logged and discarded; an explicit action returns this
    string to the user.
    """


class FlowCancelled(FlowError):
    """The owning turn, request, or client connection went away.

    Separate so the caller can distinguish "stop, nothing is wrong" from a real
    failure: a cancelled hook is not worth a diagnostic on the package.
    """


class PackageIncompatible(PackageError):
    """The package is well-formed but this Orb build cannot run it.

    An ``extension_api`` this build does not implement, or a declared
    ``requires.operations`` / ``requires.components`` entry that does not exist
    here. The distinction matters: an incompatible revision stays *installed*
    with a diagnostic, whereas an invalid one is rejected outright.
    """
