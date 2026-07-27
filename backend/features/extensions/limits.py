"""Every hard bound the community-extension subsystem enforces, in one place.

The package format, the compiler, and the (future) interpreter all quote the
same numbers, so a limit that moves moves once. Splitting them across the
parser and the runtime is how a "1 MiB manifest" becomes a 4 MiB manifest that
merely *parses* in two steps -- these constants exist so the streaming
boundary and the post-parse check cannot disagree.

Source of truth: ``docs/architecture/community-extensions.md`` sections 4
(package limits) and 5 (execution limits). Numbers here are byte counts unless
the name says otherwise; ``KIB``/``MIB`` suffixes are always UTF-8 bytes, never
characters, because a character bound is not a memory bound.
"""

from __future__ import annotations

KIB = 1024
MIB = 1024 * 1024

# ── package limits (section 4) ───────────────────────────────────────────────
# Applied while reading a source, before anything is persisted. Each is checked
# at its streaming boundary: an archive that *claims* 2 MiB but expands past
# MAX_REFERENCED_BYTES_TOTAL is rejected mid-expansion, not after.

MAX_SOURCE_BYTES = 50 * MIB
"""Downloaded Git pack or ``.orbext`` archive, compressed bytes on the wire."""

MAX_TREE_ENTRIES = 512
"""Reachable tree entries in the package root, directories included."""

MAX_REFERENCED_BYTES_TOTAL = 25 * MIB
"""All manifest-referenced files together, after decompression."""

MAX_MANIFEST_BYTES = 1 * MIB
"""``orb-extension.json`` alone."""

MAX_ASSET_BYTES = 10 * MIB
"""One referenced asset."""

MAX_PATH_BYTES = 240
"""One normalized relative path, in UTF-8 bytes."""

# ── JSON parse limits (section 5, "Execution limits") ────────────────────────
# Shared by package files and by every runtime JSON value, so a value that
# could not have been authored also cannot be synthesized at runtime.

MAX_JSON_DEPTH = 32
"""Nesting depth of any JSON container."""

MAX_JSON_MEMBERS = 1024
"""Members of any one object or array."""

MAX_JSON_STRING_BYTES = 256 * KIB
"""One JSON string value, in UTF-8 bytes."""

# ── flow execution limits (section 5) ────────────────────────────────────────

MAX_FLOW_STEPS_EXECUTED = 128
MAX_FLOW_NESTING_DEPTH = 8
MAX_PREDICATE_DEPTH = 8
MAX_MODEL_CALLS_PER_INVOCATION = 2
MAX_HTTP_REQUESTS_PER_INVOCATION = 4
MAX_MODEL_OUTPUT_TOKENS = 4096
MAX_MODEL_OUTPUT_BYTES = 1 * MIB
MAX_HTTP_REQUEST_BODY_BYTES = 1 * MIB
MAX_HTTP_RESPONSE_BYTES = 5 * MIB
MAX_HTTP_REDIRECTS = 3
HTTP_TIMEOUT_SECONDS = 30.0
MAX_STATE_BYTES_PER_SCOPE = 256 * KIB
MAX_CONTEXT_BLOCK_BYTES = 8 * KIB
MAX_CONTEXT_BYTES_PER_TARGET = 32 * KIB
MAX_DRAFT_BYTES = 1 * MIB
MAX_ACTION_RESULT_BYTES = 1 * MIB

# ── declaration-shape limits ─────────────────────────────────────────────────
# Bounds on the *declaration* rather than on runtime values. A package that
# declares 10,000 steps is rejected at parse time; the step budget above is
# what an invocation may actually execute (a loop-free flow can still declare
# fewer steps than it executes, via ``if`` branches).

MAX_FLOW_STEPS_DECLARED = 128
MAX_IDENTIFIER_CHARS = 64
MAX_LABEL_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000
MAX_TEMPLATE_CHARS = 8192
MAX_TEMPLATE_SUBSTITUTIONS = 32
MAX_REF_PATH_SEGMENTS = 8
MAX_PERMISSIONS = 64
MAX_COMMANDS = 32
MAX_PLACEMENTS = 32
MAX_VIEWS = 32
MAX_ACTIONS = 32
MAX_ORIGINS = 8
MAX_SECRETS = 8
MAX_FRAGMENT_TYPES = 16
MAX_COMPONENT_NODES = 256
MAX_COMPONENT_DEPTH = 12

# ── fragment-type resolution (section 9) ─────────────────────────────────────

MAX_EXTENSION_FRAGMENT_INSTANCES_PER_TURN = 50
"""Active extension-backed fragment instances resolved per turn, across global
and card sources. Excess instances are diagnosed and skipped in deterministic
fragment order rather than silently multiplying the Director schema."""
