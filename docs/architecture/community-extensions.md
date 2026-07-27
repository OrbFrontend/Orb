# Community Extensions v1 — Architecture Handoff

Status: **Phases 0-1 implemented; Phases 2-6 not started**

Landed (see section 17 for the phase definitions):

**Phase 0 — contracts and runtime seams**

- Frozen v1 contract models in `backend/features/extensions/contracts/` --
  manifest, permissions, flow/operations, values and predicates, JSON Schema
  subset, components, effect envelope, fragment-type descriptors.
- The strict JSON loader (`json_loader.py`), canonical encoding and content
  digest (`digest.py`), path normalization (`paths.py`), and the limit and
  error vocabularies.
- Registry refactor: built-in base plus immutable community overlay, hook
  stages, runtime generation, and snapshot-aware lookups
  (`backend/workflows/registry.py`), with the captured snapshot threaded
  through `PipelineContext` -> `_prepare_turn` -> `_run_pipeline` -> both
  workflow bridges.
- Persistence: `extension_packages` / `extension_revisions` /
  `extension_secrets`, `interactive_fragments.type_config`, migration 0051, and
  the `LOCAL_ONLY_TABLES` preset policy.
- `GET /api/workflows` reports `source` / `frontend_kind` / `load_status` /
  `diagnostic`, and `frontend/workflow_loader.js` imports only
  `trusted_module` entries.

**Phase 1 — local package lifecycle and management UI**

- Bounded `.orbext` reading (`sources.py`): symlink/traversal/case-collision
  rejection, entry and expanded-byte budgets, and a path-keyed interface with
  no enumeration, so only manifest-referenced files can be read.
- The media allowlist and leading-byte check (`assets.py`).
- The compiler (`compiler.py`): reference-graph validation, conservative
  requirement derivation from operations, `ctx.*` reads, view bindings and
  placements, declaration-coverage enforcement, and the consent-contract
  fingerprint. Startup recompilation compares that fingerprint with the active
  revision record and requires an inspected update/re-consent if compiler
  semantics would change the approved contract for identical package bytes.
- The content store (`content_store.py`): `data/extensions/objects/<digest>/`,
  fsync + atomic rename durability, and garbage collection rooted by active /
  rollback revisions, published runtime state, and live inspection tokens.
- Staging tokens (`staging.py`): opaque, expiring, single-use, in-memory, and
  content-pinning for as long as the inspection remains redeemable.
- Runtime publishing (`runtime.py`): per-package compile isolation, the three
  independent state axes, blocked-entry-point derivation, and one overlay swap
  per mutation.
- The lifecycle (`lifecycle.py`) and its routes
  (`backend/api/routes/extensions.py`): inspect / install / list / detail /
  enable / inspect-update / update / inspect-rollback / rollback / permissions /
  uninstall / two-phase purge, plus startup reconciliation wired into the
  FastAPI lifespan and `core.locks.extension_lifecycle_lock`. Package
  registration and enablement commit together; revision history is bounded to
  active plus rollback; purge accepts only valid non-built-in community
  namespaces and transactionally revalidates the exact previewed row set.
- `frontend/extension_manager.js` and `css/extensions.css`: the Orb-owned
  sidebar, catalog, consent diff, diagnostics, purge preview, and cross-tab
  catalog refresh (`EXTENSION_MUTATION` on the tab-lock channel). Built with
  DOM creation and `textContent` throughout, with no inline handlers.

Deliberately absent, per the note at the end of section 16: there is still no
interpreter, no `ExtensionCtx`, no host HTTP client or secret substitution, no
component renderer, no Git reader, and no fragment-type contribution. Phase 1
publishes community records with **no subscriptions** -- an installed package
contributes catalog metadata and nothing executable -- and no permissive
placeholder executor stands in for the real one.

Two Phase 1 route names extend section 12's family. `POST
/api/extensions/{id}/inspect-rollback` exists because rollback is an inspected
operation with a real permission diff (restoring a revision must not restore a
capability since revoked), and it deserves the same two-request shape as
update rather than an implicit mode on `/rollback`. `POST
/api/extensions/inspect` (Git) is deferred with the rest of the Dulwich work to
Phase 4; `PUT /{id}/secrets` and the `/actions`, `/views`, `/resources`, and
`/assets` routes are deferred to the phases that add the runtime which
validates what they would serve.

Audience: the engineer implementing Orb's community extension system. This
document is an implementation handoff for v1: trust boundary, required host
behaviors, package and runtime contracts, concrete codebase changes,
persistence, failure behavior, rollout order, and acceptance tests.

Read these first:

- [Secondary Workflows](secondary-workflow.md) — the trusted first-party
  execution framework community extensions reuse.
- [KV Cache Reuse](kv-cache.md) — constraints on prompt placement and tool
  schemas.
- [SSE Turn Stream](sse-stream.md) — the stream community adapters may augment
  through host-defined events.
- [Card-Embedded Fragments](../features/card-fragments.md) — the existing
  untrusted fragment boundary that extension-defined fragment types must
  preserve.

---

## 1. Decisions

| Question | v1 decision |
|---|---|
| Reuse secondary workflows? | Yes, for lifecycle, state, locks, hooks, artifacts, SSE, and enablement. Do not expose their Python/JavaScript authoring boundary to community packages. |
| Runtime language | Declarative JSON interpreted by Orb. No Python, JavaScript, HTML, CSS, WASM, native libraries, templates with evaluation, or install/build scripts. |
| UI power | Broad but host-rendered: commands, menu placements, panels/workspaces, forms, tables, media, and graph/tree components. No DOM or raw markup access. |
| Installation | HTTPS Git URL or local `.orbext` archive. Resolve and inspect before asking for permission consent. |
| Git implementation | In-process Git protocol client; never execute the system `git` command. Inspect Git objects without checking out a worktree. |
| Network | Host-mediated HTTP to exact manifest-declared origins after user consent. Public and local/private origins are separate grants. |
| Model tools | Community extensions do **not** add tools to Director, Writer, or Editor. Extension flows may make their own bounded model calls. |
| Built-in workflows | Remain trusted Python plus same-origin JavaScript. They are a separate trust tier and do not need to migrate. |
| Dependencies | No extension-to-extension dependencies in v1. |
| Updates | Manual by default; atomic; permission expansion requires fresh consent; keep one prior revision for rollback. |

The design borrows SillyTavern's discover/install/enable experience, not its
execution model. SillyTavern extensions identify JavaScript and CSS entry files
and can access mutable application context; its own documentation warns users
that third-party extensions may pose security risks. Orb's v1 package is data,
not executable code:

- <https://docs.sillytavern.app/for-contributors/writing-extensions/>
- <https://docs.sillytavern.app/extensions/>

### Security claim

"No RCE surface" means there is no intentional primitive that interprets a
package file as host or browser code, launches a package-selected process, or
hands a package an object capable of doing so. It is not a claim that parsers,
Git clients, media decoders, or Orb itself can never contain vulnerabilities.
Those inputs still need bounds, fuzzing, dependency updates, and defense in
depth.

---

## 2. Core implementation contract

The implementation is complete only when the following host behaviors work
together. Individual parsers, routes, or UI components are not useful milestones
unless they preserve this lifecycle.

### Package state machine

An extension has three independent state axes:

1. **Installed revision** — package metadata and an active content digest exist.
2. **Load status** — the active revision is `available`, `incompatible`,
   `invalid`, or `missing_content`.
3. **Enablement** — the existing global workflow master switch and
   `settings.workflow_enabled[extension_id]` both permit invocation.

Permission grants are stored separately from all three. The effective runtime
state is:

```text
installed
  AND load_status == available
  AND globally enabled
  AND enabled for this extension
  AND the entry point's required grants are currently approved
```

An explicit install may enable the package as part of the same confirmed
operation. Installing with `enabled: false` is also supported. A prior explicit
off value survives uninstall/reinstall because normal uninstall preserves the
existing namespaced settings and state. `settings.workflow_enabled` is
authoritative; `extension_packages.enabled` is its catalog projection. Install
and toggle paths write both in one database transaction so a crash cannot leave
the catalog and invocation gate disagreeing.

`installed`, `enabled`, and `available` must not be collapsed into one boolean:

- Disabled packages remain inspectable and configurable but have no hooks,
  commands, or placements.
- Invalid, incompatible, or missing-content packages remain installed with a
  diagnostic but publish no runtime entry points.
- A partially granted package may publish only entry points whose statically
  derived, transitive grant set is satisfied. A view inherits the requirements
  of every host resource/action it references. Blocked hooks/actions/views are
  listed in its diagnostic; they do not fail halfway through ordinary use.
- Uninstall removes package registration and secrets but leaves inert
  namespaced data. Purge is a separate destructive operation.

Because state namespaces use the extension ID rather than a cryptographic
publisher identity, inspection must detect preserved/orphaned data for that ID.
Installing from a different source (or reinstalling after uninstall) shows a
specific warning that the package will regain access to that data if
`state.read` is approved. The user may purge it before install. v1 must not imply
publisher authenticity that Git URL plus commit/digest does not provide.

Every install, update, rollback, enablement change, permission change, and
uninstall returns a monotonically increasing `runtime_generation`. The frontend
uses it to discard stale catalog/view responses.

### Inspect, compile, consent, publish

Every source follows one path:

```text
bounded source read
  -> canonical package tree
  -> strict parse
  -> reference-graph validation
  -> compile immutable runtime definition
  -> derive operations/components/capabilities/origins
  -> show consent diff
  -> durable content
  -> database transaction
  -> atomic runtime-snapshot publish
