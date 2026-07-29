"""Community extensions -- the untrusted, declarative extension tier.

Orb has two extension mechanisms with an explicit trust distinction. Trusted
built-in workflows (``backend/workflows/**`` plus ``frontend/workflows/**``)
are Python callables and same-origin ES modules that ship with Orb and receive
normal code review. Community extensions -- this slice -- are **data**: a
manifest, declarative flows, component trees, and constrained schemas that Orb
interprets. No Python, JavaScript, HTML, CSS, WASM, native library, evaluating
template, or install script is ever loaded from a package.

What that claim does and does not mean is worth being precise about. "No RCE
surface" means there is no intentional primitive that interprets a package file
as host or browser code, launches a package-selected process, or hands a
package an object capable of doing so. It is not a claim that parsers, Git
clients, media decoders, or Orb itself can never contain a vulnerability --
which is why the package limits, the strict parser, and the hostile-package
test corpus in ``tests/unit/extensions/`` are load-bearing rather than
decorative.

Contents:

* :mod:`.contracts` -- frozen v1 models for manifest, permissions, flows,
  values, schemas, components, effects, and fragment-type descriptors.
* :mod:`.json_loader` -- the strict, duplicate-key-rejecting JSON parser every
  package file goes through.
* :mod:`.digest` -- canonical JSON encoding and the shared content digest.
* :mod:`.paths` -- package-path normalization and collision rules.
* :mod:`.limits` / :mod:`.errors` -- the bounds and the failure vocabulary.
* :mod:`.sources` -- bounded archive and content-store readers, which offer no
  enumeration, so only manifest-referenced files can ever be read.
* :mod:`.assets` -- the media allowlist and leading-byte check.
* :mod:`.compiler` -- reference-graph validation, requirement derivation, and
  the immutable compiled record.
* :mod:`.content_store` -- the digest-addressed store, its durability order,
  and garbage collection.
* :mod:`.staging` -- opaque, expiring, single-use consent tokens.
* :mod:`.runtime` -- compiling installed revisions and publishing them as one
  community overlay.
* :mod:`.lifecycle` -- inspect/install/update/rollback/enable/permissions/
  uninstall/purge plus startup reconciliation.
* :mod:`.catalog` -- the host-owned projections the extension manager renders.
* :mod:`.values` -- runtime resolution of ``$ref``, ``$template``, and the
  predicate AST, plus the ``MISSING`` sentinel that keeps "absent" and
  ``null`` distinct.
* :mod:`.ctx` -- the capability-filtered ``ExtensionCtx`` projection, built
  field by field from grants rather than filtered down from a trusted context.
* :mod:`.interpreter` -- bounded execution with quotas, deterministic
  randomness, cancellation, and the staged effect transaction.
* :mod:`.adapters` -- compiled flows bound as workflow subscriptions and named
  actions, owning their own lock plan and committing staged effects.
* :mod:`.execution` -- process-local invocation gating and drain/cancel
  coordination for disable, purge, and shutdown.
* :mod:`.network` -- the bounded egress every package-influenced request goes
  through, with origin derivation, address validation, and connection pinning.
* :mod:`.secrets` -- write-only secret storage and the substitution that
  happens inside the network client and nowhere else.
* :mod:`.git_source` -- the in-process, byte-bounded shallow fetch and object
  walk. No system ``git``, no checkout.
* :mod:`.artifacts` -- ``artifact.emit``'s declared byte sources, media
  allowlist, and recovery metadata.
* :mod:`.resources` -- the cursor-paginated host read surfaces a view or flow
  may consume, each behind its own grant.
* :mod:`.writer_tools` -- the compiled API 2 Writer-tool spec and the executor
  a snapshot publishes as a binding.
* :mod:`.telemetry` -- host-only invocation counters. No prompt or result
  content.

:data:`.interpreter.UNIMPLEMENTED_OPS` is empty: every operation the contract
parses is executable. It stays as a tested seam for a future operation whose
contract lands before its runtime -- an entry point reaching one is *blocked*
with a diagnostic rather than failing halfway. The design note is explicit that
no permissive placeholder executor should stand in for the real thing: a
temporary "run arbitrary operation" switch is difficult to tighten once
packages exist that depend on it.

Full contract, for authors and for anyone growing the ABI:
``docs/architecture/community-extensions.md``.
"""

from __future__ import annotations

from .digest import PackageContent, canonical_json_bytes, content_digest
from .errors import (
    PackageError,
    PackageIncompatible,
    PackageLimitExceeded,
    PackageParseError,
    PackageValidationError,
)
from .json_loader import load_json
from .lifecycle import (
    LifecycleConflict,
    LifecycleError,
    collect_content_garbage,
    reconcile,
)
from .paths import assert_no_case_collisions, normalize_package_path
from .runtime import current_state

__all__ = [
    "LifecycleConflict",
    "LifecycleError",
    "PackageContent",
    "PackageError",
    "PackageIncompatible",
    "PackageLimitExceeded",
    "PackageParseError",
    "PackageValidationError",
    "assert_no_case_collisions",
    "canonical_json_bytes",
    "collect_content_garbage",
    "content_digest",
    "current_state",
    "load_json",
    "normalize_package_path",
    "reconcile",
]
