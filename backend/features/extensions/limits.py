"""Every hard bound the community-extension subsystem enforces, in one place.

The package format, the compiler, and the (future) interpreter all quote the
same numbers, so a limit that moves moves once. Splitting them across the
parser and the runtime is how a "1 MiB manifest" becomes a 4 MiB manifest that
merely *parses* in two steps -- these constants exist so the streaming
boundary and the post-parse check cannot disagree.

Source of truth: the "Limits" tables in
``docs/architecture/community-extensions.md``. Numbers here are byte counts unless
the name says otherwise; ``KIB``/``MIB`` suffixes are always UTF-8 bytes, never
characters, because a character bound is not a memory bound.
"""

from __future__ import annotations

KIB = 1024
MIB = 1024 * 1024

# ── package limits ───────────────────────────────────────────────────────────
# Applied while reading a source, before anything is persisted. Each is checked
# at its streaming boundary: an archive that *claims* 2 MiB but expands past
# MAX_REFERENCED_BYTES_TOTAL is rejected mid-expansion, not after.

MAX_SOURCE_BYTES = 50 * MIB
"""Downloaded Git pack or ``.orbext`` archive, compressed bytes on the wire."""

MAX_GIT_OBJECT_BYTES = 25 * MIB
"""One expanded Git object before it enters Dulwich's object store."""

MAX_GIT_EXPANDED_BYTES = 100 * MIB
"""Aggregate expanded object/delta bytes in one received Git pack."""

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

# ── JSON parse limits ────────────────────────────────────────────────────────
# Shared by package files and by every runtime JSON value, so a value that
# could not have been authored also cannot be synthesized at runtime.

MAX_JSON_DEPTH = 32
"""Nesting depth of any JSON container."""

MAX_JSON_MEMBERS = 1024
"""Members of any one object or array."""

MAX_JSON_STRING_BYTES = 256 * KIB
"""One JSON string value, in UTF-8 bytes."""

# ── flow execution limits ────────────────────────────────────────────────────

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

MAX_LIST_OPERATION_MEMBERS = 256
"""Members of either input array to ``list.intersect`` / ``list.join``.

Both operations are single bounded folds with no per-element package logic, so
this is a memory bound rather than a work bound -- but it is also what keeps
them from being mistaken for the seed of a collection library."""

MAX_ARTIFACTS_PER_INVOCATION = 2
"""``artifact.emit`` calls per invocation. Two, not one: a producer that emits a
rendered result beside its source data is the ordinary case, and the byte budget
below -- not the count -- is what bounds the cost."""

MAX_ARTIFACT_BYTES = 10 * MIB
"""One emitted artifact. Matches ``MAX_ASSET_BYTES`` so the largest package
asset a flow may attach is not larger than the attachment it becomes."""

MAX_ARTIFACT_FILENAME_CHARS = 120

MAX_ARTIFACT_RECOVERY_BYTES = 16 * KIB
"""The package-authored half of an artifact's recovery metadata.

Small because it is *parameters*, not payload: enough to say which prompt,
model, or endpoint produced the bytes, and not enough to smuggle the bytes back
in beside them."""

MAX_WRITER_TOOL_ARGUMENT_BYTES = 16 * KIB
"""The model's encoded arguments to one Writer-tool call.

Small on purpose. The tool input carries a *semantic request* -- what the
character is attempting and what is at stake -- while the draft, the
conversation, and every entity identity come from the host. A budget large
enough to hold a draft would invite a package to ask for one."""

MAX_WRITER_TOOL_RESULT_BYTES = 8 * KIB
"""One Writer-tool result, encoded, before it reaches the model.

Deliberately far below ``MAX_ACTION_RESULT_BYTES``: an action's return value is
read by the host renderer, but this one is spliced into an unfinished model turn
where every byte is context the Writer pays for and cannot decline."""

MAX_WRITER_TOOL_SCHEMA_BYTES = 4 * KIB
"""One contributed Writer-tool schema entry, canonically encoded.

Charged against the *tool blob*, which in single-model mode is part of the
prefix every pass shares. A schema is prompt bytes the user did not write, so
it is bounded like prompt bytes rather than like a declaration."""

# The aggregate blob budget and the published-binding count are snapshot-level
# caps enforced by ``workflows/registry.py``, which cannot import this module.
# They live beside the ABI in ``core/writer_tools.py`` instead --
# ``MAX_WRITER_TOOL_BLOB_BYTES`` and ``MAX_WRITER_TOOLS_PUBLISHED``.

MAX_WRITER_TOOL_DESCRIPTION_CHARS = 600
"""The package-authored tool description, and each property description.

Bounded package-authored *model input*: it influences generation on every turn
the tool is active, whether or not a call happens. ``MAX_DESCRIPTION_CHARS``
(2000) is the bound on catalog copy a user reads once; this is the bound on
text a model reads every turn, and they are not the same quantity."""

