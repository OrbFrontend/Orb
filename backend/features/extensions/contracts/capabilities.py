"""The v1 permission vocabulary and the operation table it gates.

Two frozen tables live here:

* :data:`CAPABILITIES` -- every grant a package may request, with the
  parameter that scopes it. Consent is shown per entry, so the vocabulary is
  also the UI's list of things a user is agreeing to.
* :data:`OPERATION_SPECS` -- every flow operation, the capability it requires,
  the hook contexts it may appear in, and the quota counter it charges.

The compiler derives a package's requirement set from ``OPERATION_SPECS`` by
walking the flows, and then checks that the manifest's declared ``permissions``
covers it. The manifest's claims never *grant* anything; they are checked
against what the code actually reaches. This is why an operation hidden behind
``when: false`` still appears in the consent diff -- validation is conservative
over all reachable branches, because a predicate's value is not knowable at
install time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    """A grant the user approves at install or update time."""

    CONTEXT_INPUT_READ = "context.input.read"
    CONTEXT_DRAFT_READ = "context.draft.read"
    CONTEXT_HISTORY_READ = "context.history.read"
    CONTEXT_CHARACTER_READ = "context.character.read"
    CONVERSATION_TREE_READ = "conversation.tree.read"
    CONVERSATION_TREE_PREVIEWS = "conversation.tree.previews"
    CONVERSATION_BRANCH_ACTIVATE = "conversation.branch.activate"
    PROMPT_CONTEXT_APPEND = "prompt.context.append"
    DRAFT_REPLACE = "draft.replace"
    MODEL_CALL = "model.call"
    STATE_READ = "state.read"
    STATE_WRITE = "state.write"
    ARTIFACT_WRITE = "artifact.write"
    NETWORK_REQUEST = "network.request"
    UI_CONTRIBUTE = "ui.contribute"
    FRAGMENT_TYPE_CONTRIBUTE = "fragment_type.contribute"


class OpContext(StrEnum):
    """Where a flow runs, which decides what it may do.

    ``POST_OBSERVE`` is not ``POST_TRANSFORM`` with a flag: an observer sees the
    final immutable draft, so ``draft.replace`` is absent from its allowlist
    rather than checked at runtime. ``REDUCER`` is the strictest profile -- a
    fragment reducer is a pure function from (config, previous, director
    output) to the next value.
    """

    PRE_PIPELINE = "pre_pipeline"
    POST_TRANSFORM = "post_transform"
    POST_OBSERVE = "post_observe"
    ACTION = "action"
    REDUCER = "reducer"


HOOK_CONTEXTS = frozenset({OpContext.PRE_PIPELINE, OpContext.POST_TRANSFORM, OpContext.POST_OBSERVE})
IMPURE_CONTEXTS = frozenset(HOOK_CONTEXTS | {OpContext.ACTION})
ALL_CONTEXTS = frozenset(OpContext)
PURE_CONTEXTS = frozenset(IMPURE_CONTEXTS | {OpContext.REDUCER})


class Quota(StrEnum):
    """The per-invocation counter an operation charges, beyond the step count."""

    MODEL_CALL = "model_calls"
    HTTP_REQUEST = "http_requests"
    STATE_WRITE = "state_writes"
    CONTEXT_BLOCK = "context_blocks"
    DRAFT_REPLACE = "draft_replacements"
    BRANCH_ACTIVATE = "branch_activations"
    ARTIFACT = "artifacts"


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


def _spec(
    capability: Capability | None,
    contexts: frozenset[OpContext],
    *,
    output: bool = False,
    quota: Quota | None = None,
    staged: bool = False,
) -> OperationSpec:
    return OperationSpec(capability, contexts, output, quota, staged)


OPERATION_SPECS: dict[str, OperationSpec] = {
    # ── data and deterministic transforms ────────────────────────────────────
    "state.get": _spec(Capability.STATE_READ, IMPURE_CONTEXTS, output=True),
    "state.set": _spec(Capability.STATE_WRITE, IMPURE_CONTEXTS, quota=Quota.STATE_WRITE, staged=True),
    "state.delete": _spec(Capability.STATE_WRITE, IMPURE_CONTEXTS, quota=Quota.STATE_WRITE, staged=True),
    "text.concat": _spec(None, PURE_CONTEXTS, output=True),
    "text.replace_literal": _spec(None, PURE_CONTEXTS, output=True),
    "json.pick": _spec(None, PURE_CONTEXTS, output=True),
    "json.merge": _spec(None, PURE_CONTEXTS, output=True),
    "math.add": _spec(None, PURE_CONTEXTS, output=True),
    "math.subtract": _spec(None, PURE_CONTEXTS, output=True),
    "math.negate": _spec(None, PURE_CONTEXTS, output=True),
    "math.clamp": _spec(None, PURE_CONTEXTS, output=True),
    # Randomness is host-owned and per-invocation seeded, so it is deterministic
    # for a given invocation but not for a reducer, which must replay identically
    # when a branch is rewound.
    "random.integer": _spec(None, IMPURE_CONTEXTS, output=True),
    "random.choice": _spec(None, IMPURE_CONTEXTS, output=True),
    "if": _spec(None, PURE_CONTEXTS),
    "return": _spec(None, PURE_CONTEXTS),
    # ── host capabilities ────────────────────────────────────────────────────
    "model.text": _spec(Capability.MODEL_CALL, IMPURE_CONTEXTS, output=True, quota=Quota.MODEL_CALL),
    "model.structured": _spec(Capability.MODEL_CALL, IMPURE_CONTEXTS, output=True, quota=Quota.MODEL_CALL),
    "http.request": _spec(
        Capability.NETWORK_REQUEST,
        IMPURE_CONTEXTS,
        output=True,
        quota=Quota.HTTP_REQUEST,
    ),
    "context.append": _spec(
        Capability.PROMPT_CONTEXT_APPEND,
        frozenset({OpContext.PRE_PIPELINE}),
        quota=Quota.CONTEXT_BLOCK,
        staged=True,
    ),
    "draft.replace": _spec(
        Capability.DRAFT_REPLACE,
        frozenset({OpContext.POST_TRANSFORM}),
        quota=Quota.DRAFT_REPLACE,
        staged=True,
    ),
    "artifact.emit": _spec(
        Capability.ARTIFACT_WRITE,
        frozenset({OpContext.POST_TRANSFORM, OpContext.POST_OBSERVE, OpContext.ACTION}),
        quota=Quota.ARTIFACT,
        staged=True,
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