```

The compiler, not the manifest's claims, derives the complete requirement set by
walking every referenced flow, view, placement, asset, schema, secret use, and
contribution. The declared `requires` and `permissions` must cover that derived
set. An omitted declaration is a validation error; an unknown declared
requirement makes the revision incompatible. Unreferenced repository files are
never compiled, persisted, or served.

JSON parsing rejects duplicate object keys, non-finite numbers, invalid Unicode,
and values beyond the common depth/size limits. Compilation resolves all paths,
references, schemas, operations, action targets, component properties, and
assets into immutable host-owned values. Runtime code never reinterprets a raw
manifest path or lazily opens a package-selected file.

### Immutable snapshots and hot reload

The trusted built-in registry is finalized once. Community definitions are a
copy-on-write overlay. Publishing a lifecycle mutation swaps one immutable
snapshot containing:

- Community workflow records and ordered subscriptions.
- Compiled flows/actions/views/assets.
- Approved permission metadata.
- Extension-defined fragment descriptors.
- A runtime generation and active content digests.

A turn captures exactly one snapshot **before** loading extension-sensitive
context and threads it through pre-hooks, fragment resolution, post-hooks, and
persistence. An on-demand action, query, asset request, or view request likewise
captures one snapshot at request start. An update cannot mix an old pre-hook
with a new post-hook or an old fragment schema with a new reducer.

Snapshot acquisition is blocked only for the short database-commit plus
in-memory-publish window. Existing invocations retain their compiled objects;
new invocations see the new generation. Content garbage collection keeps every
digest named by active/rollback package pointers, the published runtime state,
or a live staging token; later executable phases must additionally retain any
older snapshot/request digest until its last invocation releases it.

Permission revocation is the exception to snapshot isolation: immediately
before every privileged host operation, the interpreter checks the current live
grant view as well as the invocation's compiled requirement. Revocation can
therefore stop the next operation of an already-running flow. Disable/update do
not retroactively rewrite an in-flight invocation, but prevent new ones.

### Invocation transaction

One flow invocation has a prepare/commit split:

1. Validate action input, derive the entity/state scopes the compiled flow may
   touch, and acquire their locks in the canonical host order.
2. Resolve a capability-filtered JSON context and state snapshot under those
   locks.
3. Execute steps with quotas and cancellation while staging every Orb-owned
   effect.
4. If and only if the flow returns successfully, validate the complete staged
   effect set again and commit it under the owning locks/transaction.
5. Publish fixed host events and invalidations after commit.

The staged effects are state writes/deletes, draft replacement, context blocks,
attachments, branch activation, view invalidations, and success toasts. A failed
flow discards all of them. `ui.status` may stream while work is running and is
always cleared in `finally`.

Model calls and HTTP requests are external side effects and cannot be rolled
back. They still cannot make a partial Orb mutation visible. In particular, a
flow that stages a state write, then receives an invalid HTTP response, leaves
the prior state untouched.

Holding the per-extension locks from state read through commit preserves atomic
read-modify-write semantics even when a model/HTTP step is slow. It serializes
only the same extension/entity keys, matching the current trusted workflow
behavior. The canonical order extends the existing order:
conversation stream (when required), workflow config, conversation state,
character state, then message state.

The existing workflow bridge currently wraps every subscription in
conversation/character state locks. Preserve that path for trusted callables,
but let the community interpreter own its statically derived lock plan; the
bridge must not acquire the same non-reentrant lock around a community adapter.

A flow containing branch activation cannot contain model or HTTP operations.
The action acquires the conversation stream lock before its state locks and
keeps the resulting pure action short; this matches the pipeline's
stream-before-workflow-lock order and prevents a branch-action/pipeline
deadlock.

The extension interpreter must use transaction-aware database helpers; it must
not compose the current helpers that each open and commit an independent
connection.

### Hook stages and ordering

Community v1 exposes only:

- `pre_pipeline` — may produce trailing context blocks and state, but cannot see
  or replace a draft.
- `post_pipeline` with `stage: "transform"` — sees the current draft and may
  replace it once.
- `post_pipeline` with `stage: "observe"` — sees the final immutable draft and
  cannot replace it.
- Named on-demand actions plus the required regenerate/reroll actions for
  artifact producers.

The workflow registry gains an explicit hook stage. Post processing is:

1. Trusted and community `transform` subscriptions.
2. Trusted and community `observe` subscriptions.

Within a stage, trusted built-ins are the first source band and preserve their
declared priority/insertion order; community entries form the second band and
sort by priority then extension ID. Stage precedes source band, so every
community transform still runs before every trusted observer. Core built-ins
are classified deliberately: format consistency is a transform, while
consumers such as TTS are observers. Each successful transformer feeds the next
one; all observers see the same final draft. One extension's failure discards
only that invocation and does not abort the turn or erase earlier successful
transforms.

The operation compiler enforces a hook-specific allowlist. For example,
`context.append` is pre-only, `draft.replace` is post-transform-only,
conversation branch mutation is action-only, and an observe hook cannot smuggle
in a rewrite through a generic state or UI operation.

### Fixed host effects

Community packages cannot invent SSE event names or frontend callbacks.
Interpreter output is translated to a small host envelope:

```json
{
  "data": {},
  "effects": [
    {
      "resource": "conversation.messages",
      "conversation_id": "conv-1"
    },
    {
      "resource": "extension.view",
      "extension_id": "scene-meter",
      "view": "inspector"
    }
  ],
  "runtime_generation": 12
}
```

The frontend owns the effect-to-refetch mapping for messages, Director state,
direction notes, extension views, and the extension catalog. The same fixed
effects drive local repaint and cross-tab broadcast. Unknown effects are dropped
and logged; package strings never become event names, DOM selectors, function
names, or module paths.

---

## 3. Two trust tiers, one workflow lifecycle

Orb will have two extension mechanisms with an explicit trust distinction:

### Trusted built-in workflows

The existing `backend/workflows/**` and `frontend/workflows/**` mechanism:

- Python hook callables receive rich context and toolkit objects.
- Frontend ES modules import the workflow facade and execute in Orb's origin.
- These modules ship with Orb and receive normal code review.

### Community extensions

A new `features/extensions/` slice:

- Loads and validates package data.
- Compiles declarative flows into generic adapter callables.
- Projects trusted `PreCtx`/`PostCtx`/route contexts into a capability-filtered
  `ExtensionCtx`.
- Registers the adapters in the existing workflow registry.
- Renders package UI through a host-owned component renderer.

One community extension maps to one workflow ID. Its hooks and named actions may
be numerous, but its config and conversation/message/character state remain
under the one existing namespace.

Use the existing lowercase fragment/card grammar for community extension IDs:

```text
[a-z0-9][a-z0-9_-]{0,63}
```

Reject IDs that collide with a built-in workflow, the reserved `macros` message
state slot, or another installed package. The same grammar and built-in/reserved
checks protect purge-by-ID. A valid uninstalled community ID remains purgeable
so preserved orphan data is not stranded.

---

## 4. Package format

### Authoring language

The installed contract is JSON: a manifest, declarative flows, component trees,
and constrained schemas. Authors may generate those files with TypeScript,
Python, or any other development tool, but Orb consumes only the resulting
validated JSON and safe assets. It never installs an author's source language,
runtime, lockfile, or dependencies.

The reference `orb-ext` validator/packer should be Python so it can reuse Orb's
Pydantic contract models. It is an optional author-side tool; the Orb installer
does not execute it or trust its output without validating the package again.

Canonical repository layout:

```text
orb-extension.json
flows/
  score-scene.json
  reduce-meter.json
ui/
  inspector.json
  tree.json
assets/
  icon.webp
README.md
LICENSE
```

Only `orb-extension.json` is mandatory. Referenced paths must be relative,
normalized UTF-8 paths contained by the package root. Paths are case-sensitive
in the package contract even on a case-insensitive host filesystem.

The content digest is SHA-256 over a versioned domain separator followed by
every selected normalized path in UTF-8 byte order and its length-delimited
canonical content. JSON uses Orb's canonical encoding of the strictly parsed
value; media/text assets use exact bytes. The same digest algorithm is shared by
Git inspection, archive inspection, install revalidation, startup, update, and
the author CLI.

Example manifest:

```json
{
  "$schema": "https://orbfrontend.github.io/Orb/schemas/extension-v1.json",
  "extension_api": 1,
  "id": "scene-meter",
  "name": "Scene Meter",
  "version": "1.0.0",
  "author": "Example Author",
  "description": "Tracks and displays scene tension.",
  "homepage": "https://example.invalid/scene-meter",
  "requires": {
    "operations": ["model.structured", "state.set", "ui.invalidate"],
    "components": ["meter"]
  },
  "permissions": [
    {
      "capability": "context.draft.read"
    },
    {
      "capability": "model.call",
      "lane": "agent"
    },
    {
      "capability": "state.write",
      "scope": "conversation"
    },
    {
      "capability": "state.read",
      "scope": "conversation"
    },
    {
      "capability": "ui.contribute",
      "slot": "inspector"
    }
  ],
  "hooks": {
    "post_pipeline": {
      "flow": "flows/score-scene.json",
      "stage": "observe"
    }
  },
  "views": {
    "inspector": {
      "source": "ui/inspector.json"
    }
  },
  "placements": [
    {
      "slot": "inspector",
      "view": "inspector"
    }
  ]
}
```

`extension_api` is the compatibility boundary. Orb currently exposes no product
version constant, so v1 must not invent a `minimum_orb_version` field that
cannot be evaluated reliably. `requires.operations` and
`requires.components` provide precise feature detection; an unknown requirement
leaves the extension installed but unavailable with a diagnostic.

### Package limits

Apply all limits before persistence:

| Limit | Value |
|---|---:|
| Downloaded Git pack/archive | 50 MiB |
| Reachable tree entries | 512 |
| Referenced files after decompression | 25 MiB total |
| `orb-extension.json` | 1 MiB |
| One referenced asset | 10 MiB |
| Path length | 240 UTF-8 bytes |

Reject an archive as soon as either its compressed-input or decompressed-output
budget is exceeded; never trust archive size metadata alone. Apply the same
entry and expanded-byte limits while walking Git blobs.

The repository may contain development sources such as TypeScript or a
`package.json`, but Orb never materializes or serves unreferenced files.
Manifest references may target only JSON, safe raster/audio/video assets,
README text, and license text. Reject active formats such as HTML, JavaScript,
CSS, SVG, PDF, and browser fonts in v1.

---

## 5. Flow language

A flow is a bounded sequence of host operations. It has no user-defined
functions, imports, recursion, cycles, or unbounded iteration.

```json
{
  "flow_version": 1,
  "steps": [
    {
      "id": "score",
      "op": "model.structured",
      "lane": "agent",
      "prompt": {
        "$template": "Rate scene tension from 0 to 100.\n\n{{ctx.draft}}"
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "tension": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100
          }
        },
        "required": ["tension"],
        "additionalProperties": false
      }
    },
    {
      "op": "state.set",
      "scope": "conversation",
      "path": "tension",
      "value": {
        "$ref": "steps.score.tension"
      }
    },
    {
      "op": "ui.invalidate",
      "view": "inspector"
    }
  ]
}
```

### Values and conditions

Supported dynamic values:

- `{"$ref": "ctx.draft"}` — read a value by validated path.
- `{"$template": "...{{ctx.draft}}..."}` — interpolate scalar path values.
- JSON literals and arrays/objects containing the two forms above.

Templates perform path substitution only. They have no filters, property
access beyond the validated path resolver, calls, loops, includes, or
expressions.

`when` uses a structured predicate AST with `eq`, `ne`, `lt`, `lte`, `gt`,
`gte`, `exists`, `and`, `or`, and `not`. Predicate depth is capped at 8.

Step IDs are unique within a flow. A `$ref` may target `ctx`, validated action
input, host-provided invocation metadata, or the output of an earlier named
step; forward references are rejected. Missing optional paths resolve to a
distinct `missing` sentinel rather than JSON `null`, so `exists` is
unambiguous. The sentinel may be tested or replaced by a fallback but cannot be
returned, persisted, interpolated, or sent to a host operation.

All runtime values remain JSON values except opaque host handles for bounded
HTTP bytes and package assets. Handles can flow only into operations that
declare them; they cannot be inspected as Python objects, formatted into a
template, or stored in state.

### Initial operations

Data and deterministic transforms:

- `state.get`, `state.set`, `state.delete`
- `text.concat`, `text.replace_literal`
- `json.pick`, `json.merge`
- `math.add`, `math.subtract`, `math.negate`, `math.clamp`
- `random.integer`, `random.choice`
- `if`, `return`

Host capabilities:

- `model.text`, `model.structured`
- `http.request`
- `context.append`
- `draft.replace`
- `artifact.emit`
- `ui.status`, `ui.toast`, `ui.invalidate`

Do not expose general regular expressions in v1. Python's backtracking regex
engine would turn an innocent-looking text operation into a denial-of-service
surface. A future bounded RE2-backed operation can be additive.

Do not expose arbitrary SSE event handlers. There is no community JavaScript to
receive them. The three host UI operations above cover progress, notification,
and repaint.

### Operation semantics

Compilation maps every operation to its required capability, allowed hook
stages, input/output type, quota counter, and allowed sinks. It validates all
reachable branches conservatively; putting a privileged operation behind
`when: false` does not hide it from permission consent.

`state.get/set/delete` address only the current extension's slot. Paths are
validated JSON-pointer-like segments, never SQLite JSON paths supplied by the
package. Config, conversation, message, and character writes require the
corresponding entity to exist in the invocation context. A post-turn write to
the not-yet-created assistant message is staged into the pipeline result and
committed with that message.

`model.text` and `model.structured` use the selected configured lane's
transport, credentials, model, and ordinary model parameters, but **not** its
Orb system prompt, conversation prefix, attachments, cached tools, or
`CachedBase`. The request contains only a small host-owned safety preamble plus
the flow-produced prompt. Conversation/card data reaches that prompt only
through granted `ExtensionCtx` fields. `model.structured` validates the decoded
value against the compiled local schema; it does not register a function tool.

`random.integer` and `random.choice` use a host-owned per-invocation PRNG, never
module-global randomness. The seed and flow revision are recorded in artifact
recovery metadata. Regenerate reuses the seed; reroll receives a new seed.
Ordinary hook/action invocations derive a stable seed from host invocation
identity plus extension ID, flow ID, and content digest.

`context.append`, `draft.replace`, `artifact.emit`, branch activation, and all
state/UI effects are staged until successful return. A flow may stage at most
one draft replacement and one branch activation. Multiple state writes to the
same path are folded in program order before the final slot-size check.

For an in-turn post hook, effects tied to the pending assistant message
(message state, artifacts, and their view invalidations) remain in the pipeline
result and commit with message persistence. The host publishes their
invalidations only after that commit, so the frontend cannot refetch between a
successful hook and the assistant row it describes.

`return` ends execution and validates its value against the entry point's
declared output schema, if present. Falling off the end returns JSON `null`.

### Execution limits

Per flow invocation:

- At most 128 executed steps and nesting depth 8.
- At most 2 model calls and 4 HTTP requests.
- Model output request capped at 4,096 tokens and 1 MiB after decoding.
- HTTP request body capped at 1 MiB; timeout 30 seconds; at most 3 redirects;
  decompressed response capped at 5 MiB.
- State value capped at 256 KiB per extension per scope.
- One `context.append` block capped at 8 KiB UTF-8; all extension blocks for one
  model target capped at 32 KiB per turn.
- Template/result values capped before concatenation so intermediate values
  cannot bypass final quotas.
- JSON container depth 32, at most 1,024 members per value, and no `NaN`,
  infinities, duplicate keys, or non-string object keys.
- At most one final draft of 1 MiB and one action return value of 1 MiB.

Cancellation follows the owning turn, action request, or client disconnect. A
hook failure aborts that extension invocation and is logged; it does not abort
the core turn. An explicit UI action receives a sanitized error response.

`on_error` defaults to `stop`. A step may declare `on_error: "continue"` only
when it also declares an explicit `fallback` value, so later paths never depend
on an undefined output.

---

## 6. Capability-filtered context

Never hand community flows the existing workflow context objects directly.
They include trusted objects and a settings snapshot that may contain endpoint
URLs, API keys, proxies, and other values a community package must not see.

The adapter builds a plain JSON `ExtensionCtx` containing only granted fields:

```json
{
  "extension_id": "scene-meter",
  "hook": "post_pipeline",
  "conversation": {
    "id": "..."
  },
  "message": {
    "id": 42
  },
  "character": {
    "id": "...",
    "name": "..."
  },
  "input": {
    "last_user_message": "..."
  },
  "draft": "...",
  "history": []
}
```

Each field is absent unless the package has the matching capability. Context
objects are deep-copied JSON values with size and count bounds, not proxy views
of live core state.

Initial permission vocabulary:

| Capability | Meaning |
|---|---|
| `context.input.read` | Read the effective current user message. |
| `context.draft.read` | Read the current post-writer draft. |
| `context.history.read` | Read a bounded active-path history window. |
| `context.character.read` | Read an allowlisted character projection; avatar bytes and raw extensions require separate future capabilities. |
| `conversation.tree.read` | Read all message-node metadata in the active conversation. |
| `conversation.tree.previews` | Also read bounded content previews from inactive branches. |
| `conversation.branch.activate` | Change the active branch through Orb's locked host action. |
| `prompt.context.append` | Add a per-turn trailing context block for Director, Writer, or both. |
| `draft.replace` | Replace a post-pipeline draft once. |
| `model.call` | Make a flow-owned call on the declared Writer or Agent lane. |
| `state.read` / `state.write` | Access only this extension's config/conversation/message/character slot. |
| `artifact.write` | Emit workflow attachments under the existing artifact contract. |
| `network.request` | Use exact declared origins through the host client. |
| `ui.contribute` | Place commands/views into exact declared slots. |
| `fragment_type.contribute` | Register namespaced fragment-type descriptors. |

Model-call consent must state that the extension can incur token cost. Reading
inactive branches and sending any conversation data to a network origin are
separate, conspicuous grants.

History contains role and bounded text only: no attachments, workflow state,
progressive fields, logs, reasoning, or inactive messages. Character context is
an explicit allowlist of textual card fields; it excludes avatar bytes, raw card
extensions, endpoint settings, and persona data. Every variable-length
projection has both an item count and aggregate UTF-8 byte cap.

### Prompt placement and KV cache

Community v1 does not expose the existing `system_prompt` workflow event.
Dynamic extension context belongs in the current turn's trailing message, like
lorebook and Scene Direction data, so it does not invalidate cached history.

Add a new internal workflow control event, `context_block`, carrying:

```json
{
  "targets": ["director", "writer"],
  "label": "Extension label",
  "text": "..."
}
```

The pipeline collects these blocks in deterministic extension-ID order and
threads them into the appropriate trailing prompts. Director blocks are placed
with other per-turn Director context before the host's tool instruction. Writer
blocks are placed with lorebook/Scene Direction context before the effective
user message. `build_writer_content()` still runs once, so the Editor replays
the exact extension-augmented writer message. Enforce the 8 KiB per-block and
32 KiB combined-per-target limits before prompt assembly. Reject the
overflowing extension invocation with a diagnostic; do not silently truncate
or let installation order decide which text survives.

Extension flows may force their own structured model output, but their schemas
do not enter `TOOLS`, `PRE_WRITER_TOOLS`, `POST_WRITER_TOOLS`, or any
Director/Writer/Editor `CachedBase`. Enabling an ordinary extension therefore
does not change the main tool blob. A flow-owned call uses an isolated request,
validates the response against its local schema, and never mutates a shared
cached request object. It also does not inherit the pipeline prefix: that would
leak history/card/persona data around the capability projection.

---

## 7. Native UI contribution contract

Community UI is a validated component tree rendered by Orb with DOM creation
and `textContent`. No component accepts raw HTML, raw CSS, JavaScript, SVG,
iframe content, event-handler source, or unrestricted URLs.

### Extension manager

The host needs an Orb-owned management surface before it needs contributed UI.
Add an Extensions panel that can:

- Inspect a Git URL or upload a local `.orbext`.
- Show identity, exact commit/digest, compatibility, derived requirements,
  requested permissions, origins, secret names, package limits, and update
  permission diff before install.
- Install enabled or disabled; enable/disable later.
- Show load status and sanitized diagnostics.
- Inspect/apply an update, rollback, uninstall, and separately preview/confirm
  purge.
- Grant/revoke requested permissions and edit write-only secrets.

Consent controls and diagnostics are host components populated from server
inspection results, never package views. The frontend sends the opaque staging
token and the exact normalized grants the user approved; it never reconstructs
permissions from display strings.

### Slots

Initial named slots:

- `composer.menu` — the burger menu next to the user input.
- `mobile.chat_actions` — optional explicit mobile placement; the host may also
  mirror `composer.menu` where appropriate.
- `tools`
- `inspector`
- `message.toolbar`
- `message.after`
- `artifact.body`
- `workspace` — a host modal/full-size panel opened by a command.

The current composer burger is fixed markup in `frontend/index.html`. Replace
its item list with a framework-owned menu model that combines built-in commands
and enabled extension placements. Do not let extensions insert DOM nodes.

### Commands

Commands separate an action from its placements:

```json
{
  "commands": [
    {
      "id": "open-tree",
      "label": "Conversation Map",
      "icon": "git-branch",
      "opens": "conversation-tree",
      "when": {
        "exists": {
          "$ref": "host.active_conversation_id"
        }
      }
    }
  ],
  "placements": [
    {
      "slot": "composer.menu",
      "command": "open-tree"
    }
  ]
}
```

Icons are Orb-owned symbolic names, not asset URLs or markup. Availability
predicates read a small host state projection and cannot call flow operations.

### Components

Initial components:

- Layout: `stack`, `grid`, `card`, `divider`, `tabs`
- Content: `text`, sanitized `markdown`, `badge`, `list`, `table`
- Inputs: `text-input`, `textarea`, `number-input`, `select`, `toggle`, `button`
- Status: `progress`, `meter`, `empty-state`, `error`
- Media: `image`, `audio`, `video` from validated package assets or workflow
  artifacts
- Structured views: `tree` and the higher-level `conversation-tree`

Styling is tokenized (`tone`, `size`, `density`, `columns`, `align`, `span`).
Unknown properties fail validation rather than becoming DOM attributes.

View data comes from:

- A declared host resource.
- The extension's config or state projection.
- A named flow action's JSON return.

Buttons dispatch named actions. Host controls such as tabs and disclosure
panels may update ephemeral renderer state without a backend round trip.
Action inputs are validated by a declared local schema. Action results use the
fixed host effect envelope from Section 2; packages do not provide fetch URLs,
HTTP methods, refetch callbacks, or DOM targets.

Form controls bind only to declared config/state paths and keep an ephemeral
draft until submit. Submission runs a host-generated state action under normal
permission, schema, size, lock, and transaction checks. Merely rendering a view
never writes state.

The renderer keys local ephemeral state by
`(extension_id, active_digest, view_id, instance_id)` and drops it when the
digest/view disappears. Hot reload reconstructs declarative commands and views
from the latest catalog generation; it never appends duplicate handlers to the
trusted workflow registries.

Serve package assets through an API route, never by mounting the package
directory under `/static`. MIME-sniff bytes, allowlist media types, set
`X-Content-Type-Options: nosniff`, and use restrictive content disposition for
anything not explicitly safe to render inline. The route resolves an exact
compiled asset key to a digest-owned descriptor; it never joins the request path
onto a directory.

---

## 8. Reference use case: conversation map

### Desired behavior

An extension adds **Conversation Map** to `composer.menu`. It opens a GitLens-like
tree showing every message branch, highlights the active path, collapses
subtrees, shows optional short previews, and activates a branch when the user
selects a node.

### Missing core resource

`GET /api/conversations/{cid}/messages` currently returns only the active
root-to-leaf path. Each active node receives sibling count/index and previous/
next sibling IDs, but inactive descendants are not returned. A tree view cannot
reconstruct the full graph from that response.

Add a single-query, attachment-free tree projection:

```json
{
  "conversation_id": "conv-1",
  "active_leaf_id": 92,
  "active_path": [1, 2, 8, 20, 92],
  "nodes": [
    {
      "id": 20,
      "parent_id": 8,
      "role": "assistant",
      "turn_index": 4,
      "created_at": "2026-07-27T12:00:00Z",
      "child_count": 3,
      "preview": "She pauses before answering…"
    }
  ]
}
```

`preview` is omitted unless `conversation.tree.previews` was granted. Bound its
length and return no attachments, workflow state, progressive fields, logs, or
full content.

Give the resource an explicit host node-count and encoded-response budget. A
conversation beyond the budget returns a structured `resource_too_large`
diagnostic; it is never silently truncated into a graph that looks complete.
The limit is part of the extension API feature metadata so a future paginated
tree resource can be additive.

Implement this as a database projection plus an extension host-resource
adapter, not by calling the existing active-path HTTP route and not by handing a
flow a database query primitive. The declarative view requests the named
`conversation.tree` resource through
`GET /api/extensions/{id}/resources/conversation-tree?conversation_id=...`;
the server derives preview inclusion from the current grant. A direct core tree
route may also be exposed for Orb-owned UI, but a package cannot choose its
fields.

The generic renderer, not the package, computes layout and draws connectors.
`conversation-tree` receives nodes, active path, collapse state, and a named
select action.

### Branch activation

The host operation `conversation.branch.activate` must call the same core
behavior as the existing switch-branch route:

- Validate the message belongs to the active conversation.
- Hold `_conversation_stream_lock(cid)`.
- Resolve the selected node's deepest descendant and update
  `active_leaf_id`.
- Return/refetch the active path and Director state.
- Broadcast the standard cross-tab mutation and repaint messages.

An extension cannot write `active_leaf_id` directly or call arbitrary internal
routes.

The current branch helper (`switch_to_branch` in
`backend/database/queries/messages.py`) performs descendant lookup and update
through separate database connections. Refactor it into one transaction-aware
database operation, and move/share the conversation stream lock through a
downward dependency that both the message route and extension host operation
may use. Do not import `api.deps` from `features/extensions/`.

Concretely, the lock to move is `_conversation_stream_lock` (and its
try-acquire sibling `stream_idle_lock`, used by the greeting re-roll path) in
`backend/api/deps.py` — today the only home for per-conversation stream
serialization, guarding `/edit`, `/delete`, `/switch-branch`, and the SSE turn
routes. `api/deps.py` sits in the `api/` layer, so `features/extensions/`
(below `api/` in the layer stack) cannot import it without breaking the
one-way rule; the fix is to relocate the lock dict and both context managers
into `backend/core/locks.py` and have `api/deps.py` import them back rather
than own them. `api/deps.py`'s own docstring calls out
`_conversation_stream_locks` as a monkeypatch seam tests reach by canonical
module path (`backend.api.deps`, not a facade re-export) — every test
patching it moves with the lock, so audit that seam explicitly rather than
discovering it as flaky-test churn after the move.

Successful activation returns the fixed
`conversation.messages`/`conversation.director`/`conversation.direction_notes`
effects. The frontend applies the same refetch and inspected-message cleanup as
`switchBranch()`, then refreshes the tree resource. This keeps the extension
path behaviorally identical to the built-in branch buttons.

Optional node labels/bookmarks belong in the extension's message state,
namespaced by its extension ID. They do not alter the conversation schema.

---

## 9. Domain contribution: extension-defined fragment types

Fragment types are a constrained domain extension point, not arbitrary pipeline
hooks. A descriptor may participate only in this fixed lifecycle:

1. Render additional fragment-instance configuration.
2. Contribute one validated property schema to Orb's existing `direct_scene`
   tool.
3. Add prior branch state to the Director's trailing request.
4. Reduce the Director output to a normalized next state through a declarative
   flow.
5. Persist that state on the produced assistant message.
6. Add a formatted value to the Writer's trailing Scene Direction context.
7. Render the current value through host components.

It cannot add a new model tool, new pipeline pass, database query, or persistence
location.

The contribution registry belongs in the lower `workflows/` layer (with pure
descriptor value contracts in `core/` if needed). `features/extensions/`
publishes compiled community descriptors into that registry; the pipeline reads
the captured snapshot and normalizes fragments before calling `inference/`.
Neither `pipeline/` nor `inference/` imports `features/extensions/`, and
`database/` never consults the live registry. This preserves the layer stack and
allows stored unknown providers to remain inert data.

### IDs and data model

Stored extension type IDs are namespaced:

```text
<extension-id>:<local-type-id>
```

Add `type_config TEXT NOT NULL DEFAULT '{}'` to
`interactive_fragments`, mirrored in schema, migration, row contract, CRUD
schemas/queries, preset tests, and the fragment editor. Decode it only at query
boundaries that promise a dict.

Card-embedded interactive fragments may carry the same `type_config` object.
Run it through the installed type's instance schema before the fragment reaches
the pipeline.

### Missing provider behavior

Current imported-card behavior converts an unknown `field_type` to `string`.
That is unsafe for extension types: uninstalling an extension would silently
change the Director schema and writer injection.

Change the rule:

- Preserve namespaced unknown types in stored/card JSON.
- Mark them unavailable in editing UI and name the missing extension.
- Skip them in pipeline assembly.
- Never coerce them to a core type.
- Reinstalling the provider revalidates and reactivates them.
- Retain the existing fallback-to-`string` behavior only for malformed,
  non-namespaced legacy card values.

Creating or editing a global fragment with an unavailable type is rejected, but
existing/imported values remain preserved.

Installed but invalid instances behave like missing providers: the editor shows
the descriptor/config error and the pipeline skips them without rewriting
storage. At most 50 active extension-backed fragment instances are resolved per
turn across global and card sources; excess instances are diagnosed and
skipped in deterministic fragment order.

### Descriptor

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
            "minimum": {
              "type": "integer"
            },
            "maximum": {
              "type": "integer"
            },
            "initial": {
              "type": "integer"
            },
            "max_delta": {
              "type": "integer",
              "minimum": 1,
              "maximum": 100
            }
          },
          "required": ["minimum", "maximum", "initial", "max_delta"],
          "additionalProperties": false
        },
        "director_schema": {
          "type": "object",
          "properties": {
            "delta": {
              "type": "integer",
              "minimum": {
                "$neg_config": "max_delta"
              },
              "maximum": {
                "$config": "max_delta"
              }
            },
            "reason": {
              "type": "string",
              "maxLength": 160
            }
          },
          "required": ["delta", "reason"],
          "additionalProperties": false
        },
        "prior_context": {
          "$template": "{{fragment.injection_label}} is currently {{fragment.previous.value}}."
        },
        "reduce_flow": "flows/reduce-meter.json",
        "writer_context": {
          "$template": "{{fragment.injection_label}}: {{fragment.current.value}} — {{fragment.current.reason}}"
        },
        "config_view": "ui/meter-config.json",
        "value_view": "ui/meter.json"
      }
    ]
  }
}
```

`$config` and `$neg_config` are the only schema-template forms in v1. They may
fill numeric JSON Schema keywords from validated integer fragment config. They
are not general expressions. Resolve the template and validate the resulting
schema before including it in the per-turn override map.

`assistant_progressive` stores the reduced JSON value in the existing
`messages.progressive_fields[fragment.id]` map. Refactor the progressive helper
to ask the fragment-type registry whether a fragment uses this storage policy
instead of hard-coding only `field_type == "progressive"`.

This is not a pure rename. Core progressive fragments today have the
Director's output *be* the persisted value: `passes/director/progressive.py`'s
`select()` filters `extra_fields` down to progressive ids, and that filtered
dict is the next `progressive_fields` state with no transform in between. An
extension-typed fragment breaks that identity on purpose — the Director's raw
output (Meter's `{delta, reason}`) is reducer *input*, not the persisted
value; the reducer's return is. `TurnState.extra_fields` and
`progressive.select`/`branch_baseline` need to split into two stages instead
of one filter: collect each fragment's raw Director output first (unchanged
plumbing for core `string`/`array`/`progressive`, new plumbing for extension
types), then run the reduce step (identity for core types, the compiled
`reduce_flow` for extension types) before anything is treated as the turn's
`progressive_fields` output. Persistence and branch-baseline rewind stay keyed
on the post-reduce value either way, so regeneration/branching correctness is
unaffected — only where the seam between "what the Director said" and "what
gets stored" sits.

Reducer flows are a stricter flow profile: only reference/template,
predicate, text/JSON/math, `if`, and `return` operations are allowed. They
cannot call a model/network, read/write workflow state, emit UI/artifacts, or
mutate the draft/context. Each reducer receives validated
`fragment.config`, `fragment.previous`, and `fragment.director`; its result is
validated before use. All reducers share an additional per-turn step/byte
budget so many fragment instances cannot multiply the ordinary per-flow quota.

Resolution for one turn is:

1. Capture the runtime snapshot.
2. Merge enabled global and card fragments with the existing global-ID
   precedence.
3. Resolve each field type and validate `type_config`.
4. Build every dynamic tool schema once from the resulting immutable fragment
   set.
5. Validate Director values and run pure reducers after the Director call.
6. Persist all normalized `assistant_progressive` values, including unchanged
   prior/initial values, on the produced assistant message.
7. Render Writer context from the same normalized current values.

If a Director value or reducer fails, carry forward the validated prior value
(or validated configured initial value), record a sanitized diagnostic, and continue the
turn. Carry-forward must still be persisted; otherwise the next branch baseline
would silently lose the value.

### Reference type: Meter

A Meter models Trust, Tension, Suspicion, Corruption, Affection, or a similar
bounded quantity.

Example instance:

```json
{
  "id": "trust",
  "label": "Trust",
  "description": "How much Mara currently trusts the user.",
  "field_type": "scene-meter:meter",
  "injection_label": "Mara's trust",
  "type_config": {
    "minimum": 0,
    "maximum": 100,
    "initial": 40,
    "max_delta": 10
  }
}
```

The Director returns a bounded delta plus a reason. The reducer adds it to the
prior value and clamps to the configured range. Orb persists, injects, and
renders the result. Because the value lives on each assistant message,
regeneration and branch switching rewind it through the same
`progressive_fields` baseline already used by core progressive fragments.

The prior value belongs in the Director's trailing request, not the tool schema.
The schema therefore stays byte-stable across turns. Editing meter
configuration changes the schema once, just like editing a core fragment does;
all model passes still receive the same schema blob built once for that turn.

---

## 10. Artifact contract

Community extensions use the existing workflow attachment cache and lifecycle.
They do not receive filesystem paths.

`artifact.emit` accepts bytes already held by the interpreter from:

- A bounded host HTTP response.
- A safe package asset.
- UTF-8 text/JSON produced by a prior step.

If a manifest permits artifact emission, it must declare
`produces_artifacts: true` and provide both `regenerate` and `reroll_gen` flows,
preserving the existing `WorkflowMandateError` invariant. Recovery metadata,
seed behavior, byte-budget eviction, sibling grouping, and validation remain
framework-owned.

Framework recovery metadata also records the producing extension version and
content digest. Regenerate/reroll always execute the **currently active**
revision under its current grants, passing the prior metadata through the
declared recovery input schema. Packages are responsible for backward-compatible
recovery inputs. If an update no longer accepts old metadata, the operation
fails with a sanitized "artifact was produced by an incompatible revision"
diagnostic and leaves the existing attachment untouched; Orb does not silently
execute an old package revision or retain old permissions.

`artifact.emit` in a post hook attaches to the assistant message being
persisted. In an action it requires an explicit target message already validated
to belong to the action's conversation. No flow receives attachment filesystem
paths or arbitrary message IDs from host context.

---

## 11. Network and secrets

`http.request` is the only outbound network operation available to a flow.

### Origin grants

The manifest declares exact origins (`scheme://host:port`). Installation shows
them individually. Grants distinguish:

- Public HTTPS.
- Loopback.
- Private/link-local LAN.
- Plain HTTP.

Local/private or plain-HTTP origins require a stronger warning. Do not allow
wildcard hosts, wildcard ports, URL userinfo, `file:`, Unix sockets, or
package-controlled proxy settings.

### Host client requirements

- `trust_env=False`; do not inherit environment proxies.
- Resolve and validate the destination before connecting.
- Revalidate every redirect and resolved address.
- Block loopback/private/link-local/multicast/reserved addresses unless the
  matching stronger grant exists.
- Pin the validated address for the connection while preserving HTTP Host and
  TLS SNI, preventing a second unvalidated DNS resolution.
- Limit request/response bytes, redirects, and wall time.
- Redact secret values and authorization headers from logs and errors.

### Secrets

Secrets are declared by name and edited only in an Orb-owned form. The API is
write-only: reads return presence metadata, never the value. Flows may reference
a secret only in declared header or body positions; never in a URL, log,
template, UI value, state write, SSE payload, or returned error.

At-rest storage follows Orb's existing local SQLite secret posture; v1 must not
claim encryption that Orb does not provide. The security improvement is
non-disclosure to package logic and frontend payloads.

Secret placeholders compile to opaque request substitutions; they are not
ordinary flow values and `http.request` returns no rendered request headers or
body. Before any response becomes a flow value or any value reaches a log,
error, state, UI, SSE, or second request, scan for exact configured secret byte
sequences and reject/redact matches. This prevents ordinary reflection of the
literal value.

Consent must still say that a secret is transmitted to each named origin. Orb
cannot prevent an origin that legitimately receives a secret from transforming
or retaining it, so the design must not claim that taint scanning makes a
malicious granted origin trustworthy.

---

## 12. Git and archive installation

Use Dulwich, whose documented purpose is Git access without invoking the Git
executable:
<https://www.dulwich.io/docs/>.

### Supported sources

- Any public HTTPS Git host that supports a shallow fetch of the selected ref.
- Optional explicit branch/tag/ref.
- Local `.orbext` ZIP for development/offline install.

SSH, Git's unauthenticated `git://` protocol, private-repository credentials,
submodules, and LFS are deferred.

### Safe fetch

1. Parse and normalize the URL.
2. Apply the installer network policy. A local/private repository requires
   explicit confirmation just like an extension network origin.
3. Use an Orb-owned Dulwich HTTP transport that applies the same
   `trust_env=False`, address validation/pinning, redirect, TLS Host/SNI, byte,
   and timeout rules as the flow HTTP client. Do not fall back to Dulwich's
   ambient proxy/credential behavior.
4. Shallow-fetch only the selected ref into a temporary bare object store with
   a hard received-pack limit. If the server cannot honor the bounded fetch,
   reject instead of downloading full history.
5. Resolve and record the commit ID.
6. Walk the commit tree as Git objects. Never checkout.
7. Reject symlink modes, submodule/gitlink modes, path traversal, absolute
   paths, NULs, duplicate normalized paths, case-folding collisions, excessive
   entries, and size violations.
8. Read and validate `orb-extension.json`.
9. Materialize only allowlisted files referenced by the validated manifest into
   a content-addressed staging directory.
10. Hash the canonical selected package content.
11. Return an inspection result and permission diff. Do not activate yet.

No system Git means no hooks, configured filters, credential helpers, checkout
smudge commands, or package-selected subprocesses. Never run a package manager,
compiler, build, or lifecycle command.

### Two-phase API

Route family:

```text
POST   /api/extensions/inspect
POST   /api/extensions/inspect-file
POST   /api/extensions/install
GET    /api/extensions
GET    /api/extensions/{id}
POST   /api/extensions/{id}/enabled
POST   /api/extensions/{id}/inspect-update
POST   /api/extensions/{id}/update
POST   /api/extensions/{id}/inspect-rollback
POST   /api/extensions/{id}/rollback
PUT    /api/extensions/{id}/permissions
PUT    /api/extensions/{id}/secrets
POST   /api/extensions/{id}/actions/{action}
GET    /api/extensions/{id}/views/{view}
GET    /api/extensions/{id}/resources/{resource}
GET    /api/extensions/{id}/assets/{path}
DELETE /api/extensions/{id}
POST   /api/extensions/{id}/purge-data
```

Package inspection returns an opaque, expiring staging token bound to the
package digest, operation (`install`, `update`, or `rollback`), extension ID,
and the active digest observed during revision inspection. The digest binds the
derived requirement set because apply recompiles the durable staged bytes
before normalizing the submitted grants. Tokens are short-lived, single-use,
stored outside the package, invalid after restart, and pin their content digest
against garbage collection while live. Installation accepts that token plus
the exact approved permission/origin set and desired enablement. Revalidate the
staged digest and token before commit. An update fails with `409` if the active
digest changed since inspection.

Updates are the same inspect/consent/apply sequence. Reject an update whose
manifest ID changes. Permission reductions need no new consent; additions or
broadened origins do. Activate atomically and keep the prior content digest for
rollback. Reapplying an identical active digest preserves the existing rollback
pointer rather than erasing it.

Rollback is also an inspected operation. It shows the prior manifest and
permission diff and cannot restore a capability/origin the user has not
currently approved without fresh consent.

The permissions route accepts only a normalized subset of the active manifest's
request. Expansion is an explicit consent action in the Orb manager; reduction
takes effect immediately, republishes the runtime overlay, and may unpublish
dependent entry points. A package cannot request or approve permissions through
one of its own actions/views.

Normal uninstall removes package registration and secrets but preserves
extension config, namespaced state, unavailable fragment instances, and
existing attachments. `purge-data` is a separate destructive operation that
names exactly what it will remove. Its first response is a per-store row-count
preview plus an opaque token whose server-side payload fingerprints the exact
selected primary keys. Confirmation starts a write transaction, recomputes
that selection, and returns `409` without deleting anything if it changed, so a
stale UI cannot authorize a broader purge. Purge-by-ID remains available for a
valid uninstalled community extension ID, while invalid IDs, reserved IDs, and
built-in workflow IDs are rejected. The manager/storage view lists orphaned
namespaced data; otherwise "uninstall but preserve data" would make later
cleanup unreachable.

Before destructive purge, atomically persist the disabled settings/package
state and publish the disabled overlay before deleting
state/attachments/fragments. Keep it disabled afterward. Phase 1 has no
community executor and therefore no community invocation to drain; the phase
that adds execution must prevent new invocations and drain or cancel active
ones before deletion. Otherwise an in-flight flow could commit the data again
after the purge response.

---

## 13. Persistence and preset policy

Add:

### `extension_packages`

- `id` primary key
- source kind, normalized source URL, and requested ref
- active and previous content digests
- approved permissions JSON
- enabled catalog mirror (written atomically with
  `settings.workflow_enabled[extension_id]`)
- install/update timestamps
- load status and sanitized error

### `extension_revisions`

- `(extension_id, content_digest)` primary key
- canonical manifest JSON and extension API/version metadata
- resolved commit when the source is Git
- compiled-contract fingerprint
- first-seen timestamp
- `ON DELETE CASCADE` to package metadata

Keep only the active and previous revision rows. A revision activation upserts
the new active record and prunes older rows in the same transaction; an
identical-digest activation preserves the prior rollback pointer. Revision
rows, rather than only two digest strings on the package row, give rollback
enough manifest/commit information to recompile and show an honest permission
diff. Content directories displaced by those pointers are collected only after
publish and only when no runtime or staging root still names them.

### `extension_secrets`

- `(extension_id, name)` primary key
- secret value
- updated timestamp
- `ON DELETE CASCADE` to package metadata

Package files live under a content-addressed directory derived from
`dirname(DB_PATH)`, for example `data/extensions/objects/<digest>`, not inside
the frontend tree. Derive paths from validated digests; never persist an
arbitrary path supplied by a package. Tests must derive this root dynamically
from the monkeypatched database path just as the preset engine does.

Inspection fsyncs and atomically renames the complete content directory before
it issues a token. Activation then uses the short lifecycle-mutation lock:
recompile and verify those durable staged bytes, commit
revision/package/grants/enablement metadata in one database transaction, rebuild
and publish the installed-package snapshot, then collect unreferenced content
and release the mutation lock. Readers remain lock-free and observe the old or
new immutable snapshot. A crash can leave only an unreferenced content
directory (startup GC removes it) or committed metadata that startup
deterministically reloads; it cannot point at a half-written directory.

Existing state locations remain authoritative:

- Global config: `settings.workflow_config[extension_id]`
- Conversation: `conversations.workflow_state[extension_id]`
- Message: `messages.workflow_state[extension_id]`
- Character: `character_cards.workflow_state[extension_id]`
- Artifacts: `workflow_attachments.workflow_id`

Every state API and interpreter commit enforces the 256 KiB cap against the
canonical encoded extension slot, not the whole containing JSON column. Purge
removes selected slots from all four scopes with parameterized JSON paths,
deletes selected workflow attachments, and optionally deletes global/card
fragment instances whose `field_type` belongs to the extension. SQL `LIKE`
metacharacters are escaped even though lifecycle validation admits only the
extension-ID grammar. Preview and destructive writes use the same selection
helper; apply compares the selected-row fingerprint inside `BEGIN IMMEDIATE`
before deleting.

### Presets

The current preset coverage policy requires every table to belong to a domain
or be excluded, and intentionally rejects placing live user data in the generic
excluded set. Add an explicit `LOCAL_ONLY_TABLES` policy rather than abusing
`EXCLUDED_TABLES`.

`LOCAL_ONLY_TABLES` contains `extension_packages`, `extension_revisions`, and
`extension_secrets`. Local-only behavior:

- Omitted from partial/shareable presets and merges.
- Retained in full local DB snapshots so a local rollback also rolls back
  installation metadata.
- Secret values participate in the existing key-stripping path.
- If a full DB is moved to a machine without the corresponding content-addressed
  files, mark the package unavailable and offer an explicit refetch of the
  recorded commit. Archive-installed revisions require re-upload. Never fetch
  automatically during startup or restore.

Portable chat/character presets may contain inert namespaced extension state.
Preserve it. It becomes meaningful again if the user installs the provider.

Update `schema.py`, migration, database row contracts/queries, preset policy,
fresh-install stamping tests, and preset coverage tests together.

---

## 14. Registry and frontend loading

The current registry is populated and finalized at Python import time. Community
packages load only after the database is initialized.

Required registry changes:

- Add owner/source metadata (`builtin` or `community`).
- Separate the finalized built-in base from a replaceable community overlay;
  do not expose general unregister for built-ins.
- Add hook-stage metadata and build copy-on-write immutable registry snapshots.
- Change `iter_subscriptions`, `get_subscription`, `get_workflow`, and manifest
  reads to resolve against an explicit captured snapshot.
- Capture the snapshot in each public pipeline entry point and thread it through
  `PipelineContext`, `_TurnSetup`, `_run_pipeline`, both workflow bridges,
  extension fragment resolution, and persistence. Do not independently read the
  global pointer at pre- and post-hook time.
- Preserve built-in priority/insertion order. Community ordering is
  deterministic by hook stage, priority, then extension ID, not installation
  time.
- Re-run artifact mandate and declaration validation before publishing a new
  snapshot.
- Never let a community operation mutate the inference tool registry.

Startup initialization runs after `init_db()`/migrations and before the FastAPI
lifespan yields. It compiles every installed active revision independently,
publishes all valid definitions in one snapshot, and records per-package
sanitized load status without blocking Orb for a bad package.

`GET /api/workflows` adds:

```json
{
  "source": "builtin",
  "frontend_kind": "trusted_module"
}
```

or:

```json
{
  "source": "community",
  "frontend_kind": "declarative",
  "extension_api": 1
}
```

`workflow_loader.js` dynamically imports only `trusted_module` entries.
Declarative entries are registered with the host component/command renderer.
An extension directory is never mounted as same-origin static content.

Today `loadWorkflowModules` has no source check at all: it iterates every
`S.workflowManifest` entry and runs
`import(\`/static/workflows/${w.id}/index.js\`)`, trusting only that every id
currently reaching the manifest belongs to a shipped, reviewed built-in. The
`source`/`frontend_kind` filter must land in the same change that first lets a
community id reach `GET /api/workflows` — Phase 0 step 5 exercises
disabled/unavailable community records on that endpoint, so the loader gate
has to already be in place by then, not follow as later polish, or that step
ships a live `import()`-by-package-string window.

Install/update/uninstall should hot-reload safely. In-flight flow objects keep
their already-compiled JSON and do not read package files lazily, so replacing
or garbage-collecting a content directory cannot change an active turn.

The frontend adds `extensionCatalog` and `extensionRuntimeGeneration` to
`state.js`. Lifecycle responses and a generalized cross-tab mutation event
trigger a catalog/workflow-manifest refetch. Trusted modules are imported only
for the fixed built-in entries; community commands/views are replaced as one
generation-keyed declarative model. No community record is ever interpolated
into `import()`, `innerHTML`, an inline handler, or an existing
`workflow_registry.js` callback array.

---

## 15. Validation and failure behavior

Use strict Pydantic discriminated unions for manifest, permission, operation,
component, and contribution shapes. Use a deliberately supported JSON Schema
subset for package-defined data:

- Local schemas only; no external `$ref`.
- Bounded depth, properties, enum length, string limits, and aggregate size.
- Reject unknown schema keywords in v1.
- `additionalProperties` defaults to `false`.
- Validate defaults as well as user/runtime values.

Install is transactional:

- Fetch/validation/compilation/permission preview happen before persistence.
- A rejected install leaves no active registry entry.
- A rejected update leaves the prior package, grants, registry definition, and
  files active.
- Activation publishes database metadata and registry snapshot only after the
  content-addressed directory is durable.

Startup:

- Invalid/missing package content does not block Orb.
- Recompile the active content and compare its contract fingerprint with the
  stored active revision. A mismatch is incompatible and inert until an
  inspected update records fresh consent, even when the package digest is
  unchanged.
- Mark only that package unavailable, show its sanitized diagnostic, and skip
  registration.
- Built-in workflows continue loading.

Runtime:

- Capability checks happen both at compilation and immediately before each
  privileged host operation.
- Revoking a grant prevents the next operation even if a stale compiled flow
  still references it.
- Repeated flow failures do not auto-grant, auto-update, or crash the process.

---

## 16. Concrete codebase change map

Keep the dependency stack intact. `api/` owns HTTP orchestration;
`features/extensions/` owns package/contract/compiler/interpreter behavior and
may register into lower `workflows/`; `pipeline/` consumes only lower runtime
contracts/snapshots; `inference/` and `database/` never import the extension
feature.

| Area | Required modifications |
|---|---|
| `backend/features/extensions/` (new) | Strict manifest/flow/component/schema models; duplicate-key JSON loader; package reference walker/compiler; immutable compiled records; interpreter and effect staging; capability/context projection; package/CAS/staging lifecycle; safe archive/Git readers; mediated HTTP/secrets; host resources; startup reconciliation. Split these by responsibility rather than one extension manager module. |
| `backend/core/locks.py` | Expose the conversation stream lock through a downward-safe owner or add an equivalent core lock service; add any message/extension lifecycle locks needed by transaction commits. Preserve current lock ordering and document it to prevent stream/workflow/character deadlocks. |
| `backend/database/` | Add package/revision/secret tables, `interactive_fragments.type_config`, migrations, models, facades, and transaction-aware queries. Add an attachment-free full-tree projection and atomic branch activation. Add bounded namespaced-state and purge helpers. Update `schema.py`, fresh bootstrap/stamping, seeds where applicable, and preset policy together. |
| `backend/workflows/contracts.py` / `registry.py` / `enablement.py` | Add source/frontend kind, hook stage, immutable built-in-base + community-overlay snapshots, generation, snapshot-aware lookup/iteration, scoped community replacement, and artifact declaration validation. Do not route community tool declarations through `register_tool`. |
| `backend/pipeline/entrypoints.py`, `context.py`, `state.py` | Capture/thread the runtime snapshot; resolve extension fragment providers before building schema; carry Director/Writer context-block collections and extension diagnostics; keep one writer content value for Editor replay. |
| `backend/pipeline/workflow_bridge.py` / `orchestrator.py` | Adapt trusted contexts to `ExtensionCtx`; run staged declarative hooks in explicit transform/observe phases; consume only fixed control effects; commit post-message state/artifacts with the assistant result; preserve failure isolation and cancellation. |
| `backend/pipeline/config.py`, `passes/director/**`, `passes/writer.py`, `persistence.py` | Replace hard-coded progressive/string/array decisions with resolved fragment descriptors; build the dynamic schema once; validate/reduce/carry forward extension values; render descriptor writer context; persist normalized progressive values. Keep `feedback` and `direction_note` dedicated behaviors while moving ordinary string/array/progressive handling behind the common descriptor contract. |
| `backend/inference/` | Keep the main tool registry extension-blind. `tool_registry.py:build_direct_scene_tool` currently emits only two property shapes per fragment (array-of-string, or plain string) from a literal `field_type` branch; replace that branch with a lookup that accepts a pre-resolved JSON-schema property dict per fragment id, so a core type keeps synthesizing its own shape through the same seam an extension type's `director_schema` fills. `prompt_builder.py` carries four more `field_type` branches that must move the same way: the progressive-prior-value filter and the per-field-type hint text in `build_director_scene_step_prompt`, the progressive-only prior-value line beside it, and the array/progressive split in `format_message_with_attachments`'s Scene Direction rendering. All four must consume pre-rendered strings/schemas the pipeline resolved from the fragment-type registry, never switch on a type string themselves. If shared helpers are needed beyond that, add an isolated no-prefix model-call path and schema-value validation that `features/extensions/` may call downward. |
| `backend/api/routes/extensions.py` (new) | Inspect/install/catalog/detail/enable/update/rollback/permissions/secrets/actions/views/resources/assets/uninstall/purge routes. Use strict request/response schemas, staging tokens, lifecycle lock, generation checks, fixed effect envelopes, and no package-selected routes. Register the router in `api/routes/__init__.py`. |
| `backend/api/__init__.py` | After DB initialization/migrations, reconcile content, compile installed revisions, and publish the initial snapshot before serving. Shutdown cancels/drains extension invocations and removes staging data. |
| `frontend/extension_manager.js` (new) | Orb-owned inspect/consent/status/update/rollback/secrets/uninstall/purge UI. Never render package-provided consent text as markup. |
| `frontend/extension_renderer.js`, `extension_commands.js` (new) | Validated component creation, form drafts, named action dispatch, workspace/view lifecycle, fixed effect handling, safe media sources, symbolic icons, and generation-keyed disposal. |
| `frontend/index.html`, `chat_composer.js`, `chat_inspector.js`, `chat_messages.js`, `chat_workflow.js` | Convert the burger/mobile menus to host command models; expose declared slots without raw DOM access; integrate message/artifact/workspace views; route branch effects through the same refresh behavior as built-in switching. |
| `frontend/state.js`, boot/loader, `tabLock.js` | Declare extension catalog/generation/view state; load only trusted modules; replace declarative catalog atomically; generalize cross-tab mutation invalidation without weakening the existing send lock. |
| `frontend/library_fragments.js`, `validate.js`, card editor | Populate field types from the host descriptor catalog; render descriptor config views; preserve/display unavailable namespaced providers; include `type_config`; retain existing validation/fallback only for legacy non-namespaced unknown types. |
| Tests and docs | Contract golden files, malicious package corpus, interpreter unit tests, lifecycle/concurrency integration tests, frontend renderer/XSS tests, KV parity, preset/fresh-install coverage, card-fragment regression, and reference extension fixtures. Update `secondary-workflow.md`, `kv-cache.md`, `card-fragments.md`, and AGENTS architecture notes when implementation lands. |

The first implementation PR should establish contracts, migration/preset policy,
and snapshot seams without adding a permissive placeholder executor. Avoid
temporary `dict[str, Any]` package contracts or a "run arbitrary operation"
switch: those are difficult to tighten after extension packages exist.

---

## 17. Implementation sequence

Build this as gated vertical slices. A phase is not done until its failure and
concurrency tests pass; later phases must not weaken earlier trust boundaries.

### Phase 0 — Freeze contracts and runtime seams

1. Freeze strict manifest, permission, flow, value, schema, component, action,
   effect, and fragment descriptor v1 models.
2. Add the duplicate-key/limit-aware JSON loader, canonical digest rules, and
   hostile package fixtures.
3. Refactor the trusted registry into built-in base plus immutable community
   overlay snapshots, including hook stages and explicit snapshot threading.
4. Add package/revision/secret/type-config migrations and preset policy, but do
   not execute packages yet.
5. Add disabled/unavailable community records to `/api/workflows` and prove
   trusted modules still load and tool blobs remain identical.

Exit gate: old workflow/pipeline/preset/card tests pass; registry concurrency
tests prove one turn cannot mix generations.

### Phase 1 — Local package lifecycle and management UI

1. Implement bounded `.orbext` inspection, reference-graph compilation,
   content-pinning staging tokens, CAS durability, startup contract-fingerprint
   reconciliation, and GC rooted by active/rollback/runtime/staging digests.
2. Implement inspect/install/list/detail/enable/update/rollback/uninstall/purge
   for packages that have no privileged executable entry point yet. Enablement
   mirrors must commit together; revision retention is active plus rollback;
   purge revalidates its exact previewed selection transactionally.
3. Build the Orb-owned extension manager, consent diff, status, diagnostics,
   and cross-tab catalog refresh.
4. Exercise missing content, incompatible API/features, crash recovery,
   rejected update, rollback, and preserved inert state.

Exit gate: a declarative metadata-only package can traverse its full lifecycle
without any package file becoming browser/server code.

### Phase 2 — Flow runtime and turn integration

1. Implement resolver/predicate/pure operations, quotas, deterministic random,
   cancellation, sanitized errors, and staged effect transaction.
2. Add `ExtensionCtx`, derived permission sets, live pre-operation grant checks,
   and namespaced state with locks/size limits.
3. Add isolated model calls, pre `context_block`, post transform/observe
   adapters, fixed progress/invalidation events, and KV parity tests.
4. Add on-demand actions and action input/output schemas, initially without
   network or artifact bytes.
5. Ship a minimal Scene Meter hook/action fixture using a structured model
   call plus conversation state.

Exit gate: failure at every step boundary leaves no partial Orb mutation; hook
ordering/failure isolation and permission revocation are integration-tested.

### Phase 3 — Host UI and Conversation Map vertical slice

1. Build the safe component renderer, form drafts, commands, workspace, and
   generation-keyed disposal.
2. Convert desktop/mobile burger menus to the host command model, then add
   tools, inspector, message, and artifact slots incrementally.
3. Add fixed effect handling and generalized cross-tab invalidation.
4. Add the full-tree database projection/resource adapter, atomic shared branch
   action, and `conversation-tree`.
5. Ship Conversation Map as the first complete reference package.

Exit gate: renderer XSS/property fuzz tests pass and selecting a tree node has
the same lock, state refresh, and cross-tab behavior as `switchBranch()`.

### Phase 4 — Network, secrets, Git, and artifacts

1. Implement the pinned-address HTTP client, exact-origin grants, redirect
   revalidation, response handles, and byte/time caps.
2. Add write-only secret editing/substitution, reflection scanning, redaction,
   and permission UI.
3. Add `artifact.emit`, framework recovery metadata, current-revision
   regenerate/reroll behavior, and update incompatibility diagnostics.
4. Reuse the installer URL policy for bounded Dulwich shallow fetch/object-tree
   walking; keep local archive install as the simpler fallback.
5. Ship API Artifact as the reference package.

Exit gate: SSRF/DNS-rebinding/secret-leak corpus and artifact lifecycle tests
pass on Linux, macOS, and Windows path/network variants.

### Phase 5 — Fragment-type contributions

1. Add the lower fragment-type catalog and normalize built-in
   string/array/progressive behavior behind descriptors without changing
   feedback/direction-note behavior.
2. Add `type_config` CRUD/card transport/editor support and preserve unavailable
   namespaced types.
3. Compile schema templates and pure reducer profiles; integrate
   validate/reduce/carry-forward/render/persist with one captured snapshot.
4. Ship Meter as the reference contributed type and test regeneration,
   branching, configuration edits, provider loss, and card import.

Exit gate: dynamic schemas are built once and byte-identical across all passes;
missing/invalid providers never coerce or erase stored data.

### Phase 6 — Hardening and developer experience

Ship a small `orb-ext` CLI:

```text
orb-ext init
orb-ext validate
orb-ext test --fixture <file>
orb-ext pack
```

The CLI is author tooling, not an install-time build system. Orb installs its
canonical JSON output and never runs the CLI from a package.

Add parser/compiler fuzzing, performance budgets, startup/load diagnostics,
storage cleanup observability, complete schema/golden fixtures, and author
documentation. Re-run the full lint, pyright, frontend layer, fresh-install,
preset, SSE, KV, and workflow suites.

Reference extensions:

1. Conversation Map — command placement, full-tree resource, workspace, branch
   action.
2. Scene Meter — structured model call, state, inspector meter.
3. API Artifact — origin consent, secret header, byte response, regeneration
   and reroll.

---

## 18. Acceptance tests

### No executable package path

- Package Python/JS/HTML/CSS/SVG cannot be referenced, served, imported, or
  executed.
- Installer never invokes Git, a shell, package manager, compiler, hook,
  filter, or credential helper.
- Community workflow entries never reach dynamic `import()`.
- Package paths cannot escape the content store through traversal, symlink,
  Unicode/case collision, archive metadata, or Git modes.

### Validation and limits

- Fuzz manifest, flow, schema, template, component, and Git tree parsing.
- Reject duplicate JSON keys, non-finite values, forward/missing references,
  undeclared derived requirements, and referenced-file cycles.
- Reject cycles, excessive nesting/steps, oversized intermediate values,
  unknown operations/components, and schema bombs.
- Enforce the exact model, HTTP, state, context, archive, tree-entry, and
  decompressed-byte quotas at their streaming boundaries.
- Cancellation stops outstanding HTTP/model work and releases locks.
- A bad hook never prevents the assistant turn from persisting.

### Lifecycle, snapshots, and effects

- Install/update/rollback publish content, DB metadata, grants, enablement, and
  runtime generation as one observed transition.
- A failure while persisting the authoritative enablement map rolls back the
  package/revision row and its catalog enablement mirror together.
- A live inspection remains installable across unrelated lifecycle GC; expired,
  discarded, and restart-cleared tokens release their content pin.
- Reapplying identical content preserves rollback; a third distinct activation
  retains only active plus rollback revision metadata/content.
- Startup refuses a recompiled contract fingerprint that differs from the
  consent record, and an inspected same-digest update can record fresh consent.
- A failed update leaves the old files, metadata, grants, commands, fragment
  descriptors, and runtime snapshot active.
- A turn that starts before an update uses one old snapshot for context,
  fragments, pre-hooks, post-hooks, and persistence; the next turn uses the new
  snapshot.
- Startup isolates missing/invalid packages and loads valid packages plus all
  built-ins.
- Disabled/unavailable/under-granted entry points have no hook or placement but
  remain visible with diagnostics.
- A flow failure after each possible staged effect leaves state, draft,
  context, branch, attachments, and view revisions unchanged.
- Transform hooks run before all observers; observers receive one immutable
  final draft. Ordering is independent of install time.
- Purge rejects built-in/reserved/invalid namespaces. Preview and confirmation
  select the same primary keys inside a write transaction; newly added rows
  produce `409` and no deletion. Normal uninstall does not remove inert state
  or attachments.

### Permissions and privacy

- Every privileged operation fails after grant revocation.
- `ExtensionCtx` never contains API keys, endpoint credentials, proxies, raw
  settings, DB/toolkit/client objects, or ungranted fields.
- Inactive branch previews require their distinct grant.
- Model-call and network grants are shown separately.
- Flow-owned model calls contain no Orb system prompt, cached prefix, tools,
  attachments, persona, card, or history unless that data was explicitly
  projected and used by the flow.
- Secrets never appear in reads, state, UI, logs, SSE, errors, URLs, or
  shareable presets.

### Network

- Block redirect-to-private, alternate-IP encodings, IPv4-mapped IPv6,
  link-local metadata addresses, DNS rebinding, wildcard origins, URL userinfo,
  and proxy inheritance.
- Enforce byte/time/redirect/request caps on compressed and decompressed data.
- Installer fetches receive equivalent URL/address validation.

### UI

- XSS payloads in every string, Markdown, manifest field, view model, state
  value, filename, and remote response render as text or sanitized content.
- Unknown properties never become DOM attributes.
- Asset responses use sniffed allowlisted MIME and `nosniff`.
- Disabling/uninstalling removes command placements without leaving handlers.
- Replacing a catalog generation disposes old view/form state and never
  duplicates a command, action, or renderer.
- Fixed action effects drive the same local and cross-tab refetches; unknown
  package-provided effect/resource/slot names cannot become selectors or
  callbacks.

### Conversation Map

- Within its declared budget, tree projection returns every node exactly once
  and identifies the active path; an oversized tree returns an explicit error,
  not a partial graph.
- It does not return attachments, state, logs, or content without preview
  permission.
- Selecting a foreign message fails.
- Branch activation serializes against an active stream and refreshes
  messages, Director state, and other tabs.
- Branch lookup plus `active_leaf_id` update is one database transaction and
  the extension action matches the built-in switch route.

### Fragment contributions

- Meter config validates; deltas clamp; branch regeneration/switching restores
  the correct prior value.
- Installed type schema is built once and byte-identical across all passes.
- Per-turn prior values appear only in trailing messages, not schema bytes.
- Missing extension types remain stored and visible-but-unavailable, never
  coerced to `string` or injected.
- Invalid Director/reducer output carries forward and persists the prior or
  initial normalized value.
- Reducer flows cannot reach model, network, state, artifact, UI, draft, or
  prompt operations and obey the aggregate per-turn budget.
- Card fragment caps, ID precedence, and untrusted normalization still hold.

### Compatibility

- Existing first-party workflow, artifact, frontend-layer, SSE, fresh-install,
  preset, card-fragment, and pyright tests stay green.
- Enabling an ordinary extension does not change the main model tool blob.
- Trusted workflow entries are the only manifest records that reach dynamic
  `import()`.
- Applying old presets and loading old cards with only core fragment types is
  unchanged.

---

## 19. Deferred beyond v1

- WASM or any general-purpose package code.
- Extension-defined Agent/Writer/Editor tools or new model passes.
- Background services, timers, daemons, or startup jobs.
- Extension dependencies.
- Arbitrary DOM/CSS, custom canvas code, iframe applications, or browser
  storage access.
- SSH/private Git authentication, submodules, or LFS.
- Wildcard network permissions.
- General regular expressions.
- Arbitrary database tables, migrations, SQL, filesystem paths, or raw HTTP
  routes.
- Automatic update installation.

These may be added only as new, separately threat-modeled capabilities. They
must not be smuggled through permissive v1 fields.