MAX_AUDIT_FINDINGS_PER_DETECTOR = 8
"""Findings one detector invocation may return.

Charged against the Editor's report, which is prompt bytes on every rewrite
iteration -- and against ``MAX_PREFILL_TARGETS`` (8), so one detector can at
most fill the prefill batch it is inserted at the head of."""

MAX_AUDIT_FINDING_SNIPPET_CHARS = 400
"""One finding's draft span. Long enough for a sentence or two, short enough
that a "finding" cannot be the whole draft echoed back as a patch anchor."""

MAX_AUDIT_FINDING_NOTE_CHARS = 300
"""One finding's explanation. Bounded package-authored *model input*: it lands
in the Editor's tail message and in the prefilled patch prompt."""

# The per-turn wall clock for the detector batch is
# ``AUDIT_DETECTOR_TIMEOUT_SECONDS`` in ``workflows/contracts.py``: the Editor
# enforces it, and ``pipeline/`` cannot import this peer slice.

MAX_CARD_TAG_WRITES_PER_INVOCATION = 1
"""``card.tags.set`` calls per invocation. One card per invocation, by design:
library-wide reach comes from a user driving a host-rendered loop, never from
a single flow widening its own blast radius."""

# ── host resources ───────────────────────────────────────────────────────────
# Every resource is bounded by both an item count and an encoded-byte budget.
# The tree fails past its budget because a partial graph looks complete; every
# other resource paginates, because a single-response cap would either truncate
# a sweep into reporting success over cards it never saw, or lock a large
# library out of the feature permanently.

MAX_TREE_NODES = 2000
"""Nodes in one conversation-tree projection before ``resource_too_large``."""

MAX_TREE_PREVIEW_CHARS = 120
"""One node's preview, when ``conversation.tree.read`` is granted on ``preview``."""

MAX_RESOURCE_BYTES = 512 * KIB
"""Encoded size of one host-resource response."""

MAX_RESOURCE_PAGE_ITEMS = 100
"""Items in one page of a cursor-paginated host resource."""

MAX_CTX_PERSONA_BYTES = 8 * KIB
"""Aggregate UTF-8 bytes of the active-persona projection."""

MAX_RESOURCE_TEXT_BYTES = 4 * KIB
"""One variable-length text field inside a paginated resource item."""

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
"""The declared ``$template`` literal. A *declaration* bound, like its
neighbours here -- see ``MAX_RENDERED_TEMPLATE_CHARS`` for what one may render
to, which is a different quantity with a different right answer."""

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
MAX_AUDIT_DETECTORS = 4
"""Audit detectors one package may contribute.

Small because each one is a whole extra pass over every draft, and a package
that wants four different checks almost always wants one flow with four
branches. The snapshot-wide cap is ``MAX_AUDIT_DETECTORS_PUBLISHED`` in
``workflows/registry.py``, which is the layer that enforces it."""

MAX_COMPONENT_NODES = 256
MAX_COMPONENT_DEPTH = 12
MAX_VIEW_DATA_SOURCES = 8

# ── capability-filtered context projection ───────────────────────────────────
# Every variable-length projection has both an item count and an aggregate
# UTF-8 byte cap. One without the other is not a bound: 10,000 one-byte
# messages and one 10 MiB message are the same failure in different shapes.

MAX_CTX_HISTORY_MESSAGES = 20
"""Messages in the bounded active-path history window."""

MAX_CTX_HISTORY_BYTES = 32 * KIB
"""Aggregate UTF-8 bytes of the history projection."""

MAX_CTX_TEXT_BYTES = 64 * KIB
"""One projected text field (draft, input, a single history message body)."""

MAX_CTX_CHARACTER_BYTES = 16 * KIB
"""Aggregate UTF-8 bytes of the allowlisted character projection."""

MAX_CTX_DIRECTION_BYTES = 16 * KIB
"""Aggregate UTF-8 bytes of the allowlisted Director-output projection."""

MAX_RENDERED_TEMPLATE_CHARS = 128 * KIB
"""What a ``$template`` may render to, once its holes are filled.

Deliberately larger than ``MAX_TEMPLATE_CHARS``: a template's holes are filled
from projections *the host itself* bounds, and a cap below their sum makes the
host contradict itself -- ``ctx.character`` is offered at up to 16 KiB and then
a flow interpolating it fails on a card the library accepted. This one clears
the declared body plus every distinct projection at once (character, history,
one full text field), so the failure it still catches is the one worth
catching: a template that repeats a large hole until the prompt balloons."""
