# How Orb Reuses the LLM's KV Cache

An LLM builds a KV cache as it reads a prompt. A later request can reuse that
work when its prompt starts with the same tokens. The cache is prefix-based:
changing an early token invalidates everything after it, while appending text at
the end is cheap.

Orb's prompt architecture keeps stable conversation context at the front and
puts turn-specific instructions at the end.

> **Animation:** [KV cache animation](https://orbfrontend.github.io/Orb/architecture/kv-cache-animation.html)
> walks through two turns and the reasoning-mode caveat.

## The prompt shape

Think of a call as a stack. The bottom is shared; only the top changes:

```text
system prompt       ← stable
conversation history ← stable for this exchange
trailing request     ← varies by pass and speaker
```

`CachedBase` captures the stable part once per turn and endpoint. Director,
Writer, Editor, and workflow calls extend that base with their own trailing
messages.

## The three passes

| Pass | Adds to the base | Output |
|---|---|---|
| Director | OOC instruction and the current user message | A forced `direct_scene` tool call |
| Writer | Selected lore, scene direction, and the user message | Streaming prose |
| Editor | The Writer request, the draft, and an audit instruction | Patches or a rewritten draft |

The Editor extends the Writer request instead of rebuilding a new conversation.
Its first call can therefore reuse the Writer's history and draft; later ReAct
iterations only add the newest instruction and tool result.

Director output, keyword lore, and other per-turn values stay in the trailing
request. They must not change the system prompt or the shared history.

## Rules that preserve reuse

### Build the base once

The system prompt and history are assembled once in
`backend/pipeline/config.py` and held in an immutable `CachedBase` from
`backend/inference/cached_call.py`. Passes call `base.complete(...)`; they do
not assemble their own prefix.

Deterministic message and context construction lives in `backend/prompting/`.
Pass-specific instruction prose lives beside its pass under
`backend/pipeline/passes/`; provider and cache execution remain in
`backend/inference/`.

The system prompt includes stable card, persona, scenario, constant lore, and
scene instructions. History is shared byte-for-byte, including attachment
encoding. A macro that changes those bytes, such as an unseeded `{{roll}}`,
breaks reuse; persisted message text is used for later turns.

### Keep group context consistent

In a group, history labels each assistant message with its member. The selected
[character context mode](group-chats.md#character-context) determines whether
card text is in the shared body or in the active speaker's trailing message.
Every call for a speaker uses the same mode and speaker, so the Editor never
audits a draft against a different cast.

Swap mode creates a cache lane per speaker because the active card appears before
history. Private perspective keeps the card after history, leaving one common
trunk. Shared dossier has one common body containing every member's dossier.

The Director runs before a speaker is selected, so it uses the neutral group
base. The first speaker must still rebuild when the mode makes the prefix
speaker-specific.

### Treat tools as part of the prompt

Orb sends a stable, ordered tool list through the base, even to passes that do
not call a tool. The pass selects its behavior with `tool_choice`:

- Director and Editor force one tool.
- Writer uses `tool_choice="none"` so it writes prose.
- Workflow tools add their schemas to the same per-turn list.

`backend/prompting/tool_catalog.py` owns the list. Built-ins are emitted in the
explicit `BUILTIN_TOOL_ORDER`; workflow tools append in registration order, and
re-registering one preserves its position. Schema property order and compact,
insertion-order-preserving JSON bytes are part of the cache contract.

Inference servers may render only the forced tool, or no tools for `none`. As a
result, the three passes can share the conversation body without sharing the
entire rendered prefix. Think in **cache lanes**: each distinct rendered shape
warms and reuses its own lane across turns.

The exact cache hit is provider-specific. Provider `usage` is the source of
truth; Orb's local tracker is only a diagnostic signal.

Endpoints with `structured_tool_calls` use a response schema and omit tool
schemas from every chat request. Native text-completion mode also omits tools
from the prompt and uses a grammar or client-side parsing. These modes keep the
same stable system/history contract with a simpler prefix.

### Separate model lanes

When the Agent uses a different model from the Writer, the models cannot share a
KV cache. The Writer sends no Agent tools; Director and Editor share the Agent
model's base instead.

### Keep optional work on the same path

Feedback, direction notes, and document auditing extend the relevant prompt in
the same way as the Editor. Image prompting rebuilds the neutral scene prefix
through the shared cast resolver, so off-turn calls use the same group history
and context rules as a turn.

## One turn, briefly

For a user message such as `I draw my sword`:

1. Build the base from the system prompt and prior history.
2. Ask the Director for scene direction.
3. Ask the Writer for prose, adding direction and selected lore to the tail.
4. If needed, extend that request with the draft and let the Editor apply fixes.

The first three calls may have different tool-rendered lanes, but each call
keeps its own reusable prefix. The Editor's follow-up calls extend the prompt it
just used.

## Across turns

The next turn appends the saved user and assistant messages to the old history:

```text
turn N:   system + history + writer request
turn N+1: system + history + user N + assistant N + director request
```

That shared beginning is why a long conversation does not require a complete
prefill on every call. A group exchange follows the same rule as speakers add
their replies; lore activation itself remains frozen until the next exchange.

## Reasoning can create another lane

The default `reasoning_enabled_passes` setting keeps Director, Writer, and
Editor in the same reasoning mode. Some providers use separate KV caches for
thinking-on and thinking-off. If the modes differ, the passes still have equal
prompt bytes but cannot reuse one another's provider lane within the turn.

This is a deliberate trade-off when enabled. Keep the setting uniform when
cross-pass reuse matters; each mode will still reuse its own lane across turns.

## What the tracker tells you

Orb records two kinds of cache information:

- **Provider usage:** the number of prompt tokens actually served from cache.
- **Local estimate:** message-prefix overlap and whether the sent tool blob
  changed from the previous same-label call.

The local values cannot know where a provider's template renders tools, so they
are kept separate. A high message overlap with a low provider hit can indicate
different tool rendering or reasoning lanes.

The tracker compares a call with the previous call of the same kind in the
conversation, which makes the first call of a new turn useful rather than an
unhelpful zero baseline.

## In short

Keep system prompt and history stable. Put changing direction, lore, and tool
choices at the tail. Extend an existing prompt for Editor and workflow work.
Use provider usage to judge the result, and expect separate lanes when model,
tool rendering, speaker context, or reasoning mode changes.
