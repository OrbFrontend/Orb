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

Contents (Phases 0-1 -- contracts, package lifecycle, no executor):

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

Deliberately absent until their phase lands: the flow interpreter, the
capability-filtered context projection, the host HTTP client and secrets
substitution, the component renderer, the Git reader, and fragment-type
contribution. Phase 1 publishes community records with **no subscriptions** --
an installed package contributes catalog metadata and nothing executable. The
design note is explicit that no permissive placeholder executor should stand in
for the real one: a temporary "run arbitrary operation" switch is difficult to
tighten once packages exist that depend on it.

Full design: ``docs/architecture/community-extensions.md``.
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
