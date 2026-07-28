# Community Extensions

Orb can be extended by third-party packages that are **data, not code**. A
package is a manifest, some declarative flows, some component trees, and a few
inert assets. Orb reads them, validates them against a frozen contract, and
interprets them itself. Nothing in a package is ever imported, evaluated, or
executed — there is no JavaScript entry point, no Python hook, no install
script, no build step.

This page is the whole contract: what an author writes, what the host
guarantees, and what to change when the ABI itself grows. Skip to
[Extending the ABI](#extending-the-abi) if you are adding a capability, an
operation, a component, a resource, or an API version.

## Two trust tiers

| | Built-in workflows | Community extensions |
|---|---|---|
| Lives in | `backend/workflows/**`, `frontend/workflows/**` | `data/extensions/objects/<digest>/`, installed by the user |
| Written in | Python + same-origin ES modules | JSON that Orb interprets |
| Reviewed by | Orb's own code review | nobody — the host is the only boundary |
| Gets | rich context objects, its own SSE events | a capability-filtered JSON context, fixed host events |

The two share a lifecycle — registry, enablement, state slots, locks,
artifacts — and nothing else. One community extension maps to one workflow ID,
and its config plus conversation/message/character state live under that one
namespace. See [Secondary Workflows](secondary-workflow.md) for the trusted
tier.

**What "no RCE surface" claims.** There is no intentional primitive that
interprets a package file as host or browser code, launches a package-selected
process, or hands a package an object capable of doing so. It is not a claim
that parsers, Git clients, media decoders, or Orb itself can never contain a
vulnerability — those inputs still need bounds, fuzzing, and dependency
updates.

---

## Quick start

Zip the package directory and install it from Settings → Extensions:

```sh
zip -r my-extension.orbext my-extension/
```

One wrapping directory is stripped, so both `my-extension/orb-extension.json`
and a root-level `orb-extension.json` work. An HTTPS Git URL installs the same
way. Orb inspects first and shows exactly what the package will be allowed to
do; nothing is installed until you approve that list.

Reference packages live in `tests/extension_packages.py`, so the package you
install by hand is the package the test suite asserts against.
`./scripts/build_example_extensions.py` writes three of them to
`dist/extensions/`:

| Package | Demonstrates |
|---|---|
| **Conversation Map** | command placement, the full-tree resource, a workspace view, branch activation |
| **Scene Meter** | a post hook and an action, plus a contributed Meter fragment type with config and value views |
| **Tag Librarian** | the library resource, a user-managed vocabulary, the renderer-driven sweep, a first-party card write |

Two more fixtures in the same module round out the surface: `api_artifact_package`
(origin consent, a secret header, artifact regenerate/reroll) and
`outcome_resolver_package` (the API 2 Writer tool).

---

## Package format

```text
orb-extension.json      ← the only mandatory file
flows/
  score-scene.json
ui/
  inspector.json
assets/
  icon.webp
README.md
LICENSE
```

Only files the manifest **references** are compiled, stored, or served. A
repository may keep TypeScript sources, a `package.json`, or a build directory;
Orb never materializes them. Authors may generate package JSON with any tool —
Orb consumes only the resulting validated JSON and inert assets, and installs no
author runtime, lockfile, or dependency.

### Paths

Referenced paths are relative, normalized, UTF-8, and contained by the package
root. They are **case-sensitive** even on a case-insensitive filesystem, and two
paths that differ only by case are rejected for the whole package.

### Content digest

Every revision has a SHA-256 content digest over a versioned domain separator
followed by each selected normalized path in UTF-8 byte order and its
length-delimited canonical content. JSON is hashed in Orb's canonical encoding
(sorted keys, no whitespace); assets are hashed as exact bytes. So reformatting
a flow file does not change the package's identity, but changing a value does.
The digest is the revision's identity in the content store, what an update
compares against, and what Git inspection, archive inspection, install
revalidation, and startup all recompute.

### The manifest

```json
{
  "extension_api": 1,
  "id": "scene-meter",
  "name": "Scene Meter",
  "version": "1.0.0",
  "author": "Example Author",
  "description": "Tracks and displays scene tension.",
  "homepage": "https://example.invalid/scene-meter",
  "requires": {
    "operations": ["model.structured", "state.set", "ui.invalidate"],
    "components": ["meter", "stack", "text"]
  },
  "permissions": [
    { "capability": "context.read", "field": "draft" },
    { "capability": "model.call", "lane": "agent" },
    { "capability": "state.read", "scope": "conversation" },
    { "capability": "state.write", "scope": "conversation" },
    { "capability": "ui.contribute", "slot": "inspector" }
  ],
  "hooks": {
    "post_pipeline": { "flow": "flows/score-scene.json", "stage": "observe" }
  },
  "views": {
    "inspector": { "source": "ui/inspector.json" }
  },
  "placements": [
    { "slot": "inspector", "view": "inspector" }
  ]
}
```

`id` uses the lowercase grammar `[a-z0-9][a-z0-9_-]{0,63}` — the same one
fragments and cards use. It is the package's namespace: its state, its config,
its views, and its purge target all live under it. It may not collide with a
built-in workflow, the reserved `macros` message-state slot, or another
installed package.

Other top-level keys: `secrets`, `actions`, `commands`, `produces_artifacts` +
`artifact_flows`, and `contributions`. Each is covered in its own section
below.

### `extension_api`

The compatibility boundary. `1` is the base contract; `2` adds exactly one
thing, a Writer-tool contribution. Declare the lowest version that covers what
you use.

**Versions are a table, not a literal.** `SUPPORTED_EXTENSION_APIS` says which
versions this build implements (`{1, 2}`) and `CONTRIBUTION_MIN_API` says which
version each `contributions` slot was introduced in (`writer_tool` → 2). A v1
manifest declaring a v2 slot is refused with *"contributions.'writer_tool'
requires extension_api 2; this manifest declares 1"* rather than being silently
upgraded, so v1 keeps meaning exactly what it meant. The compiler reads the raw
`extension_api` integer **before** strict parsing, which is what lets an older
build say *"this package needs a newer Orb"* instead of *"this package is
malformed"* — every model here forbids extra fields, so without that ordering a
future manifest would look broken rather than new.

### `requires` is checked, not trusted

The compiler walks the flows and views and derives the real requirement set
itself. The declared `requires.operations`, `requires.components`, and
`permissions` must **cover** that derived set:

- Under-declaring is a validation error at install time, naming exactly which
  operation, component, or grant is missing.
- Declaring something unknown to this build leaves the package installed but
  *unavailable*, with a diagnostic. That is the feature-detection mechanism.
- Putting a privileged operation behind `"when": false` does not hide it. All
  reachable branches are walked conservatively, because a predicate's value is
  not knowable at install time.

---

## Permissions

The unit of consent is a **grant**: a `(capability, parameter)` pair.
`state.write` on `conversation` and `state.write` on `character` are two
grants, approved and revoked independently. Where a capability is scoped by
something, that scope is a **parameter**, never a suffix on the name — which is
why there is no `context.draft.read`.

| Capability | Parameter | What it allows |
|---|---|---|
| `context.read` | `field`: `input` | Read the effective user message for the turn. |
| `context.read` | `field`: `draft` | Read the post-writer draft. |
| `context.read` | `field`: `history` | Read a bounded active-path history window. |
| `context.read` | `field`: `character` | Read an allowlisted character text projection plus its tags. |
| `context.read` | `field`: `direction` | Read this turn's scene direction: active moods, the Scene Direction text, and the reduced fragment map. |
| `context.read` | `field`: `persona` | Read the active persona's name and description. Also gates the `persona` resource. |
| `conversation.tree.read` | `field`: `structure` | Read message-node metadata for every branch. Gates the `conversation.tree` resource. |
| `conversation.tree.read` | `field`: `preview` | Also read previews from inactive branches. Requires `structure`. |
| `library.cards.read` | — | Enumerate the card library, and resolve a card named by action input into `ctx.character`. |
| `lorebook.read` | — | Read the lorebook of the conversation's world, including untriggered entries. |
| `direction_notes.read` | — | Read the active branch's direction notes. |
| `conversation.branch.activate` | — | Change the active branch. Actions only. |
| `prompt.context.append` | `targets`: `director`, `writer` | Add a per-turn trailing context block. |
| `draft.replace` | — | Replace the draft once, in a post-transform hook. |
| `card.write` | `field`: `tags` | Replace the tags of the one card in the invocation's context. Requires `context.read` for `character`. |
| `model.call` | `lane`: `writer`, `agent` | Make bounded flow-owned model calls on that endpoint. Costs tokens. |
| `state.read` / `state.write` | `scope`: `config`, `conversation`, `message`, `character` | Access **this extension's own** namespaced slot in that scope. |
| `artifact.write` | — | Emit workflow attachments. |
| `network.request` | `origin` | Reach one exact declared origin through Orb's client. |
| `ui.contribute` | `slot` | Place a command or view in one exact slot. |
| `fragment_type.contribute` | — | Register namespaced fragment types. |
| `writer.tool.contribute` | — | Contribute the one Writer tool (API 2). |
| `audit.detector.contribute` | — | Contribute checks to the Output Auditor (API 3). Reads every draft before you see it and can steer a rewrite. |

`CAPABILITY_SPECS` in `features/extensions/contracts/capabilities.py` is the one
place any of this is written down. Each entry carries its consent copy, the
class of user data it exposes, its emphasis, the parameter that scopes it and
that parameter's admissible values, the host resource it gates, and its
prerequisites. The permission model's admissible parameters, the resource map,
`UI_SLOTS`, the consent copy, the loud set, and the combination banner's read
set are all *derived* from it.

Two rules follow from the parameter being part of the grant, and both are
enforced rather than documented:

- **Prerequisites are per grant.** `GRANT_PREREQUISITES` is keyed by the pair.
  `card.write` needs the character projection only for its `tags` field; tree
  previews need tree structure while no other `conversation.tree.read` value
  needs anything. The compiler resolves them transitively, so an operation's
  derivation states only what it directly reaches.
- **A multi-valued parameter makes the whole entry the unit of approval.** Use
  one only where partial approval is meaningless — `prompt.context.append`
  takes a list of `targets`, while `conversation.tree.read` takes a *singular*
  `field` so previews stay separately revocable.

### Consent surface

Consent controls and diagnostics are host components populated from server
inspection results, never package views. The frontend sends the opaque staging
token and the exact normalized grants the user approved; it never reconstructs
permissions from display strings, and a package can never request or approve a
permission through one of its own actions or views.

Whenever the approved set contains `network.request` together with any grant
that reads conversation, character, lorebook, persona, or history data, the
consent diff and the detail panel show one fixed host-authored **combination
banner**: data the package can read can be sent to its named origins. It is
derived server-side from the normalized grant set, and package strings never
influence it.

### Revocation

Grants can be revoked at any time from the manager. Revocation takes effect
**immediately**, including in the middle of a running flow — the interpreter
re-checks the live grant view immediately before every privileged operation, so
the next one fails. Entry points whose transitive requirements are no longer
covered stop being published and are listed in the package's diagnostic instead
of failing halfway through ordinary use. Reduction takes effect at once;
expansion is always an explicit consent action.

### UI slots

`composer.menu`, `mobile.chat_actions`, `tools`, `inspector`,
`message.toolbar`, `message.after`, `artifact.body`, `workspace`,
`library.card_actions`.

A package names a slot; it never names a selector, an element, or a position.

---

## What a flow can see

Flows never receive Orb's trusted workflow context objects — those carry
endpoint URLs, API keys, and proxy settings. The adapter builds a plain JSON
`ExtensionCtx` containing only granted fields, deep-copied and bounded:

```json
{
  "extension_id": "scene-meter",
  "hook": "post_pipeline",
  "conversation": { "id": "..." },
  "message": { "id": 42 },
  "character": { "id": "...", "name": "..." },
  "input": { "last_user_message": "..." },
  "draft": "...",
  "history": []
}
```

Every variable-length projection has both an item count and an aggregate byte
cap: history is 20 messages / 32 KiB, one text field is 64 KiB, the character
projection is 16 KiB, the persona projection is 8 KiB.

History carries role and bounded text only — no attachments, workflow state,
progressive fields, logs, reasoning, or inactive messages. The character
projection is an explicit allowlist of textual card fields plus the card's
current `tags`; it excludes avatar bytes, raw card extensions, endpoint
settings, and persona data.

### How a card reaches `ctx.character`

In a hook, `ctx.character` is the card bound to the turn's conversation. An
action has no such binding, so there are two host-driven ways to fill it:

- **Action input declares a card identifier.** Orb resolves that card through
  the same allowlist. This needs **both** `context.read` for `character` and
  `library.cards.read` — an extension holding an id from anywhere could
  otherwise read any card in the library under a grant whose consent line says
  "the character in this conversation".
- **A `library.card_actions` placement is clicked.** The host supplies the
  card id from the click, so `library.cards.read` is *not* required; reach is
  still exactly one user-chosen card.

Either way the resolution also rebinds the `character` state scope to that card,
so `state.get`/`state.set` with `scope: "character"` address the resolved card's
slot. This is the only place package input selects an entity.

---

## Flows

A flow is a bounded sequence of host operations. There are no user-defined
functions, imports, recursion, cycles, or loops.

```json
{
  "flow_version": 1,
  "steps": [
    {
      "id": "score",
      "op": "model.structured",
      "lane": "agent",
      "prompt": { "$template": "Rate scene tension from 0 to 100.\n\n{{ctx.draft}}" },
      "output_schema": {
        "type": "object",
        "properties": { "tension": { "type": "integer", "minimum": 0, "maximum": 100 } },
        "required": ["tension"],
        "additionalProperties": false
      }
    },
    { "op": "state.set", "scope": "conversation", "path": "tension", "value": { "$ref": "steps.score.tension" } },
    { "op": "ui.invalidate", "view": "inspector" }
  ]
}
```

### Values

- `{"$ref": "ctx.draft"}` — read a value by validated path.
- `{"$template": "…{{ctx.draft}}…"}` — substitute **scalar** path values.
- Plain JSON literals, and arrays/objects containing either of the above.

Templates are path substitution and nothing else: no filters, no property
access beyond the resolver, no calls, loops, includes, or expressions.
Interpolating an array fails — use `list.join`, whose separator comes from a
closed host-owned set. Do not fix a formatting need by giving templates a format
argument; that is where a filter language starts.

Readable namespaces are `ctx` (whatever the grants project), `input` (validated
action input), `host` (invocation metadata), `steps` (earlier named steps), and
`fragment` (reducer flows only). Step ids are unique within a flow and forward
references are rejected.

A path that resolves to nothing becomes a distinct `missing` sentinel rather
than `null`, so `exists` is unambiguous. The sentinel can be tested or replaced
by a `fallback`, but it can never be returned, stored, interpolated, or passed
to a host operation. Bounded HTTP bytes and package assets are opaque host
handles: they flow only into operations that declare them, and cannot be
templated or stored.

### Conditions

`when` takes a structured predicate AST: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`,
`exists`, `and`, `or`, `not`. Depth is capped at 8. Comparisons are type-strict
and total — an incomparable pair is `false`, never an error.

### Operations, and where each may run

`OPERATION_SPECS` (same module as the capabilities) maps every operation to the
capability it consumes, the contexts it may appear in, the quota it charges, and
whether its effect is staged.

| Operation | Capability | Allowed in |
|---|---|---|
| `if`, `return`, `text.concat`, `text.replace_literal`, `json.pick`, `json.merge`, `list.intersect`, `list.join`, `math.add`, `math.subtract`, `math.negate`, `math.clamp` | — | everywhere, including reducers |
| `random.integer`, `random.choice` | — | pre, post, action, recovery, writer tool, detector |
| `state.get` | `state.read` (`scope`) | pre, post, action, recovery, writer tool, detector |
| `state.set`, `state.delete` | `state.write` (`scope`) | pre, post, action, recovery, writer tool, detector |
| `model.text`, `model.structured` | `model.call` (`lane`) | pre, post, action, recovery, writer tool, detector |
| `http.request` | `network.request` (origin) | pre, post, action, recovery, writer tool, detector |
| `ui.status`, `ui.toast` | — | pre, post, action, recovery |
| `ui.invalidate` | `ui.contribute` | pre, post, action, recovery |
| `artifact.emit` | `artifact.write` | post, action, recovery |
| `context.append` | `prompt.context.append` (`targets`) | pre-pipeline only |
| `draft.replace` | `draft.replace` | post-transform only |
| `card.tags.set` | `card.write` for `tags` | actions only |
| `conversation.branch.activate` | `conversation.branch.activate` | actions only |

The eight contexts are `pre_pipeline`, `post_transform`, `post_observe`,
`action`, `recovery`, `reducer`, `writer_tool`, and `detector`. A **reducer** is
the strictest profile — a pure function from (config, previous, Director output)
to the next value. A **Writer tool** and a **detector** share the next profile
and share the reason for it: both run inside an unfinished turn with no user
click and no assistant row, so both get `EXTERNAL_CONTEXTS` (model calls, state,
HTTP) and neither gets `IMPURE_CONTEXTS` — no UI, no draft replacement, no
artifacts, no first-party writes, and no message-scoped state. **Recovery** is
the regenerate/reroll pair an artifact producer declares; the framework already
knows which attachment is being rebuilt, so a recovery flow names no target
message and cannot activate a branch or rewrite tags on the way.

### Operation notes

- **`model.text` / `model.structured`** use the selected lane's transport,
  credentials, model, and parameters — but **not** Orb's system prompt,
  conversation prefix, attachments, or cached tools. The request is a small
  host-owned safety preamble plus the flow-produced prompt, so conversation data
  reaches it only through granted `ctx` fields the flow interpolates itself.
  `model.structured` validates the decoded value against the compiled local
  schema; it registers no function tool.
- **`state.*`** addresses only this extension's own namespaced slot. Paths are
  validated segments, never raw SQLite JSON paths. The target entity must exist
  in the invocation's context; a post-turn write to the not-yet-created
  assistant message is staged and commits with that message.
- **`list.intersect`** returns the members of one bounded string array that also
  appear in another, in first-array order, deduplicated. It exists because a
  package's `output_schema` compiles at install time and so cannot carry an
  `enum` drawn from runtime config — it is how a model result gets constrained
  to a user-managed vocabulary. It and `list.join` perform no per-element
  package logic, which is why they are not the seed of a collection library.
- **`card.tags.set`** takes no card argument. It writes `ctx.character` and only
  that card, so its blast radius is one card by construction rather than by
  quota. The host — not the package — normalizes the result (trim, drop empties,
  case-insensitive dedupe, per-tag length and per-card count caps) through
  `core/tags.py`, the same path the character API uses. It is action-only: a
  card is shared across every conversation that uses it, so a per-turn hook
  silently rewriting library metadata is not something a user can supervise.
- **`http.request`** goes to an exact declared origin through Orb's client,
  which validates every resolved address, follows redirects itself, and drops
  package headers on a cross-origin hop. Bodies may reference
  `{"$secret": name}`; the value is substituted inside the client and never
  becomes a flow value.
- **`random.*`** use a host-owned per-invocation seeded PRNG, never module
  randomness. The seed derives from host invocation identity plus extension id,
  flow id, and content digest. Regenerate replays the seed; reroll gets a new
  one.
- **`return`** ends execution and validates its value against the entry point's
  declared output schema. Falling off the end returns `null`.
- **`on_error`** defaults to `stop`. A step may declare `on_error: "continue"`
  only if it also declares a `fallback`, so later steps never read an undefined
  output.

### The invocation transaction

One invocation is prepare/commit:

1. Validate action input, derive the entity and state scopes the compiled flow
   may touch, and acquire their locks in the canonical host order — conversation
   stream (when required), workflow config, conversation state, character state,
   message state.
2. Resolve the capability-filtered context and state snapshot under those locks.
3. Execute steps with quotas and cancellation, staging every Orb-owned effect.
4. On successful return only, revalidate the complete staged set and commit it
   under the owning locks and transaction.
5. Publish fixed host effects and invalidations after commit.

Staged effects are state writes and deletes, draft replacement, context blocks,
attachments, branch activation, card tags, view invalidations, and success
toasts. A failure at any step discards all of them. `ui.status` streams while
work runs and is always cleared.

Model calls and HTTP requests are external and cannot be rolled back. They still
cannot leave a partial Orb mutation behind: a flow that stages a state write and
then gets a bad HTTP response leaves the prior state untouched. Holding the
per-extension locks from read through commit is what preserves
read-modify-write atomicity across a slow model step; it serializes only the
same extension and entity keys.

A flow containing branch activation cannot contain model or HTTP operations. The
action takes the conversation stream lock before its state locks and stays
short, matching the pipeline's stream-before-workflow-lock order so a branch
action and a running turn cannot deadlock.

### Failure

Cancellation follows the owning turn, action request, or client disconnect. A
hook failure aborts that extension's invocation and is logged — it does not
abort the turn, and it does not erase earlier successful transforms. An explicit
UI action gets a sanitized error response. Repeated failures never auto-grant,
auto-update, or crash the process.

---

## Entry points

### Hooks

- `pre_pipeline` — runs before the turn. Can add trailing context blocks and
  write state. Cannot see or replace a draft.
- `post_pipeline` with `"stage": "transform"` — sees the draft and may replace
  it once. Each successful transform feeds the next.
- `post_pipeline` with `"stage": "observe"` — sees the final immutable draft.

Post processing runs every transform, then every observer. Within a stage,
trusted built-ins are the first band and keep their declared priority; community
entries form the second band and sort by priority then extension id, so ordering
never depends on install time. Stage precedes band, so every community transform
runs before every trusted observer. (Among built-ins, format consistency is a
transform and consumers such as TTS are observers.)

`context.append` blocks land in the current turn's trailing message — beside
lorebook and Scene Direction context — so they never invalidate cached history.
The pipeline collects them in extension-id order; Director blocks go with the
per-turn Director context before the host's tool instruction, Writer blocks
before the effective user message. `build_writer_content()` still runs once, so
the Editor replays the exact augmented Writer message. An extension whose blocks
overflow 8 KiB each or 32 KiB per target is rejected with a diagnostic rather
than truncated. See [KV Cache Reuse](kv-cache.md).

### Actions

Named on-demand entry points, invoked by a command, a button in a view, or a
card action. They may declare `input_schema` and `output_schema` from the closed
local JSON Schema subset.

### Commands and placements

A command separates the action from where it appears:

```json
{
  "commands": [
    {
      "id": "open-tree",
      "label": "Conversation Map",
      "icon": "git-branch",
      "opens": "conversation-tree",
      "when": { "exists": { "$ref": "host.active_conversation_id" } }
    }
  ],
  "placements": [
    { "slot": "composer.menu", "command": "open-tree" }
  ]
}
```

Icons are Orb-owned symbolic names, not asset URLs or markup. Availability
predicates read a small host state projection and cannot call flow operations.
The composer burger, the mobile action menu, and the library card actions all
render from one host command model in `frontend/extension_commands.js`: built-in
entries first, then enabled extension placements ordered by extension id. Other
slots are empty `[data-ext-slot]` containers the host puts in its own markup and
`renderPanelSlots(root)` fills. Extensions never insert DOM nodes.

### Artifacts

A package that emits attachments declares `produces_artifacts: true` plus
`artifact_flows` with both `regenerate` and `reroll_gen`, preserving the existing
workflow mandate. `artifact.emit` accepts bytes the interpreter already holds:
a bounded HTTP response, a package asset, or UTF-8 text or JSON from a prior
step. In a post hook it attaches to the assistant message being persisted; in an
action it requires a target message already validated to belong to that
conversation. Recovery metadata, byte-budget eviction, sibling grouping, and
validation stay framework-owned, and the metadata additionally records the
producing extension version and digest — regenerate and reroll run the revision
captured when the request resolved its snapshot. If an update no longer accepts
old metadata the operation fails with an "incompatible revision" diagnostic and
leaves the attachment untouched; Orb never runs an old revision under new
grants.

---

## Views

A view is a component tree Orb renders with DOM creation and `textContent`. A
package never produces markup, a class name, a selector, a URL, or an event
handler; every package string becomes text. Unknown properties fail validation
rather than becoming DOM attributes, and styling is tokenized (`tone`, `size`,
`density`, `columns`, `align`, `span`).

Components: `stack`, `grid`, `card`, `divider`, `tabs`, `text`, `markdown`,
`badge`, `list`, `table`, `tree`, `conversation-tree`, `library-sweep`, `image`,
`audio`, `video`, `button`, `text-input`, `textarea`, `number-input`, `select`,
`toggle`, `progress`, `meter`, `empty-state`, `error`.

### Data sources

A view's `data` map names up to 8 sources, each either this extension's own
state in one scope or a host **resource**:

| Resource | Grant | Contents |
|---|---|---|
| `conversation.tree` | `conversation.tree.read` for `structure` | every node's id, parent, role, turn index, timestamp, child count, plus the active path; previews only with the `preview` grant |
| `library.cards` | `library.cards.read` | id, name, tags, plus this extension's own namespaced card slot — present only with `state.read` for `character`, so "not granted" and "no record yet" stay distinguishable |
| `lorebook.entries` | `lorebook.read` | id, keys, secondary keys, selective, use_regex, enabled, insertion order, bounded content |
| `direction.notes` | `direction_notes.read` | id, content, author kind, timestamps |
| `persona` | `context.read` for `persona` | the active persona's name and description |

A view inherits the requirements of everything it references, so a view reaching
an ungranted resource or action is blocked rather than rendered empty. Every
resource is an allowlisted projection served by a host adapter over a database
query — never a query primitive handed to a flow — and every one is bounded by
both an item count and an encoded-byte budget.

The tree fails past its budget with `resource_too_large` (2000 nodes) because a
partial graph looks complete. Everything else paginates and hands back an
**opaque cursor**: pass it back unchanged, because a cursor a package
constructs or edits is rejected. The library walk pins a rowid high-water mark
on the first page, so cards added mid-sweep wait for the next run; the lorebook
and direction-note walks use monotonic ids, so later entries may appear but
surviving entries never shift or repeat.

### Interaction

Buttons dispatch named actions. Tabs and disclosure panels update ephemeral
renderer state with no backend round trip. Opening or refreshing a view never
runs an action, and merely rendering a view never writes state.

Form controls bind only to declared config or state paths and keep an ephemeral
draft until submit; submission runs a host-generated state write under the same
grant check, schema, size cap, lock order, and transaction a flow's `state.set`
uses. A binding also declares the *shape* it stores: `textarea` takes
`value_kind` (`text` or `lines`), and under `lines` the host splits the box into
a trimmed array of non-empty members and renders a stored array back one per
line. Without it a list-shaped setting would save a string while the flow
reading the same key runs `list.join` over it.

`library-sweep` exists because the loop belongs to the host: the package gives
an action id, a label, and the state key that marks a card done, and Orb owns
the page size, cursor walk, progress display, and every stop condition. Its
concurrency is one at a time — a deliberate floor, since each card costs a model
call on a user-configured lane and there is no rate-limit model for those lanes.
A failed card stops the sweep rather than skipping on, because the likely cause
is a revoked grant or an unreachable lane and 200 more failures would bury it,
and a `runtime_generation` change mid-sweep halts it and reports the count
completed rather than committing writes whose envelopes the view would discard.

`conversation-tree` likewise computes its own layout and connectors. It folds
each chain of single-child messages into one row with a `⋯N` chip, opens
subtrees off the active path collapsed, and never folds past the message the
user is on — a package cannot opt out, which is what keeps the map a map of
branch points rather than one row per turn.

The renderer keys ephemeral state by
`(extension_id, active_digest, view_id, instance_id)` and drops it when the
digest or view disappears.

### `views.config`

Name a view `config` and the Orb-owned manager renders it in the package's
detail panel — no slot, placement, or `ui.contribute` grant, because the surface
is the manager the user opened deliberately. A config view may bind form
controls only to the `config` scope; declaring another scope fails compilation.

### Assets

Referenced assets must be inert media or text: `.png`, `.jpg`, `.jpeg`,
`.webp`, `.gif`, `.mp3`, `.ogg`, `.wav`, `.m4a`, `.mp4`, `.webm`, `.txt`,
`.md`. Rejected outright: `.html`, `.htm`, `.xhtml`, `.js`, `.mjs`, `.cjs`,
`.css`, `.svg`, `.pdf`, `.wasm`, and browser fonts.

The check is two-sided: the declared extension decides which media type a path
is allowed to be, and the leading bytes have to agree. Assets are served from an
API route that resolves an exact compiled asset key to a digest-owned
descriptor — never a path join, never a directory mounted under `/static` — with
the recorded type and `X-Content-Type-Options: nosniff`.

### The effect envelope

Packages cannot invent SSE event names or frontend callbacks. Every action, view,
and lifecycle response uses one envelope:

```json
{
  "data": {},
  "effects": [
    { "resource": "conversation.messages", "conversation_id": "conv-1" },
    { "resource": "extension.view", "extension_id": "scene-meter", "view": "inspector" }
  ],
  "toasts": [],
  "runtime_generation": 12
}
```

The frontend owns the effect-to-refetch mapping for messages, Director state,
direction notes, character cards, extension views, and the catalog, and the same
effects drive cross-tab broadcast. Coalescing is the frontend's business too — an
effect describes what one invocation did, so a host-rendered loop may produce one
per iteration and the mapping debounces. Unknown effects are dropped and logged.
`toasts` is a bounded list of host-rendered `{text, tone}` values from
`ui.toast`. Hooks and actions also ride two fixed SSE events,
`extension_status` and `extension_effects`; see
[SSE Turn Stream](sse-stream.md).

---

## Contributions

### Fragment types

With `fragment_type.contribute`, a package declares namespaced Director fragment
types under `contributions.fragment_types`. A descriptor may participate only in
this fixed lifecycle: render extra instance configuration, contribute one
validated property schema to Orb's existing `direct_scene` tool, add prior
branch state to the Director's trailing request, reduce the Director's output
through a declarative flow, persist that value on the produced assistant
message, add a formatted value to the Writer's Scene Direction context, and
render the current value. It cannot add a model tool, a pipeline pass, a query,
or a persistence location.

```json
{
  "contributions": {
    "fragment_types": [
      {
        "id": "meter",
        "label": "Meter",
        "storage": "assistant_progressive",
        "config_schema": {
          "type": "object",
          "properties": {
            "minimum": { "type": "integer" },
            "maximum": { "type": "integer" },
            "initial": { "type": "integer" },
            "max_delta": { "type": "integer", "minimum": 1, "maximum": 100 }
          },
          "required": ["minimum", "maximum", "initial", "max_delta"],
          "additionalProperties": false
        },
        "director_schema": {
          "type": "object",
          "properties": {
            "delta": { "type": "integer", "minimum": { "$neg_config": "max_delta" }, "maximum": { "$config": "max_delta" } },
            "reason": { "type": "string", "maxLength": 160 }
          },
          "required": ["delta", "reason"],
          "additionalProperties": false
        },
        "prior_context": { "$template": "{{fragment.injection_label}} is currently {{fragment.previous.value}}." },
        "reduce_flow": "flows/reduce-meter.json",
        "writer_context": { "$template": "{{fragment.injection_label}}: {{fragment.current.value}} — {{fragment.current.reason}}" },
        "config_view": "ui/meter-config.json",
        "value_view": "ui/meter.json"
      }
    ]
  }
}
```

`$config` and `$neg_config` are the only schema-template forms. They fill
numeric JSON Schema keywords from validated integer instance config, and the
resolved schema is validated before it enters the per-turn override map. When
validated config carries a top-level `initial` member, the host seeds
`fragment.previous` as `{"value": config.initial}`; a descriptor without one has
no host-created prior.

`assistant_progressive` stores the reduced value in
`messages.progressive_fields[fragment.id]`. Note that a core progressive
fragment's Director output *is* the persisted value, while an extension type
breaks that identity on purpose: the Director's raw output is reducer *input*,
and the reducer's return is what persists. Branch rewind and regeneration stay
keyed on the post-reduce value either way.

The stored `field_type` is `<extension-id>:<type-id>`. `type_config` on the
instance holds the validated per-instance config, and card-embedded fragments
may carry the same object.

**When a provider is missing.** Namespaced unknown types are preserved in stored
and card JSON, marked unavailable in editing UI with the missing extension
named, and skipped in pipeline assembly — never coerced to `string`, which would
silently change the Director schema and Writer injection on uninstall.
Reinstalling revalidates and reactivates them. Installed-but-invalid instances
behave the same way: the editor shows the error and the pipeline skips them
without rewriting storage. Creating or editing a global fragment with an
unavailable type is rejected; existing and imported values stay preserved. At
most 50 active extension-backed instances resolve per turn; the rest are
diagnosed and skipped in deterministic order. (The fallback-to-`string` rule
survives only for malformed, non-namespaced legacy card values.)

**Per turn:** capture the snapshot; merge global and card fragments with the
existing global-id precedence; resolve each field type and validate
`type_config`; build every dynamic tool schema once from the resulting immutable
set; validate Director values and run the pure reducers after the Director call;
persist all normalized values, including unchanged ones; render Writer context
from those same values. If a Director value or a reducer fails, the validated
prior (or configured initial) value carries forward, a sanitized diagnostic is
recorded, and the turn continues — carry-forward is still persisted, or the next
branch baseline would lose it. If the provider is gone entirely, a bounded value
already stored under that fragment id is still copied into the next assistant
node, so a disable or update turn cannot erase branch state.

Reducers share an additional per-turn step and byte budget beyond the ordinary
per-flow quota; the host reserves each shared step before executing it and stops
dispatching reducers once it is exhausted.

The prior value goes in the Director's trailing request, not the tool schema, so
the schema stays byte-stable across turns. Editing configuration changes it
once, exactly as editing a core fragment does.

### Writer tools (`extension_api: 2`)

The one narrow exception to *"community packages do not add tools to the main
pipeline"*. A v2 package may declare **one** Writer tool:

```json
{
  "extension_api": 2,
  "permissions": [
    { "capability": "writer.tool.contribute" },
    { "capability": "context.read", "field": "draft" }
  ],
  "contributions": {
    "writer_tool": {
      "id": "resolve_outcome",
      "label": "Resolve outcome",
      "description": "Resolve an uncertain action when success or failure should not be chosen by the Writer alone.",
      "flow": "flows/resolve-outcome.json",
      "input_schema": { "…": "…" },
      "output_schema": { "…": "…" }
    }
  }
}
```

**The package declares no provider-facing name.** Orb derives one:
`orb_writer_<ext>--<tool>` normally, and the reserved injective form
`orb_writer__<ext-length>_<ext>_<tool>` when either id itself contains `--`. A
name that would exceed the strictest supported provider grammar or length fails
at compile time.

**The input schema carries a semantic request only.** `draft`,
`conversation_id`, `card_id`, `message_id`, `extension_id`, `turn_id`,
`history`, `persona`, and friends are refused as property names, because the
host supplies draft and entity identity and a model argument must never redirect
the invocation elsewhere.

The description is required and bounded at 600 characters because it is *model
input*: it ships in the Writer's tool blob every turn the tool is active and
steers generation even when no call happens. That is why the grant is
conspicuous on both halves.

**Availability is not activation.** Installing publishes the binding into the
snapshot; the user still selects at most **one** active resolver across the whole
install, with `PUT /api/extensions/{id}/writer-tool-active`. The selection is a
local *preference* — it survives disable and revocation (the tool is simply
inactive meanwhile) and is dropped only by uninstall, so a future package
claiming the same id is never silently activated. Selecting one clears the prior
selection transactionally and bumps `runtime_generation`. It does not travel in
shareable presets.

**Ownership is split three ways and stays split.** `core/writer_tools.py` owns
the ABI values, the derived wire name, call-id validity, and the fixed
result/error encoding — the built-in Writer-tool set is an empty snapshot
mapping, never a module-global list. `workflows/` carries the
`WriterToolBinding` on a `RegistrySnapshot` and enforces the snapshot-level caps
(32 published bindings, 8 KiB aggregate blob). `features/extensions/` compiles
the executor from the package flow. `pipeline/` sends the schema and invokes the
binding it captured — which is why `pipeline/` still never imports the extension
feature.

**Tool blob.** `_PipelineConfig` keeps `agent_tool_schemas` and
`writer_tool_schemas` apart. Single-model builds one deterministic union —
agent order preserved, the Writer schema appended — and dual-model gives each
lane only its own. An install with no selected resolver produces byte-identical
blobs to a build without the feature. Selecting, deselecting, or updating a
resolver intentionally changes the blob and forks the cache; that is the user
changing what the model can do. Agent enablement does not gate Writer
eligibility, and Director and Editor keep forcing their exact host tool choices.

**Tail OOC.** With no active tool the Writer keeps its host no-tools
instruction and `tool_choice="none"`. With one active tool, a closed
host-authored block follows the effective user request:

```text
[OOC: Writer tool policy for this turn.
You may write normally or call ONLY `orb_writer_…`.
Call it only when the uncertain action described by the tool should be
resolved before you decide what happens.
Call it at most once. Never call Director or Editor tools.
If you call it, pause at the current point. After Orb returns the result,
continue from that exact point without repeating prior prose.
]
```

The authority, exclusivity, call budget, and continuation wording are fixed Orb
text; the two package-influenced holes are the bounded description and a
schema-derived parameter summary. The block is the semantic tail even with
attachments — content parts are built so the policy is the final text part after
the image parts. The prompt is not the security boundary; the captured
`active_writer_tool` is.

**The loop.** One initial call with `tool_choice="auto"`. If the terminal
message has no *standard structured* `tool_calls`, the accumulated prose is the
draft and the turn ends — content-encoded fallbacks are deliberately not parsed
here, because reinterpreting narrative JSON as a call is unsafe once prose has
streamed. For exactly one standard call to the captured wire name: validate call
id, name, argument JSON, schema, and byte limits; compute `ctx.draft` from all
prose emitted before the call; invoke the captured binding with cancellation and
live grant re-checks; validate the return against the compiled output schema;
append the sanitized assistant message and one tool-role message carrying
`{"status": "ok", "result": {…}}`; continue once with `tool_choice="none"`.
Never a third completion.

Multiple calls, unselected or unknown names, and stale bindings execute nothing;
the host appends one fixed error result per call id so the transcript stays
protocol-valid, then makes the same single no-tools continuation. A call with an
unusable id recovers from a clean trailing branch (accumulated prose plus a
fixed host "continue without tools" request) rather than fabricating a provider
call id. Extension failures — timeout, revocation, invalid output, sanitized
`FlowError` — become `{"status": "error", "code": "resolver_unavailable"}`; the
Writer never sees internal exception text. User cancellation is the exception:
no tool result, no continuation.

**Flow context.** `writer_tool` allows pure operations, `return`, namespaced
`state.*` for config, conversation, and character, `model.*`, and
`http.request`. Everything else is refused **at compile time**, including all
three `ui.*` operations (the resolver's progress rides one fixed host status
channel, so a package choosing what the user reads during the pause would be a
second one) and message-scoped state (no assistant row exists for this entry
point, so a package declaring the write is describing a target that cannot
occur). The successful invocation is its own transaction: committed namespaced
state is **not** rolled back if the Writer continuation later fails or the user
aborts.

**Downstream.** `WriterReplay` (`pipeline/replay.py`) decides what a downstream
agent call replays. In single-model mode it replays the sanitized trace so the
call extends the Writer cache, with the canonical concatenated draft named
explicitly in the OOC request — the immediately preceding assistant message may
hold only the post-tool continuation. In dual-model mode the agent base does not
declare the tool, so the Editor, feedback, and direction-note steps get the
normalized `writer_user_msg + canonical assistant draft` shape instead. Only the
final concatenated prose is persisted; the hidden tool transcript and raw
extension output are not, and debug logging uses sizes, names, and status codes
rather than payloads.

**Transport.** Writer tools need a chat transport that returns standard
structured `tool_calls` with replayable call ids and honors `auto`/`none`.
Native text completion is deterministically ineligible: it does not render
optional tools into the prompt and only synthesizes a call when one schema is
forced. An arbitrary OpenAI-compatible chat URL carries no metadata that would
distinguish a standard implementation from a content-encoded template without
issuing a request, so unknown chat endpoints are admitted provisionally rather
than rejecting every unprofiled local endpoint by default. When the selected
resolver is incompatible, Orb omits the Writer schema from that request, takes
the ordinary no-tools path, and surfaces `writer_tool.diagnostic` in the
manager — it does not fail the turn.

The catalog exposes the whole state as host-derived fields:

```json
{
  "writer_tool": {
    "id": "resolve_outcome",
    "label": "Resolve outcome",
    "available": true,
    "active": true,
    "compatible_with_writer_endpoint": true,
    "diagnostic": ""
  }
}
```

### Audit detectors (`extension_api: 3`)

The Editor's mirror of the Writer tool. A v3 package may contribute up to four
checks to the Output Auditor behind `audit.detector.contribute`; each one runs
against the post-Writer draft and its findings merge into the same audit report
the built-in scanners produce — which means they render in the report the Editor
reads *and* feed the prefilled `editor_apply_patch` path, exactly like a cliché
hit. The use case that shaped it is scoring slop with a classifier model instead
of a static algorithm.

```json
{
  "extension_api": 3,
  "permissions": [
    { "capability": "audit.detector.contribute" },
    { "capability": "context.read", "field": "draft" },
    { "capability": "context.read", "field": "direction" },
    { "capability": "model.call", "lane": "agent" }
  ],
  "contributions": {
    "audit_detectors": [
      {
        "id": "slop",
        "label": "Model-scored slop",
        "description": "Flags the weakest sentence in each reply.",
        "flow": "flows/score-slop.json"
      }
    ]
  }
}
```

**The finding shape is host-fixed.** A detector declares no output schema, so it
cannot widen what a finding is. The flow returns an array of
`{ "snippet": str, "note": str }`; `snippet` is a span of the draft, or `""` for
a whole-draft judgement. `label` is stamped by the *host* from the binding onto
every finding — a per-finding heading would be package text rendered as though
Orb had classified it.

**Default off, and that asymmetry is the point.** `analysis.audit._on` defaults a
missing toggle to *enabled*, which is right for the built-in scanners shipped in
`schema.py`'s default JSON. It is wrong here: installing a package must not
silently add a per-turn model call and a draft-shaped egress to every reply. A
contributed detector is eligible on install and inert until the user ticks it,
mirroring the Writer tool's separate selection step. Contributed toggles key into
the existing free-form `settings.editor_audit_toggles` JSON under
`"<ext>:<local>"` — no migration, no new table, no new route.

**Detectors run once per turn**, before the initial audit, in one
`asyncio.gather` under one `asyncio.wait_for(AUDIT_DETECTOR_TIMEOUT_SECONDS)`.
The findings are merged into every subsequent report, and
`filter_audit_report_to_text` prunes each one as the rewrite fixes the span it
named. The editor audits up to three times, so re-running per iteration would be
up to 4× the model calls and 4× the prefix evictions for findings that are
mostly still valid. There is no per-turn budget object: each invocation already
carries the interpreter's own caps, and the registry bounds how many detectors
exist.

`AUDIT_DETECTOR_TIMEOUT_SECONDS` (20 s) is the first *wall-clock* timeout on a
flow invocation — the existing ones are transport-level. The asymmetry with the
Writer tool is deliberate: a Writer-tool call is something the model chose
mid-stream while the user watches tokens arrive, whereas a detector is invisible
work between the Writer finishing and the reply appearing. It sits below the
30 s HTTP timeout so a hung origin cannot hold a turn for its full budget.

**Any failure yields zero findings and never fails the turn** — a revoked grant,
a timeout, a malformed return, a blocked invocation. The same philosophy as
`RESOLVER_UNAVAILABLE`: the reply is not the place to surface a package's
problem.

**KV cache.** Findings render through `format_report` into the Editor's per-turn
tail message, exactly where the built-in report already goes; no pass's shared
prefix gains a byte. The unavoidable cost is the one the Writer tool already
pays — a detector's `model.*` call is an isolated, prefix-free completion, so on
a single-slot server it evicts the turn's shared prefix between the Writer and
the Editor. Running once per turn is what keeps that to one eviction.

**Document mode is deliberately out of scope.** `features/documents/audit.py`
calls `run_audit` directly with its own `document_audit_toggles` column and never
sees a `_PipelineConfig`.

The catalog lists the rows the audit panel renders:

```json
{
  "audit_detectors": [
    {
      "id": "slop",
      "namespaced_id": "slop-scorer:slop",
      "label": "Model-scored slop",
      "description": "Flags the weakest sentence in each reply.",
      "enabled": false
    }
  ]
}
```

---

## Network and secrets

`http.request` is the only outbound network operation, and
`features/extensions/network.py` is the only module that performs one — for
flows *and* for the Git installer's fetch. Never add a second HTTP path for a
package-influenced URL.

**Origins.** The manifest declares exact origins (`scheme://host:port`).
Installation shows each one individually, and grants distinguish public HTTPS
from loopback, private/link-local LAN, and plain HTTP, with a stronger warning
for the latter three. That warning is not local-network authority: a
public-looking hostname is still forbidden from resolving into
loopback/private/link-local space even over HTTP. No wildcard hosts or ports, no
URL userinfo, no `file:`, no Unix sockets, no package-controlled proxy settings.

**The client.** `trust_env=False`, so no ambient proxies. It derives the
canonical origin from the URL, checks it against the *live* grant set, resolves
and validates the destination before connecting, and pins the validated address
for the connection while `Host` and TLS SNI keep the real hostname — so there is
no second unvalidated DNS resolution. Redirects are followed by Orb,
revalidated per hop, and drop package headers when they cross origins. Request
bytes, response bytes, redirects, and wall time are all bounded, and secret
values and authorization headers are redacted from logs and errors.

**Secrets.** Declared by name, edited only in an Orb-owned form, and write-only
over the API — reads return presence, never the value. A flow may reference a
secret only in a declared header or body position, never in a URL, log,
template, UI value, state write, SSE payload, or returned error. Placeholders
compile to opaque request substitutions inside that one module; `http.request`
returns no rendered headers or body. Before a response becomes a flow value, and
before any value reaches a log, error, state, UI, SSE, or second request, it is
scanned for the exact configured secret bytes and rejected or redacted.

A multi-secret edit validates the whole batch and commits in one transaction.
Activating a revision removes stored rows that revision no longer declares, so
rolling back across that removal means configuring the old secret again rather
than silently reviving a hidden credential. At rest, secrets follow Orb's
existing local SQLite posture — the improvement here is non-disclosure to
package logic and frontend payloads, not encryption. Consent says a secret is
transmitted to each named origin; Orb cannot stop an origin that legitimately
receives one from retaining it, and taint scanning does not make a malicious
granted origin trustworthy.

---

## Installation and lifecycle

### Sources

Any public HTTPS Git host that supports a shallow fetch of the selected ref, with
an optional branch/tag/ref; plain HTTP only for an explicitly confirmed
repository whose origin and resolved addresses are local; or a local `.orbext`
ZIP. SSH, `git://`, private-repository credentials, submodules, and LFS are not
supported.

Git access uses Dulwich, imported lazily — a deployment without it boots,
reconciles, and installs `.orbext` archives normally. Orb never executes the
system `git` binary, so there are no hooks, filters, credential helpers, or
checkout smudge commands, and it never runs a package manager, compiler, or
build.

### Safe fetch

Parse and normalize the URL, apply the installer's network policy, then use an
Orb-owned Dulwich HTTP transport with the same `trust_env=False`, address
validation and pinning, redirect, TLS Host/SNI, byte, and timeout rules as the
flow client. Shallow-fetch only the selected ref into a temporary bare object
store with a hard received-pack limit, preflighting the disk-backed pack so a
small compressed pack cannot become an unbounded allocation; if the server
cannot honor a bounded fetch, reject rather than download full history. Peel
annotated tags with a bounded chain and record the *commit* id. Walk the commit
tree as Git objects — never checkout — rejecting symlink and gitlink modes, path
traversal, absolute paths, NULs, duplicate normalized paths, case-folding
collisions, excess entries, and size violations. Read and validate
`orb-extension.json`, materialize only the allowlisted referenced files into a
content-addressed staging directory, hash the canonical content, and return an
inspection result and permission diff. Nothing is activated yet.

Archives follow the same path from the "read and validate" step, and are
rejected as soon as either the compressed-input or decompressed-output budget is
exceeded — archive size metadata is never trusted on its own.

### One path from source to published

```text
bounded source read
  → canonical package tree
  → strict parse
  → reference-graph validation
  → compile immutable runtime definition
  → derive operations/components/capabilities/origins
  → show consent diff
  → durable content
  → database transaction
  → atomic runtime-snapshot publish
```

JSON parsing rejects duplicate object keys, non-finite numbers, invalid Unicode,
and anything past the depth and size limits. Compilation resolves every path,
reference, schema, operation, action target, component property, and asset into
an immutable host-owned value; runtime code never reinterprets a raw manifest
path or lazily opens a package-selected file. Package-defined data uses a
deliberately supported JSON Schema subset: local schemas only, no external
`$ref`, bounded depth/properties/enums/strings, unknown keywords rejected,
`additionalProperties` defaulting to `false`, and defaults validated like any
other value.

### Routes

```text
POST   /api/extensions/inspect              (Git URL: {url, ref, allow_local})
POST   /api/extensions/inspect-file         (local .orbext upload)
POST   /api/extensions/install
GET    /api/extensions                      (catalog + orphaned data)
GET    /api/extensions/{id}
POST   /api/extensions/{id}/enabled
POST   /api/extensions/{id}/inspect-update
POST   /api/extensions/{id}/inspect-update-git
POST   /api/extensions/{id}/update
POST   /api/extensions/{id}/inspect-rollback
POST   /api/extensions/{id}/rollback
PUT    /api/extensions/{id}/permissions
PUT    /api/extensions/{id}/secrets
PUT    /api/extensions/{id}/writer-tool-active
POST   /api/extensions/{id}/actions/{action}
GET    /api/extensions/{id}/views/{view}
GET    /api/extensions/{id}/resources/{resource}
GET    /api/extensions/{id}/assets/{path}
PUT    /api/extensions/{id}/state
DELETE /api/extensions/{id}
POST   /api/extensions/{id}/purge-data
```

Inspection returns an opaque, expiring staging token bound to the package
digest, the operation (`install`, `update`, or `rollback`), the extension id,
and the active digest observed at inspection time. Tokens are short-lived,
single-use, stored outside the package, invalid after restart, and pin their
content digest against garbage collection while live. Install accepts that token
plus the exact approved permission and origin set and the desired enablement;
apply recompiles the durable staged bytes and revalidates the digest before
commit. An update whose active digest changed since inspection fails with `409`.

`allow_local` is required for a repository on your own machine or LAN. The UI
toggles enablement through `/api/workflows/{id}/enabled`; the extensions route
is the API-only equivalent.

### State machine

An extension has three independent axes, and permission grants are stored
separately from all three:

| Axis | Values |
|---|---|
| **Installed revision** | package metadata plus an active content digest exist |
| **Load status** | `available`, `incompatible`, `invalid`, `missing_content` |
| **Enabled** | `settings.workflow_enabled[extension_id]` — the only enablement gate |

```text
installed
  AND load_status == available
  AND enabled for this extension
  AND the entry point's required grants are currently approved
```

They must not collapse into one boolean. A disabled package stays inspectable
and configurable but publishes no hooks, commands, or placements. An invalid,
incompatible, or missing-content package stays installed with a diagnostic. A
partially granted package publishes only the entry points whose statically
derived, transitive grant set is satisfied, and lists the rest in its
diagnostic.

`settings.workflow_enabled` is authoritative and `extension_packages.enabled` is
its catalog projection; install and toggle write both in one transaction so a
crash cannot leave them disagreeing. The Secondary Workflows master switch
covers the built-in tier only and never reaches an extension.

### Snapshots and hot reload

The built-in registry is finalized once at import time. Community definitions
are a copy-on-write overlay, and every lifecycle mutation swaps one immutable
snapshot containing the community workflow records and ordered subscriptions,
compiled flows/actions/views/assets, approved permission metadata, fragment
descriptors, Writer-tool bindings keyed by wire name plus the selected
resolver's name, a runtime generation, and the active content digests.

A turn captures exactly one snapshot **before** loading extension-sensitive
context and threads it through pre-hooks, fragment resolution, post-hooks, and
persistence. An action, view, resource, or asset request captures one at request
start. So an update can never mix an old pre-hook with a new post-hook, or an
old fragment schema with a new reducer, or send one revision's Writer schema and
invoke another's.

Snapshot acquisition blocks only for the short commit-and-publish window.
In-flight invocations keep their already-compiled objects and never read package
files lazily, so replacing or collecting a content directory cannot change an
active turn. Content garbage collection keeps every digest named by an
active/rollback pointer, the published runtime state, a live staging token, or a
snapshot still in use.

Permission revocation is the one exception to snapshot isolation: the live grant
view is checked before every privileged operation, so revocation stops the next
one. Disable and update do not rewrite an in-flight invocation, only prevent new
ones.

Every install, update, rollback, enablement change, permission change,
Writer-tool selection, and uninstall returns a monotonically increasing
`runtime_generation`; the frontend uses it to discard stale catalog and view
responses.

### Update, rollback, uninstall, purge

- **Updates** are manual, atomic, and the same inspect/consent/apply sequence. A
  manifest id change is rejected. Permission reductions need no new consent;
  additions or broadened origins do. A failed update leaves the previous
  revision, its grants, and its files fully active. Reapplying an identical
  digest preserves the existing rollback pointer rather than erasing it.
- **Rollback** keeps exactly one prior revision and is itself inspected: it
  shows the prior manifest and permission diff and cannot restore a capability
  or origin you have since revoked without fresh consent.
- **Uninstall** removes registration and secrets but leaves namespaced config,
  state, unavailable fragment instances, and attachments inert on disk, so a
  reinstall picks up where it left off.
- **Purge** is the separate destructive operation. Its first response is a
  per-store row-count preview plus a token whose server-side payload
  fingerprints the exact selected primary keys; confirmation opens a write
  transaction, recomputes the selection, and returns `409` without deleting
  anything if it changed. Purge disables and publishes the disabled overlay
  first, and drains or cancels active invocations, so an in-flight flow cannot
  commit the data back afterwards. Purge-by-id also works for a valid
  *uninstalled* community id, so preserved orphan data is never stranded.

Because a namespace is an id rather than a publisher identity, inspection
detects preserved data for that id and warns that the package will regain access
to it if `state.read` is approved — purge first if that is not what you want. A
Git URL plus a commit is provenance, not authorship, and Orb does not claim
otherwise.

### Startup

Extension initialization runs after `init_db()` and migrations and before the
FastAPI lifespan yields. It reconciles content, compiles every installed active
revision independently, publishes all valid definitions in one snapshot, and
records per-package sanitized load status without blocking Orb for a bad
package. Recompiling also compares the contract fingerprint against the stored
revision: a mismatch is `incompatible` and inert until an inspected update
records fresh consent, even when the digest is unchanged. Built-in workflows
keep loading regardless. Shutdown cancels and drains extension invocations and
removes staging data.

`GET /api/workflows` labels every entry `{"source": "builtin",
"frontend_kind": "trusted_module"}` or `{"source": "community",
"frontend_kind": "declarative", "extension_api": N}`, and
`workflow_loader.js` calls `import()` only for `trusted_module` entries. A
community record is never interpolated into `import()`, `innerHTML`, an inline
handler, or a workflow callback array, and an extension directory is never
mounted as same-origin static content.

---

## Storage

| Table | Holds |
|---|---|
| `extension_packages` | id, source kind/URL/ref, active and previous digests, approved permissions, enabled mirror, timestamps, load status and sanitized error, `writer_tool_active` |
| `extension_revisions` | `(extension_id, content_digest)`, canonical manifest JSON, API/version metadata, resolved commit, contract fingerprint, first-seen — active and previous only |
| `extension_secrets` | `(extension_id, name)`, value, updated timestamp |

All three cascade from the package row, and all three are `LOCAL_ONLY_TABLES`:
stripped from shareable presets, retained in full local snapshots so a local
rollback also rolls back installation metadata. Secret values participate in the
existing key-stripping path. If a full database moves to a machine without the
matching content files, the package is marked unavailable and offers an explicit
refetch of the recorded commit — never an automatic fetch during startup or
restore. Archive-installed revisions require re-upload.

State lives where workflow state already lives, namespaced by extension id:

- Global config — `settings.workflow_config[extension_id]`
- Conversation — `conversations.workflow_state[extension_id]`
- Message — `messages.workflow_state[extension_id]`
- Character — `character_cards.workflow_state[extension_id]`
- Artifacts — `workflow_attachments.workflow_id`

The 256 KiB cap is enforced against the canonical encoded *extension slot*, not
the containing JSON column. Purge removes those slots with parameterized JSON
paths, deletes the selected attachments, and optionally deletes fragment
instances whose `field_type` belongs to the extension.

Package files live under a content-addressed directory derived from
`dirname(DB_PATH)` — `data/extensions/objects/<digest>/` — never inside the
frontend tree, and always derived from a validated digest rather than a
package-supplied path. Inspection fsyncs and atomically renames the complete
content directory before issuing a token; activation takes the short lifecycle
lock, recompiles and verifies those durable bytes, commits
revision/package/grants/enablement in one transaction, publishes the snapshot,
collects unreferenced content, and releases. Readers stay lock-free and see
either the old or the new snapshot. A crash can leave an unreferenced directory
(startup GC removes it) or committed metadata startup reloads — never a pointer
at a half-written directory.

Portable chat and character presets may carry inert namespaced extension state.
It is preserved, and becomes meaningful again if the user installs the provider.

### Writes that leave the namespace

Every state location above is namespaced, which is what makes the lifecycle
story coherent: uninstall leaves inert data, purge finds it by namespace, and a
package's writes are invisible to Orb until it is reinstalled.

`card.tags.set` is the one operation that breaks all three, and the consequences
are stated rather than discovered. Uninstall does not revert it — the tags stay
on the cards. Purge cannot find it, because those rows have no namespace, and
there is no "undo this extension's tag writes" operation. And it leaves the
machine: `tags` round-trips into exported character PNGs and travels in
shareable character presets, so an extension write can reach other people's
libraries. No other extension write can.

Three things bound the damage, and none should be overstated: one invocation
dirties one card, so library-wide reach requires the user to drive the loop; the
host normalizes and caps what gets written, so the worst case is wrong tags
rather than malformed ones; and `tags` reaches no prompt anywhere in the
pipeline, so a bad write degrades organization, never generation.

---

## Extending the ABI

Adding surface is the common case, and almost all of it rides the existing
vocabularies: a new entry in `OPERATION_SPECS` or `CAPABILITY_SPECS` — including
a new admissible *value* of an existing capability, which is also how `UI_SLOTS`
and the host-resource catalog grow, since both are derived from that table. New
surface is feature-detected through `requires.operations` /
`requires.components`, so an older Orb rejects a package that needs it with a
diagnostic rather than misbehaving. None of that needs an `extension_api` bump.

### Where each contract lives

| Contract | Module |
|---|---|
| Grants, operations, contexts, quotas | `backend/features/extensions/contracts/capabilities.py` |
| Manifest, descriptors, API version tables | `backend/features/extensions/contracts/manifest.py` |
| Components, view data sources | `backend/features/extensions/contracts/components.py` |
| Effect envelope | `backend/features/extensions/contracts/effects.py` |
| JSON Schema subset | `backend/features/extensions/contracts/schema_subset.py` |
| Every hard bound | `backend/features/extensions/limits.py` |
| Writer-tool ABI + snapshot caps | `backend/core/writer_tools.py` |
| Registry snapshot, bindings, hook stages | `backend/workflows/registry.py`, `contracts.py` |
| Renderer, manager, command model | `frontend/extension_renderer.js`, `extension_manager.js`, `extension_commands.js` |

### Recipes

- **A new grant, or a new value of one.** One `CapabilitySpec` (or `ValueSpec`)
  entry. Consent copy is a required field, so a grant cannot reach a dialog
  without a sentence describing it; `reads` decides whether the combination
  banner fires; `resource` wires it to a host resource; `requires` adds a
  prerequisite. Everything else derives.
- **A new operation.** One `OperationSpec`: the capability it consumes, the
  contexts it may run in, whether it produces output, its quota counter, whether
  its effect stages, and whether it pins its grant parameter or reads it from a
  step field. Then the interpreter case, and a bound in `limits.py` if it can
  grow.
- **A new component.** A model in `components.py` joined to the `Component`
  union — `COMPONENT_NAMES` derives from the union, not from a list beside it —
  plus a renderer case that creates nodes and sets `textContent`.
- **A new UI slot.** A `ValueSpec` under `ui.contribute`, plus an empty
  `[data-ext-slot]` container in the host's own markup. Adopt the existing
  command model; do not build a second placement mechanism.
- **A new API version.** Add it to `SUPPORTED_EXTENSION_APIS` and give each new
  `contributions` slot an entry in `CONTRIBUTION_MIN_API`. A slot introduced in
  a later API stays refused on an earlier one, which is the whole reason a
  contribution field warrants a version bump. API 2 added `writer_tool`; API 3
  added `audit_detectors`.
- **A new pass-facing contribution.** Follow the three-way split both existing
  ones use: the value contract in the lowest layer every owner can import
  (`core/writer_tools.py` for the Writer tool, because three layers must agree on
  a wire name; `analysis/audit.py` for a finding, because the report it merges
  into already lives there), the binding on a `RegistrySnapshot`, the executor
  compiled in `features/extensions/`, and resolution from the *captured* snapshot
  in `pipeline/`. A new `core/` module is admissible only when no lower layer can
  own the contract — check the core admission rule before adding one.

### Admission rules

**A new host resource** is admissible when all five hold:

1. It is an allowlisted field projection built for the extension surface, never
   a row contract or API response passed through.
2. It is bounded by both an item count and an encoded-byte budget, and
   paginated with an opaque host-owned cursor when the underlying set is
   unbounded.
3. Its scope is fixed by the invocation's own context unless a separate
   enumeration grant conspicuously covers wider reach.
4. It is served as a database projection plus a host-resource adapter, never a
   query primitive handed to a flow.
5. Its consent line names what it reads in user terms — and when the projected
   data can reach a model call or a network origin, the manager surfaces that
   combination.

Condition 5 is why the banner exists: reading is not the new risk, reading
*combined with* `network.request` is, and consent must say so rather than
leaving the user to compose two innocuous-looking grants in their head.

**A new first-party write** — one that lands outside the extension's namespace —
is admissible when all four hold:

1. Its target is fixed by the invocation's own context, never by a
   package-supplied identifier that a grant does not separately cover.
2. A host-owned normalizer, shared with the first-party path, decides the final
   stored value.
3. A bad write degrades organization or presentation, not generation.
4. Its propagation is understood and written down — where it exports to, which
   presets carry it, whose machine it can reach.

`card.tags.set` clears all four. A write to a card field that reaches the prompt
fails (3) and is a different threat model. A write that reaches a card the
invocation was not handed fails (1). Neither is an extra entry in an allowlist.

**Reach versus write.** Reach is granted once, visibly, by the capability that
enumerates (`library.cards.read`); writes stay scoped to whatever the invocation
already holds. A package that wants to write some other card field adds a narrow
scoped operation, not another library-wide grant.

### Layering

Dependencies run strictly downward (see `AGENTS.md`). For this feature that
means:

- `features/extensions/` owns package, contract, compiler, and interpreter
  behavior, and may register into the lower `workflows/` layer.
- `pipeline/` consumes only lower runtime contracts and snapshots. It never
  imports `features/extensions/` — the Writer-tool binding indirection exists
  precisely so it never has to.
- `inference/` and `database/` never import the extension feature, and no
  community name ever enters the mutable inference `TOOLS` registry.
- Anything `pipeline/`, `workflows/`, and `features/extensions/` must agree on
  exactly, where none of the three may import the others in the needed
  direction, goes in `core/` — and only if it also passes the core admission
  rule in `AGENTS.md`. Being pure or shared is not sufficient.
- When a lower layer needs higher-layer behavior, invert: the lower layer
  declares a hook, the higher layer registers an implementation.

### Change it in lockstep

A schema change touches `database/schema.py`, the migration, `models.py`,
`api/schemas.py`, `database/preset_schema.py`, and the seeds together —
migrations run for upgrades only, so anything not in `schema.py`/`seeds.py` will
be missing on a fresh install.
`tests/integration/test_fresh_install_stamping.py` and
`test_preset_schema_coverage.py` fail when they diverge. Tests to keep honest
alongside a contract change: the golden contract files, the malicious-package
corpus, interpreter unit tests, lifecycle and concurrency integration tests,
frontend renderer/XSS tests, KV parity, and the reference package fixtures.

### Observability

Every invocation records wall time, model-call and HTTP counts, and outcome
through the shared `InvocationTimer`; Writer-tool invocations additionally
record bounded input/output encoded byte sizes, after encoding succeeds and
before the staged state transaction commits. It aggregates per extension in the
manager's diagnostics beside load status. Telemetry is host-only: it is never
projected into `ExtensionCtx` or any package-visible surface, so flows get no
timing channel, and it contains no prompt or result content.

---

## Limits

Package:

| Limit | Value |
|---|---:|
| Downloaded Git pack / archive | 50 MiB |
| One expanded Git object / one pack's expanded total | 25 MiB / 100 MiB |
| Referenced files, decompressed | 25 MiB total |
| `orb-extension.json` | 1 MiB |
| One referenced asset | 10 MiB |
| Reachable tree entries | 512 |
| Path length | 240 bytes |
| Declared flow steps | 128 |
| Permissions / actions / views / placements / commands | 64 / 32 / 32 / 32 / 32 |
| Declared origins / secrets / fragment types / audit detectors | 8 / 8 / 16 / 4 |
| Component nodes per view, depth, data sources | 256 / 12 / 8 |
| Declared `$template` literal, substitutions | 8192 chars / 32 |
| Writer-tool description (and each property description) | 600 chars |
| One Writer-tool schema entry / all published bindings | 4 KiB / 32 bindings, 8 KiB blob |
| Published audit detectors per snapshot | 8 |

Per flow invocation:

| Limit | Value |
|---|---:|
| Executed steps, nesting depth, predicate depth | 128 / 8 / 8 |
| Model calls / HTTP requests | 2 / 4 |
| Model output | 4096 tokens, 1 MiB |
| HTTP body / response / timeout / redirects | 1 MiB / 5 MiB / 30 s / 3 |
| State per scope | 256 KiB |
| One context block / all blocks per target | 8 KiB / 32 KiB |
| Artifacts, each / recovery metadata | 2, 10 MiB / 16 KiB |
| Card tag writes | 1 (32 tags × 64 bytes) |
| `list.*` input array members | 256 |
| Rendered template | 128 KiB |
| Draft / action result | 1 MiB / 1 MiB |
| Writer-tool arguments / result | 16 KiB / 8 KiB |
| Audit findings per detector / snippet / note / batch wall clock | 8 / 400 chars / 300 chars / 20 s |
| JSON depth / members / string | 32 / 1024 / 256 KiB |

Projections and resources:

| Limit | Value |
|---|---:|
| History window | 20 messages, 32 KiB |
| One projected text field | 64 KiB |
| Character / persona / direction projection | 16 KiB / 8 KiB / 16 KiB |
| Conversation-tree nodes, one preview | 2000, 120 chars |
| One resource response / page items / one text field | 512 KiB / 100 / 4 KiB |
| Active extension-backed fragment instances per turn | 50 |

---

## What is not available

Deliberately, and not by omission: WASM or any general-purpose package code;
Director or Editor tools; new pipeline passes; background services, timers, or
startup jobs; extension-to-extension dependencies; arbitrary DOM, CSS, canvas,
iframes, or browser storage; SSH or private Git auth; wildcard network
permissions; general regular expressions (Python's backtracking engine would
make an innocent-looking text operation a denial-of-service surface); general
list iteration (`map`, `filter`, `reduce`); arbitrary database tables, SQL,
filesystem paths, or HTTP routes; writing-mode documents; lorebook or
direction-note writes (both reach the prompt); world enumeration; and automatic
update installation.

These are not TODOs with a permissive placeholder waiting behind them. Each
would need its own threat model and its own grant.
