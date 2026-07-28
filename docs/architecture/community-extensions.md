# Community Extensions — Architecture Handoff

Status: **Phases 0-6 implemented (including all of section 20's v1.x
expansions), less the `orb-ext` CLI, which was deliberately deferred.
`extension_api: 2` and its one Writer-tool contribution are implemented — see
[Community Writer Tools](community-writer-tools.md).**

The frozen v1 contract has no community tools in the main pipeline, and that
remains true of v1. API 2 adds exactly one narrow exception: a v2 package may
contribute a single **Writer** tool, the user selects at most one active
resolver across the whole install, and the selected schema joins the Writer's
tool blob for that turn. Community Director and Editor tools remain out of
scope, and no community name ever enters the mutable inference `TOOLS` registry.

### Sequencing against Community Writer Tools

Phase 5's exit gate was met and the Writer-tool slice then landed in WT0→WT4
order. Phase 5 was serialized ahead of those pipeline phases for three reasons:

1. **Phase 5 closed a granted capability that did nothing.**
   `fragment_type.contribute` parsed, consented, and compiled before a runtime
   consumer existed.
2. **`extension_api: 2` could not arrive while API 1 was incomplete.** The
   Writer-tool plan's compatibility story rests on API 1 naming one frozen
   contract. Shipping v2 first makes API 1 permanently mean "v1 minus fragment
   types," and every package published in the interval targets a contract with
   a dead grant.
3. **Risk and seam order.** A reducer is the strictest flow profile — no model,
   network, state, UI, or first-party write — so Phase 5 added no external
   surface. WT2 puts package-authored description text in every Writer prompt
   and replaces the one-shot Writer pass with a bounded loop. Phase 5 also
   performed the `TurnState`/schema refactor WT2 builds on: splitting "what
   the Director said" from "what gets stored," and replacing the literal
   `field_type` branches in `tool_registry.py` and `prompt_builder.py` with
   pre-resolved lookups. Reversed, WT2 splits per-lane schema assembly over
   hard-coded branches and the KV-parity matrix is written twice.

WT0 was explicitly *not* serialized behind Phase 5 and was the first
Writer-tool step. Versioned manifest dispatch, the core Writer-tool ABI values, the
`writer.tool.contribute` spec entry, and `OpContext.WRITER_TOOL` touch no
pipeline code; the fragment-type manifest contract in
`contracts/manifest.py` landed in Phase 0, so the two barely share lines. WT0
is also the part that gets worse
with delay: it exists because v1 models use `extra="forbid"`, so a host without
it misreports a v2 package as malformed instead of as a future API. WT1
(registry binding, activation persistence, route, manager control) is likewise
pipeline-free and may land in the same window.

WT2 onward had to remain serialized while Phase 5 was in flight. Both plans
rewrite `pipeline/context.py`, `config.py`, `state.py`, and the Writer/Director
passes; that overlap was resolved by the snapshot-driven fragment path.

Section 20 records the approved v1.x additive expansions — new resources,
grants, one operation, one slot, and host telemetry — with their security
posture and a normative implementation order. It amends section 17's Phase 3
step 7: `list.join` replaces the scalar-array template rendering rule, which
was specified but never implemented. That amendment has landed: templates
remain scalar-only in `values.py`, `list.join` is the way to render a list, and
`tests/unit/extensions/test_phase3_operations.py` asserts that interpolating an
array still fails as a plain scalar violation.

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

**Phase 2 — flow runtime and turn integration**

- Runtime value resolution (`values.py`): the validated path resolver, template
  substitution, the structured predicate evaluator, and the `MISSING` sentinel
  that keeps "absent" distinct from a stored `null`. Comparisons are
  type-strict and total: an incomparable pair is `false`, never an exception
  escaping a `when` guard.
- The capability-filtered `ExtensionCtx` (`ctx.py`), *constructed* from the
  approved grant set rather than filtered down from `PreCtx`/`PostCtx`, with
  per-field and aggregate byte caps on every variable-length projection.
- The interpreter (`interpreter.py`): the pure/data operations, per-invocation
  quotas, the host-owned seeded PRNG, cancellation, sanitized `FlowError`
  messages, the live pre-operation grant re-check, and the staged effect
  record. `http.request`, `artifact.emit`, and `conversation.branch.activate`
  parse and consent normally but are listed in `UNIMPLEMENTED_OPS`, so an entry
  point reaching one is blocked with a diagnostic instead of failing halfway.
- The adapters (`adapters.py`): compiled flows bound as `pre_pipeline` /
  `post_pipeline` subscriptions and named actions, deriving their own lock plan
  from the flow's state scopes, revalidating the complete effect set at the
  commit boundary, and committing every database-backed scope through one
  SQLite transaction under those locks.
  `pipeline/workflow_bridge.py` no longer wraps a community subscription in the
  workflow state locks — `asyncio.Lock` is not reentrant, and the adapter takes
  them itself.
- Invocation lifecycle coordination (`execution.py`): disable/uninstall/purge
  block starts retained by older registry snapshots, purge drains active
  invocations before deletion, and shutdown cancels/drains all remaining work.
- Prefix-free model calls (`inference/isolated.py`): a host safety preamble plus
  the flow's prompt and nothing else. Structured output ships an ephemeral tool
  schema forced through `tool_choice`, built per call and never entering
  `TOOLS` or any pass's `CachedBase`.
- The `context_block` control event, the pipeline's per-target collection with
  the 8 KiB / 32 KiB budgets, and `ExtensionContext` threaded into the Director
  and Writer *trailing* messages beside the lorebook and Scene Direction blocks.
- `POST /api/extensions/{id}/actions/{action}`, resolving an exact compiled
  binding from the manifest's declared action map.
- The Scene Meter fixture (`tests/extension_packages.py`) plus interpreter unit
  tests and turn/action integration tests.

**Phase 3 — host UI, resources, and the two reference packages**

- Canonical host character-tag normalization (`core/tags.py`): trim, clip on a
  character boundary, drop empties, dedupe case-insensitively, cap per-tag
  bytes and per-card count. `update_character_card` is the one write path, so
  the character API and `card.tags.set` store byte-identical lists; the chip
  widget was changed to match the casing rule, and import is deliberately left
  unnormalized. It landed first, as section 17 requires.
- `list.intersect` and `list.join` (closed separator set), plus `card.tags.set`
  — action-only, no card argument, both grants checked, host-normalized, and
  committed through the ordinary card update path.
- `conversation.branch.activate`, with `_conversation_stream_lock` /
  `stream_idle_lock` relocated to `core/locks.py` (`api/deps.py` imports them
  back) and `switch_to_branch` rewritten as one transaction-aware operation.
  The adapter takes the stream lock *outside* the state locks, matching the
  pipeline's order.
- Host resources (`resources.py`): `conversation.tree` (fails past its budget
  rather than truncating), and the cursor-paginated `library.cards`,
  `lorebook.entries`, and `direction.notes`, plus the bounded singleton
  `persona`. Cursors are authenticated, MAC'd, and bound to the resource that
  issued them; packages treat them as opaque protocol tokens.
- The `character.card` effect, `ctx.character` resolution from validated action
  input behind both `context.read` for `character` and `library.cards.read`, and the
  `library.card_actions` slot, whose host-supplied card id needs no enumeration
  grant and is checked against the compiled placements.
- The routes the earlier phases deferred: `GET /{id}/views/{view}`,
  `GET /{id}/resources/{resource}`, `GET /{id}/assets/{path}`, and the
  host-generated `PUT /{id}/state` a bound form submits through.
- `frontend/extension_renderer.js` (DOM creation and `textContent` throughout,
  tokenized styling, media by reference, node-built Markdown) and
  `frontend/extension_commands.js` (the host command model shared with the
  built-in burger menus, slot placement, workspace lifecycle, the fixed
  effect-to-refetch mapping with `character.card` debouncing, and the
  renderer-driven library sweep).
- Section 20 in full: `list.join`, invocation telemetry, the consent
  combination banner, the `views.config` convention with its config-scope
  restriction, the three read resources, and the `library.card_actions` slot.
- Conversation Map and Tag Librarian as reference packages
  (`tests/extension_packages.py`), with `test_host_surfaces.py`,
  `test_phase3_operations.py`, `extension_renderer.test.mjs`, and
  `extension_commands.test.mjs`.

**Phase 4 — network, secrets, Git, and artifacts**

- The host HTTP client (`network.py`): canonical origin derivation, the URL
  policy (no userinfo, no wildcard, no non-http scheme, no control
  characters), address validation that judges *every* resolved address, and
  connection pinning that keeps the `Host` header and TLS SNI on the origin's
  real name. Redirects are followed by Orb rather than by httpx, and each hop
  is revalidated against the live grant set and the address policy; a
  cross-origin hop drops every package-supplied header. `trust_env=False`,
  bounded request body, decompressed-response cap enforced at the streaming
  boundary, a redirect budget, and a wall-clock deadline across hops.
  `network.request` is granted per exact origin. A weak transport (all plain
  HTTP) receives stronger consent copy, but only an origin whose hostname is
  visibly local may resolve to a local address; transport warning and
  local-address authority are separate decisions. That distinction prevents
  `http://public.example` from rebinding to loopback.
- `http.request` returns `{"status", "body"}`. `json`/`text` bodies become
  ordinary flow values; `bytes` becomes an opaque `ResponseBytes` handle with
  no scalar rendering, so a template, a state write, or a return value rejects
  it through bounds that already existed. `artifact.emit` is the only operation
  that declares it as an input.
- Write-only secrets (`secrets.py`, `PUT /{id}/secrets`): `{"$secret": name}`
  survives value resolution as a marker and becomes a value only inside the
  network client, so the interpreter never holds one. A header value may be a
  list of literal/secret parts. The only statement in the codebase that selects
  `secret_value` is called by that client and by nothing else. Responses are
  scanned for the exact configured byte sequences before becoming a flow value.
- `artifact.emit` with its three declared byte sources (a response handle, a
  validated package asset, text/JSON from a prior step), an inert-media-type
  allowlist, host-owned filename reduction, and a per-invocation count and byte
  budget. A post hook attaches to the assistant row and may not name a message;
  an action must name one and it is proved to belong to the invocation while
  *staging*; a recovery flow names none. All three are compile-time rules, which
  is why `OpContext.RECOVERY` exists — a recovery flow also cannot activate a
  branch or rewrite card tags.
- Framework recovery metadata records the producing extension id, version, and
  content digest beside the package's own `recovery` payload. Regenerate and
  reroll execute the revision from the request's captured registry snapshot
  under *live* grants; a stored payload that captured
  `recovery_input_schema` rejects fails with the sanitized "produced by an
  incompatible revision" diagnostic and leaves the attachment untouched.
  `produces_artifacts` and the regenerate/reroll pair
  publish together or not at all, because the registry's artifact mandate fails
  the whole overlay swap on a half-bound record.
- Bounded Git installation (`git_source.py`): an in-process Dulwich shallow
  fetch through an Orb-owned `pool_manager` that applies the flow client's URL
  and address policy per hop, a byte-bounded pack sink, and an object-tree walk
  that rejects symlinks, submodules, non-regular modes, duplicate normalized
  paths, and case collisions. Nothing is checked out and no system Git is
  invoked. `POST /api/extensions/inspect` and
  `POST /{id}/inspect-update-git` feed the same two-phase consent flow an
  archive uses; the resolved commit rides in the staging token, so the apply
  request cannot claim an origin it was not fetched from. Dulwich is imported
  lazily, so a deployment without it still boots and still installs archives.
- API Artifact as the fourth reference package (`tests/extension_packages.py`),
  with `test_network.py`, `test_phase4_operations.py`, `test_git_source.py`, and
  the live-socket `tests/integration/extensions/test_phase4.py`.

**Permission vocabulary generalization** (post-Phase 3, pre-freeze)

The vocabulary was restructured before any package could depend on it: facts
about a grant that lived in seven unlinked tables now live in one
`CAPABILITY_SPECS` descriptor, and scopes that were encoded in capability
*names* became parameters. `context.{input,draft,history,character,persona}.read`
is `context.read` with a `field`; `conversation.tree.previews` is a `preview`
field of `conversation.tree.read`; `card.tags.write` is `card.write` with a
`field`. Sixteen capabilities where there were twenty-one, with the same number
of consent rows and the same `permission_key()` tuples. Section 6 has the rule
and its two corollaries; `tests/unit/extensions/test_capability_vocabulary.py`
holds the derivations closed.

Phase 5 closes the last deliberately absent v1 runtime surface:

- `workflows/fragment_types.py` owns the descriptor runtime contract, the
  built-in string/array/progressive definitions, and the shared reducer budget.
  Community workflow records and immutable snapshots carry compiled
  descriptors.
- `pipeline/fragment_types.py` resolves instance config once against the
  captured snapshot, prepares prior context, validates raw Director output,
  reduces it, carries prior/initial state on failure, and pre-renders Writer
  context. `TurnState.director_fields` keeps raw output separate from normalized
  `extra_fields` and persisted `progressive_fields`. Unavailable progressive
  providers stay out of schemas/prompts while their bounded stored values pass
  through inertly until the provider returns.
- The fragment API publishes the host type catalog and diagnoses unavailable
  providers without rewriting stored rows. Global and card editors round-trip
  `type_config`; imported namespaced types remain visible and inert until their
  provider returns.
- The Scene Meter reference contribution exercises config and value views,
  dynamic schema bounds, a pure reducer, progressive persistence, live grant
  revocation, and provider loss.
- Reducer steps are charged against the shared 512-step allowance before each
  interpreter operation executes; normalized results share 256 KiB, and
  rendered prior/Writer fragment context is capped at 64 KiB UTF-8 per target.
  Sanitized failures persist with the conversation log so Inspector history
  matches the live turn, then follow the ordinary diagnostic-log cleanup policy.

`UNIMPLEMENTED_OPS` remains empty — every operation the contract parses is
executable — and its blocked-entry seam remains tested against a synthetic
entry for future operations whose contract may precede their runtime.

Three route names extend section 12's family. `POST
/api/extensions/{id}/inspect-rollback` exists because rollback is an inspected
operation with a real permission diff (restoring a revision must not restore a
capability since revoked), and it deserves the same two-request shape as
update rather than an implicit mode on `/rollback`. `POST
/api/extensions/{id}/inspect-update-git` is the Git counterpart of
`inspect-update`: same compile, same diff, same token contract, but a JSON body
rather than a multipart upload — a route whose body shape depends on a flag is a
route two clients read differently. `POST /api/extensions/inspect` (Git) and
`PUT /{id}/secrets` are section 12's own, and both landed in Phase 4.

One route is additive to that family and not in section 12's list:
`PUT /api/extensions/{id}/state`. Section 7 says a bound form's submission
"runs a host-generated state action"; this is that action's transport. It
carries no package-authored intent — the host groups the draft by declared bind
scope — and it goes through the same grant check, slot cap, lock order, and
transaction a flow's `state.set` uses.

The `library-sweep` component is likewise additive, and for the reason section 8
gives: the sweep loop belongs in the host renderer, so it needs a declaration a
package can place. It takes an action id, a label, and the state key that marks
a card done; the page size, cursor walk, concurrency, stop condition, and
progress display are all Orb's.

Audience: the engineer implementing Orb's community extension system. This
document is an implementation handoff for v1: trust boundary, required host
behaviors, package and runtime contracts, concrete codebase changes,
persistence, failure behavior, rollout order, and acceptance tests.

Read these first:

- [Community Writer Tools](community-writer-tools.md) — the planned,
  not-yet-implemented API 2 Writer-only tool ABI that follows this v1
  architecture.
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
| Model tools | Frozen v1 packages do **not** add tools to Director, Writer, or Editor. Extension flows may make their own bounded isolated model calls. An API 2 package may contribute one **Writer** tool, of which at most one is user-selected and active per install; see [Community Writer Tools](community-writer-tools.md). Director/Editor tools and arbitrary shared-registry tools remain out of scope, and no community name enters the mutable inference `TOOLS` registry. |
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
3. **Enablement** — `settings.workflow_enabled[extension_id]` permits
   invocation. This is the *only* enablement gate: the Secondary Workflows
   master switch covers the built-in tier and does not reach an extension.

Permission grants are stored separately from all three. The effective runtime
state is:

```text
installed
  AND load_status == available
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
- Writer-tool bindings, keyed by their derived provider-facing name, plus the
  wire name of the one *selected* resolver. Availability and activation are
  separate fields of the same captured generation, so "the schema I sent" and
  "the binding I ran" cannot disagree.
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
  "toasts": [],
  "runtime_generation": 12
}
```

The frontend owns the effect-to-refetch mapping for messages, Director state,
direction notes, character cards, extension views, and the extension catalog.
The same fixed effects drive local repaint and cross-tab broadcast. Coalescing
is the frontend's business too: a host-rendered loop can produce one effect per
iteration (section 8), and the mapping debounces rather than asking the host to
emit fewer — an effect describes what one invocation did. Unknown effects are dropped
and logged; package strings never become event names, DOM selectors, function
names, or module paths. `toasts` is a bounded list of host-rendered
`{text, tone}` notifications produced by `ui.toast`; it is data in the same
validated envelope, not a package-selected event or callback.

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
      "capability": "context.read",
      "field": "draft"
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

**Versions are a table, not a literal.** `SUPPORTED_EXTENSION_APIS` says which
versions this build implements, and `CONTRIBUTION_MIN_API` says which version
each `contributions` slot was introduced in — today, `writer_tool` requires 2. A
v1 manifest that declares a v2 slot is *rejected*, so v1 still means exactly
what it meant and a package cannot acquire semantics its author never declared
by being parsed on a newer Orb. The compiler checks the raw `extension_api`
integer **before** strict parsing, which is what lets an older build report
"this package needs a newer Orb" instead of "this package is malformed" — every
model here forbids extra fields, so without that ordering a future manifest
looks broken rather than new. That ordering is the whole reason a contribution
field warrants a version bump at all.

### Package limits

Apply all limits before persistence:

| Limit | Value |
|---|---:|
| Downloaded Git pack/archive | 50 MiB |
| One expanded Git object | 25 MiB |
| Expanded objects/deltas in one Git pack | 100 MiB total |
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

Templates are pure path substitution over scalars. Arrays — and containers
generally — never interpolate. An earlier draft of this section made an
exception for arrays of scalars (rendered joined by a frozen `", "`), because
any feature driven by a user-managed list needs that list in a prompt. That
exception was specified but never implemented, and it is now withdrawn in
favor of the `list.join` operation (section 20), which takes its separator
from a closed host-owned set and ships in Phase 3 step 7 — before any
published package could depend on the frozen rendering. This keeps the
template contract at exactly one rule with no special cases. Do not reintroduce
the exception, and do not fix a formatting need by giving the template a format
argument; that is where a filter language starts.

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
- `list.intersect`
- `math.add`, `math.subtract`, `math.negate`, `math.clamp`
- `random.integer`, `random.choice`
- `if`, `return`

Host capabilities:

- `model.text`, `model.structured`
- `http.request`
- `context.append`
- `draft.replace`
- `card.tags.set`
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

`list.intersect` returns the members of one bounded string array that also
appear in another, in first-array order, deduplicated. It exists because a
package-declared `output_schema` is compiled at install time and therefore
cannot carry an `enum` drawn from runtime config: an extension whose behavior
is governed by a user-managed vocabulary has no other way to constrain a model
result to that vocabulary. It is deliberately the only list operation in v1 and
is not a general `map`/`filter` — both arrays are capped at 256 members and the
operation performs no per-element evaluation, so it does not reintroduce the
unbounded iteration section 5 excludes.

`model.text` and `model.structured` use the selected configured lane's
transport, credentials, model, and ordinary model parameters, but **not** its
Orb system prompt, conversation prefix, attachments, cached tools, or
`CachedBase`. The request contains only a small host-owned safety preamble plus
the flow-produced prompt. Conversation/card data reaches that prompt only
through granted `ExtensionCtx` fields. `model.structured` validates the decoded
value against the compiled local schema; it does not register a function tool.

`card.tags.set` replaces the `tags` list of **the card already in the
invocation's context** — `ctx.character`, and only that card. It takes no card
identifier. A flow cannot read card A and write card B, so the operation's
blast radius is one card by construction rather than by quota, and it needs
`context.read` for `character` to do anything at all.

This is the same rule section 10 applies to `artifact.emit`, which must prove
its target message belongs to the action's conversation: **every write proves
its target is in the invocation's scope.** Library-wide reach comes from a user
driving a host-rendered loop across cards they can already see (section 8), not
from a grant that widens the write.

It is action-only. A card is shared across every conversation that uses it, so
a per-turn hook silently rewriting library metadata is not a behavior a user can
reasonably supervise.

The host — not the package — normalizes the result: trim, drop empties,
deduplicate case-insensitively, and enforce the per-tag length and per-card
count caps.

This canonical host normalization now lives in `core/tags.py`. It is a narrow
core admission because the single database write path must enforce the invariant
without importing a feature. It does not move card CRUD, import, filtering, UI,
or extension behavior into `core/`. The character API and `card.tags.set` both
reach it through `update_character_card`; the extension operation conforms to
the host limits. The chip widget uses the same case-insensitive,
first-spelling-wins rule. Import deliberately bypasses the helper so
stored/exported author data remains faithful, while extension read projections
apply the same caps ephemerally so an old imported tag list cannot exceed a
resource or context budget.

Given that shared normalizer, `card.tags.set` calls the ordinary card update
path rather than writing the column directly.

Unlike every other write in this document, `card.tags.set` lands in a
first-party column. See "Writes that leave the namespace" in section 13 for
what that costs.

`random.integer` and `random.choice` use a host-owned per-invocation PRNG, never
module-global randomness. The seed and flow revision are recorded in artifact
recovery metadata. Regenerate reuses the seed; reroll receives a new seed.
Ordinary hook/action invocations derive a stable seed from host invocation
identity plus extension ID, flow ID, and content digest.

`context.append`, `draft.replace`, `artifact.emit`, `card.tags.set`, branch
activation, and all state/UI effects are staged until successful return. A flow
may stage at most one draft replacement, one branch activation, and one card
tag write. Multiple state writes to the
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
- At most 1 card tag write, of at most 32 tags of 64 UTF-8 bytes each.
- At most 256 members per `list.intersect` input array.
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

### The unit of consent is a grant, not a capability

A grant is a `(capability, parameter)` pair. `state.write` on `conversation` and
`state.write` on `character` are two grants, approved and revoked separately,
and `permission_key()` has always compared the pair rather than the capability
name. The vocabulary is written to match: where a capability is scoped by
something, the scope is a **parameter**, never a suffix on the name.

This is a design rule and not a formatting preference. A scope encoded in the
name makes every new field a new enum entry, a new consent line, a new
membership in every classification set, and a new branch in the projection —
five edits in five files, none of which the type system links. The first draft
of this vocabulary had twenty-one capabilities for what is structurally sixteen
decisions, because five of them were `context.<field>.read`.

Everything the host needs to know about a grant lives in one place,
`CAPABILITY_SPECS` in `contracts/capabilities.py`: its consent copy, the class
of user data it exposes, its emphasis, the parameter that scopes it and that
parameter's admissible values, the host resource it gates, and its
prerequisites. The consent copy table, the loud set, the data-reading set that
drives the combination banner, the resource-to-grant map, the prerequisite map,
the `Permission` model's admissible parameters, and `UI_SLOTS` are all
*derived* from it. Adding a capability — or a value to an existing one — is one
spec entry, and the copy is a required field, so a grant cannot reach a consent
dialog without a sentence describing it.

| Capability | Parameter | Meaning |
|---|---|---|
| `context.read` | `field`: `input` | Read the effective current user message. |
| `context.read` | `field`: `draft` | Read the current post-writer draft. |
| `context.read` | `field`: `history` | Read a bounded active-path history window. |
| `context.read` | `field`: `character` | Read an allowlisted character projection; avatar bytes and raw extensions require separate future capabilities. |
| `context.read` | `field`: `persona` | Read the active persona's name and description. Also gates the `persona` resource. |
| `conversation.tree.read` | `field`: `structure` | Read all message-node metadata in the active conversation. |
| `conversation.tree.read` | `field`: `preview` | Also read bounded content previews from inactive branches. Requires `structure`. |
| `library.cards.read` | — | Enumerate the card library (id/name/tags plus this extension's own slot), and resolve a card named by validated action input into `ctx.character`. |
| `lorebook.read` | — | Read the lorebook entries of the world bound to the invocation's conversation. |
| `direction_notes.read` | — | Read the active branch's direction notes. |
| `conversation.branch.activate` | — | Change the active branch through Orb's locked host action. |
| `prompt.context.append` | `targets`: `director`, `writer` | Add a per-turn trailing context block. |
| `draft.replace` | — | Replace a post-pipeline draft once. |
| `card.write` | `field`: `tags` | Replace the tag list of the one character card in the invocation's context. Requires `context.read` for `character`. |
| `model.call` | `lane`: `writer`, `agent` | Make a flow-owned call on the declared lane. |
| `state.read` / `state.write` | `scope`: `config`, `conversation`, `message`, `character` | Access only this extension's own slot in that scope. |
| `artifact.write` | — | Emit workflow attachments under the existing artifact contract. |
| `network.request` | `origin` | Use exact declared origins through the host client. |
| `ui.contribute` | `slot` | Place commands/views into exact declared slots. |
| `fragment_type.contribute` | — | Register namespaced fragment-type descriptors. |
| `writer.tool.contribute` | — | Add a callable tool and its instructions to the Writer (API 2 only). Conspicuous on both halves: the description ships in the prompt every turn the tool is active, and the result lands in the transcript the Writer continues from. |

Two rules follow from the parameter being part of the grant, and both are
enforced rather than documented:

- **Prerequisites are per grant.** `GRANT_PREREQUISITES` is keyed by the pair,
  because both v1 cases are: `card.write` needs the character projection only
  for its `tags` field, and tree previews need tree structure without any other
  `conversation.tree.read` value needing anything. The compiler resolves them
  transitively, so an operation's derivation states only what it directly
  reaches.
- **A multi-valued parameter makes the whole entry the unit of approval.** Use
  one only where partial approval is meaningless. `conversation.tree.read` takes
  a *singular* `field` for exactly this reason: previews are a separate
  conspicuous grant, and a list would have quietly made them come with
  structure.

Model-call consent must state that the extension can incur token cost. Reading
inactive branches and sending any conversation data to a network origin are
separate, conspicuous grants.

`card.write` for `tags` is the first grant that writes a first-party column, but it is
**not** a grant whose blast radius exceeds the current invocation. It writes
`ctx.character` and nothing else, so it inherits that projection's scoping:
consent reads "can change the tags on the character a command is run against",
and it is refused outright without `context.read` for `character`.

An earlier draft of this document gave the operation a `card_id` argument drawn
from action input, which made the grant library-wide and required a conspicuous
"all of your characters" consent band. That was avoidable — the only package
that wants it (section 8) already resolves `ctx.character` from the same card —
and it would have carried a permanent exception to the scope rule into v2. It is
recorded here so it is not reintroduced.

History contains role and bounded text only: no attachments, workflow state,
progressive fields, logs, reasoning, or inactive messages. Character context is
an explicit allowlist of textual card fields plus the card's current `tags`
list; it excludes avatar bytes, raw card extensions, endpoint settings, and
persona data. Every variable-length projection has both an item count and
aggregate UTF-8 byte cap.

In a hook, `ctx.character` is the card bound to the turn's conversation. An
action has no such binding — a library-scoped action operates on a card the
user picked, not on the card of whatever chat happens to be open. So when an
action's validated input schema declares a card identifier, the host resolves
that card and populates `ctx.character` from it, using the same allowlist as
the hook path. The package names a card; it never names the *fields*, and it
cannot reach a card by any route that skips the projection.

That resolution requires **both** `context.read` for `character` and
`library.cards.read`. Enumeration is not the only way to reach a card: an
extension that already holds an id — from its own state, from a previous run,
from something the user pasted — would otherwise read any card in the library
under a grant whose consent text says "the character in this conversation".
`context.read` for `character` alone therefore never leaves the current conversation,
whether by listing cards or by naming one.

Resolving `ctx.character` from action input also rebinds the `character` state
scope: `state.get`/`state.set` with `scope: "character"` address the resolved
card's namespaced slot, not the open conversation's card. This is the intended
behavior — a per-card record must live on the card it describes — but it means
one package-supplied identifier moves both the read projection and a write
target. It is the only place in v1 where package input selects an entity, which
is why it is gated by two grants rather than one.

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

Ordinary extension flows may force their own structured model output, but their
schemas do not enter `TOOLS`, `PRE_WRITER_TOOLS`, `POST_WRITER_TOOLS`, or any
Director/Writer/Editor `CachedBase`. Enabling an extension with no dedicated
Writer-tool contribution therefore does not change the main tool blob. A
flow-owned call uses an isolated request, validates the response against its
local schema, and never mutates a shared cached request object. It also does
not inherit the pipeline prefix: that would leak history/card/persona data
around the capability projection.

The API 2 Writer-tool ABI is a deliberate exception with a separate contract,
consent line, immutable snapshot binding, per-turn activation, and bounded
Writer ReAct loop. It still does not register community names in `TOOLS`: the
selected eligible Writer schema is assembled from the captured extension
snapshot, and single-model passes receive one deterministic union for cache
parity. The exact tail OOC policy narrows the Writer to that tool while host
validation remains authoritative. See
[Community Writer Tools](community-writer-tools.md). For a v1 package, and for
a v2 package with no selected resolver, the behavior above is complete and the
main tool blob is unchanged.

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

Buttons dispatch named actions. Host controls such as tabs and disclosure
panels may update ephemeral renderer state without a backend round trip.
Action inputs are validated by a declared local schema. Action results use the
fixed host effect envelope from Section 2; packages do not provide fetch URLs,
HTTP methods, refetch callbacks, or DOM targets.

Opening or refreshing a view never runs an action implicitly. A former draft
included an action-shaped data source, but no safe semantics were implemented:
an on-load action could silently incur model cost or mutate state, and retaining
an action result for a proposal/confirmation UI needs a separately designed
ephemeral result lifecycle. The unimplemented source was removed rather than
published as a no-op.

Form controls bind only to declared config/state paths and keep an ephemeral
draft until submit. Submission runs a host-generated state action under normal
permission, schema, size, lock, and transaction checks. Merely rendering a view
never writes state.

A control's binding also declares the *shape* it stores, not just the key:
`textarea` takes `value_kind` (`text`, the default, or `lines`), and under
`lines` the host splits the box into a trimmed array of non-empty members and
renders a stored array back one per line. Without it a list-shaped setting has
no honest binding — the box would save a string while the flow reading the same
key runs `list.join` over it, which is a silent misconfiguration the user cannot
see from the form. The split is host-owned and the enumeration is closed; a
package declares which shape it wants and never how the conversion happens.

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

## 8. Reference use cases

The two packages in this section are not illustrations chosen after the fact.
Each one was the reason a host resource, operation, or grant below exists, and
each exercises a seam no other part of the design forces: Conversation Map
needs to read a core structure the API does not expose and mutate core state
through a locked host action; Tag Librarian needs a user-driven loop to span the
library while every individual invocation stays scoped to one card, and needs to
write a first-party column.

### Conversation map

#### Desired behavior

An extension adds **Conversation Map** to `composer.menu`. It opens a GitLens-like
tree showing every message branch, highlights the active path, collapses
subtrees, shows optional short previews, and activates a branch when the user
selects a node.

#### Missing core resource

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

`preview` is omitted unless `conversation.tree.read` for `preview` was granted. Bound its
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

Because layout is the host's, so is legibility at length: the renderer folds
each chain of single-child messages into one row carrying a `⋯N` chip, opens
subtrees off the active path collapsed, and never folds past the message the
user is on. A package cannot opt out of that, which is the point — the map
stays a map of branch points instead of growing one row per turn.

#### Branch activation

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

### Library tag standardization

#### Desired behavior

Card tags arrive however the card's author left them: absent, idiosyncratic, or
in someone else's vocabulary. A user who wants a library they can actually
filter maintains **their own** tag vocabulary and wants cards classified
against it.

**Tag Librarian** adds a workspace command to the `tools` slot. The workspace
shows the user's vocabulary in an editable form, lists the cards not yet
classified, and offers a Run button. Running classifies each listed card
against the vocabulary and writes the resulting tags. It is on-demand only —
there is no trigger on import and no background pass. Re-running later picks up
whatever has arrived since, because classification is recorded per card.

#### Why this does not fit one invocation

Every other package in this document does its work inside a single invocation
bound to a single conversation. This one is bounded by neither, and it collides
with three limits at once:

- **Scope.** Every projection in section 6 is invocation-scoped. `ctx.character`
  is the card of the current conversation; nothing enumerates a library.
- **Quotas.** A flow gets 128 steps and 2 model calls. A 300-card library needs
  300 model calls.
- **Duration.** Section 19 defers background services, so there is nowhere to
  put a long-running sweep even if the quotas allowed one.

The resolution is to leave all three limits alone and **move the loop out of
the flow language into the host renderer**. The workspace dispatches one action
per card, sequentially. Each invocation classifies one card with one model call
and one tag write, well inside the existing per-invocation budget, and commits
independently.

That last property is worth stating plainly, because it inverts the usual
batch-job tradeoff: a sweep interrupted at card 87 leaves 86 correctly
classified cards and 214 untouched ones, and the next run resumes from there.
There is no partial commit to reconcile, because there was never one
transaction. Cancellation is the user closing the workspace; the in-flight
action is cancelled by the existing action-request path and every prior card is
already durable.

No new execution model, no new hook stage, and no relaxation of section 5.

##### What an O(n) invocation loop needs that a single invocation did not

Sequential per-card actions are a new *shape* even though every individual
invocation is ordinary. Three things follow from the loop rather than from any
one action, and none of them is visible when reading section 2 alone:

- **Effects must coalesce.** Each invocation stages a `character.card` effect,
  so a 300-card sweep produces 300 invalidations, 300 library refetches, and 300
  cross-tab broadcasts. The frontend debounces `character.card` refetches; the
  host still emits one effect per invocation, because the effect describes what
  that invocation did and coalescing is a rendering concern.
- **A runtime-generation change halts the sweep.** `runtime_generation` rides on
  every envelope and the manager discards responses below the generation it has
  already seen (`extension_manager.js`). Left alone, an update or disable at
  card 87 would leave the server committing writes whose envelopes the frontend
  throws away — work with no feedback and no record in the view. The workspace
  stops the loop when the generation advances, reporting cards completed so far.
  Permission revocation is different and already covered: it fails the next
  `card.tags.set` server-side, which surfaces as an ordinary action error.
- **Library actions take no conversation lock.** The invocation has no
  conversation, so there is nothing to serialize against; taking the stream lock
  per card would put 300 sequential acquisitions in front of any live turn. This
  is stated because `conversation.branch.activate` does take that lock, and the
  two are easy to conflate as "actions that write outside their own namespace".

Concurrency stays at one in flight. That is a deliberate floor, not a
measurement: each card costs a model call on a user-configured lane, and the
design has no rate-limit model for those lanes. Widening it is a change to make
against an observed limit, with a bound the user can see.

#### Missing core resource

Nothing exposes the library to an extension. Add a bounded projection,
following the same rules as the conversation-tree resource:

```json
{
  "cards": [
    {
      "id": "card-1",
      "name": "Mara",
      "tags": ["noir", "detective"],
      "state": { "tagged": true }
    }
  ],
  "next_cursor": "opaque-host-token-or-null"
}
```

`state` is the extension's **own** namespaced
`character_cards.workflow_state[extension_id]` slot and appears only with
`state.read`. It is what makes "cards not yet classified" computable in the
view without an invocation per card. No other extension's namespace is ever
projected, and no card field outside this shape is — in particular not
`description`, which the classifier reads through `ctx.character` during the
action, under `context.read` for `character`, and not here.

The page is bounded by both a card count and an encoded-response budget, and
`next_cursor` is an authenticated host-owned token — a package treats it as
opaque and cannot construct or mutate one. The first page captures a rowid
high-water mark; later pages remain below it, so cards inserted during a sweep
wait for the next run instead of appearing according to how a random public id
sorts. A deletion may remove that card from the remainder, but cannot shift or
duplicate any surviving row. `null` means the listing is complete.

Paginate rather than failing past a budget. A single-response cap would have to
either truncate, which makes a sweep report success over cards it never saw, or
return `resource_too_large`, which permanently locks a large library out of the
feature with no recourse. Neither is acceptable, and the design already has the
answer: the renderer loops cards, so it loops pages the same way. The
`next_cursor` walk is the same "keep the loop in the renderer" move applied one
level up, and the "resume from the remainder" story already requires refetching
between runs. Page assembly stops before adding an item that would cross the
encoded-byte limit; the cursor resumes from the last item actually emitted,
not the last one fetched from SQLite.

Requesting it needs `library.cards.read` (section 6); `context.read` for `character`
alone must not enumerate cards. Serve it from
`GET /api/extensions/{id}/resources/library.cards` as a database projection
plus a host-resource adapter, exactly as with the tree — never by handing a
flow a query primitive.

#### The classify action

The vocabulary lives in the extension's config slot, bound to a form control
per section 7, so editing it is an ordinary host-generated state action under
normal permission and size checks. The per-card record lives in that card's
character-scope state, written by the same invocation that classifies it, so
there is no separate bookkeeping pass that could disagree with what was
actually written.

The action's input schema declares `card_id`. That identifier does three things
at once, all host-side: it resolves `ctx.character` for the classifier prompt,
it selects the `character` state scope for the final bookkeeping write, and it
fixes the single card `card.tags.set` may write. The flow never passes it to an
operation — note that `card.tags.set` below takes only `tags`.

```json
{
  "flow_version": 1,
  "steps": [
    {
      "id": "vocabulary",
      "op": "state.get",
      "scope": "config",
      "path": "vocabulary"
    },
    {
      "id": "vocabulary_text",
      "op": "list.join",
      "value": { "$ref": "steps.vocabulary" },
      "separator": ", "
    },
    {
      "id": "proposed",
      "op": "model.structured",
      "lane": "agent",
      "prompt": {
        "$template": "Choose every tag that applies to this character. Use only tags from the allowed list. Return an empty array if none apply.\n\nAllowed tags: {{steps.vocabulary_text}}\n\nName: {{ctx.character.name}}\n\n{{ctx.character.description}}"
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "tags": {
            "type": "array",
            "items": { "type": "string" },
            "maxItems": 32
          }
        },
        "required": ["tags"],
        "additionalProperties": false
      }
    },
    {
      "id": "allowed",
      "op": "list.intersect",
      "value": { "$ref": "steps.proposed.tags" },
      "allowed": { "$ref": "steps.vocabulary" }
    },
    {
      "op": "card.tags.set",
      "tags": { "$ref": "steps.allowed" }
    },
    {
      "op": "state.set",
      "scope": "character",
      "path": "tagged",
      "value": true
    }
  ]
}
```

The `list.intersect` step is not decoration. A model asked to pick from a list
will occasionally return something adjacent to it, and `output_schema` cannot
express "one of the user's current tags" because schemas compile at install
time while the vocabulary is runtime config. Without that step the extension
would launder invented tags into the library under the user's own vocabulary —
which is precisely the mess it was installed to clean up.

#### Consent

Tag Librarian requests `context.read` for `character`, `library.cards.read`,
`model.call`, `state.read`, `state.write`, `ui.contribute`, and
`card.write` for `tags`.

`library.cards.read` is the grant that carries the weight here, and it is worth
being clear about why it is the enumeration grant rather than the write grant.
`card.write` for `tags` only ever touches the card an invocation was handed, so on its
own it is a single-card permission. What makes this package library-wide is that
it can *see* the library and resolve any card in it — so that is what consent
describes: "list your characters and read each one it is run against." The write
is bounded; the reach is the listing.

That split is the point of the section 6 rule. Reach is granted once, visibly,
by the capability that enumerates; writes stay scoped to whatever the invocation
already holds. A future package that wants to write some other card field adds a
narrow scoped operation, not another library-wide grant.

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

The contribution registry belongs in the lower `workflows/` layer. Its
descriptor contracts belong there too; being pure or shared is not sufficient
reason to put an extension descriptor in `core/`. Only a pre-existing canonical
host value contract that independently satisfies the `AGENTS.md` core admission
rule may be reused from `core/`. `features/extensions/` publishes compiled
community descriptors into that registry; the pipeline reads the captured
snapshot and normalizes fragments before calling `inference/`. Neither
`pipeline/` nor `inference/` imports `features/extensions/`, and `database/`
never consults the live registry. This preserves the layer stack and allows
stored unknown providers to remain inert data.

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

The frozen v1 initial-state convention is deliberately narrow: when validated
instance config contains a top-level `initial` member, the host seeds
`fragment.previous` as `{"value": config.initial}`. A descriptor with no
`initial` member has no host-created prior. This gives Meter a normalized first
value without adding another expression form to the v1 descriptor contract.

`assistant_progressive` stores the reduced JSON value in the existing
`messages.progressive_fields[fragment.id]` map. Descriptor-aware reduction owns
the storage-policy decision; the Director-local progressive helper now owns
only branch-baseline rewind.

This is not a pure rename. Core progressive fragments have the Director's
output *be* the persisted value, while an extension-typed fragment breaks that
identity on purpose — the Director's raw output (Meter's `{delta, reason}`) is
reducer *input*, not the persisted value; the reducer's return is.
`TurnState.director_fields` therefore collects raw Director output first, then
the reduce step (identity for core types, the compiled `reduce_flow` for
extension types) produces `extra_fields` and `progressive_fields`. Persistence
and branch-baseline rewind stay keyed on the post-reduce value either way.

Reducer flows are a stricter flow profile: only reference/template,
predicate, text/JSON/math, `if`, and `return` operations are allowed. They
cannot call a model/network, read/write workflow state, emit UI/artifacts, or
mutate the draft/context. Each reducer receives validated
`fragment.config`, `fragment.previous`, and `fragment.director`; its result is
validated before use. All reducers share an additional per-turn step/byte
budget so many fragment instances cannot multiply the ordinary per-flow quota.
The host reserves each shared step before the interpreter executes it and
refuses to dispatch later reducers after exhaustion.

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

If the provider itself is absent or its stored config no longer validates, the
fragment remains excluded from the model-facing schema and prompts. A bounded
value already present under that fragment id is nevertheless copied into the
next assistant node, so a disable/update/uninstall turn cannot erase branch
state before a compatible provider revision returns.

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
content digest. Regenerate/reroll execute the revision captured when the
request resolves its immutable registry snapshot under live grants, passing
the prior metadata through that revision's recovery input schema. A concurrent
update cannot swap the flow, assets, schema, or provenance midway through an
invocation. Packages are responsible for backward-compatible recovery inputs.
If an update no longer accepts old metadata, the operation fails with a
sanitized "artifact was produced by an incompatible revision" diagnostic and
leaves the existing attachment untouched; Orb does not silently execute an old
package revision or retain old permissions.

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

Local/private or plain-HTTP origins require a stronger warning. That warning is
not local-network authority: a public-looking hostname is still forbidden from
resolving to loopback/private/link-local space even when its scheme is HTTP.
Do not allow wildcard hosts, wildcard ports, URL userinfo, `file:`, Unix
sockets, or package-controlled proxy settings.

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

A multi-secret edit validates the complete batch and commits it in one
transaction. Activating a revision removes stored secret rows that revision no
longer declares; rolling back across that removal requires the user to
configure the old secret again rather than silently reviving a hidden
credential.

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
- Plain HTTP only for an explicitly confirmed repository whose origin and
  resolved addresses are local.
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
   a hard received-pack limit. Preflight the disk-backed pack before indexing:
   bound each advertised object, each delta result, and aggregate expanded
   bytes so a small compressed pack cannot become an unbounded allocation. If
   the server cannot honor the bounded fetch, reject instead of downloading
   full history.
5. Peel annotated tags with a bounded chain, then resolve and record the commit
   ID rather than the tag-object ID.
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
POST   /api/extensions/{id}/enabled          (API only -- the UI toggles via /api/workflows/{id}/enabled)
POST   /api/extensions/{id}/inspect-update
POST   /api/extensions/{id}/inspect-update-git
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

### Writes that leave the namespace

Every state location above is namespaced by extension ID, which is what makes
the lifecycle story coherent: uninstall leaves inert data, purge finds it by
namespace and removes it, and a package's writes are invisible to Orb until the
package is reinstalled.

`card.tags.set` is the one v1 operation that breaks all three properties. It
writes `character_cards.tags`, a first-party column, and the consequences must
be stated rather than discovered:

- **Uninstall does not revert it.** The tags stay on the cards.
- **Purge cannot find it.** Purge selects by namespace; these rows have none.
  There is no "undo this extension's tag writes" operation and v1 does not add
  one — reconstructing prior tag lists would mean journaling every write, which
  is a larger persistence commitment than the feature earns.
- **It leaves the machine.** `tags` round-trips into exported character PNGs
  (`features/cards/parsing.py`) and travels in shareable character presets, so
  an extension write can reach other people's libraries. No other extension
  write can.

Three things bound the damage, and none should be overstated. The operation
writes only `ctx.character`, so one invocation dirties one card and a user who
wants many cards written has to drive the loop themselves. The host normalizes
and caps what gets written, so the worst case is wrong tags rather than
malformed or unbounded ones. And `tags` reaches no prompt anywhere in the
pipeline — it is library metadata for filtering and display only — so a bad
write degrades organization, never generation.

#### The rule for admitting the next one

"It does not reach the prompt" is a necessary condition, not the criterion, and
using it alone would be a mistake this document has already half-made: the
bullet above establishes that `tags` **leaves the machine**, which is a wider
consequence than reaching a local prompt. Prompt-reachability bounds how badly a
write can corrupt *generation*; it says nothing about what the write propagates
to other people.

A first-party write is admissible when all four hold:

1. Its target is fixed by the invocation's own context, never by a package-
   supplied identifier that a grant does not separately cover.
2. A host-owned normalizer, shared with the first-party path, decides the final
   stored value.
3. A bad write degrades organization or presentation, not generation.
4. Its propagation is understood and written down — where it exports to, which
   presets carry it, whose machine it can reach.

`card.tags.set` clears all four, the fourth only because this section states it.
A capability to write a card field that reaches the prompt fails (3) and is a
different threat model needing its own review. A capability that reaches a card
the invocation was not handed fails (1) — an earlier draft of this design did
exactly that, and section 6 records why it was withdrawn. Neither is an extra
entry in an allowlist.

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
- Never let a community operation mutate the inference tool registry. The
  planned Writer-only ABI preserves this rule by publishing dedicated immutable
  bindings in the captured workflow snapshot rather than calling
  `register_tool`.

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
| `backend/database/` | Add package/revision/secret tables, `interactive_fragments.type_config`, migrations, models, facades, and transaction-aware queries. Add an attachment-free full-tree projection and atomic branch activation. Add a snapshot-bounded, cursor-paginated library-card projection (id/name/tags plus one extension's own namespaced slot) with an authenticated host-owned cursor and adaptive byte-sized pages. Enforce the canonical `core/tags.py` host normalization (trim, drop empties, case-insensitive dedupe, per-tag length and per-card count caps) in the single character-card update path; route both the character API and `card.tags.set` through it, and make the chip widget match. Do not backfill existing rows, and leave card import unnormalized so exported PNGs keep author fidelity; bound extension read projections independently. Add bounded namespaced-state and purge helpers. Update `schema.py`, fresh bootstrap/stamping, seeds where applicable, and preset policy together. |
| `backend/workflows/contracts.py` / `registry.py` / `enablement.py` | Add source/frontend kind, hook stage, immutable built-in-base + community-overlay snapshots, generation, snapshot-aware lookup/iteration, scoped community replacement, and artifact declaration validation. Do not route community tool declarations through `register_tool`. The planned dedicated Writer binding extends this snapshot later without weakening that prohibition. |
| `backend/pipeline/entrypoints.py`, `context.py`, `state.py` | Capture/thread the runtime snapshot; resolve extension fragment providers before building schema; carry Director/Writer context-block collections and extension diagnostics; keep one writer content value for Editor replay. |
| `backend/pipeline/workflow_bridge.py` / `orchestrator.py` | Adapt trusted contexts to `ExtensionCtx`; run staged declarative hooks in explicit transform/observe phases; consume only fixed control effects; commit post-message state/artifacts with the assistant result; preserve failure isolation and cancellation. |
| `backend/pipeline/config.py`, `passes/director/**`, `passes/writer.py`, `persistence.py` | Replace hard-coded progressive/string/array decisions with resolved fragment descriptors; build the dynamic schema once; validate/reduce/carry forward extension values; render descriptor writer context; persist normalized progressive values. Keep `feedback` and `direction_note` dedicated behaviors while moving ordinary string/array/progressive handling behind the common descriptor contract. |
| `backend/inference/` | Keep the mutable main tool registry extension-blind. `tool_registry.py:build_direct_scene_tool` currently emits only two property shapes per fragment (array-of-string, or plain string) from a literal `field_type` branch; replace that branch with a lookup that accepts a pre-resolved JSON-schema property dict per fragment id, so a core type keeps synthesizing its own shape through the same seam an extension type's `director_schema` fills. `prompt_builder.py` carries four more `field_type` branches that must move the same way: the progressive-prior-value filter and the per-field-type hint text in `build_director_scene_step_prompt`, the progressive-only prior-value line beside it, and the array/progressive split in `format_message_with_attachments`'s Scene Direction rendering. All four must consume pre-rendered strings/schemas the pipeline resolved from the fragment-type registry, never switch on a type string themselves. If shared helpers are needed beyond that, add an isolated no-prefix model-call path and schema-value validation that `features/extensions/` may call downward. The later Writer-only plan composes snapshot-derived schemas into a deterministic per-lane blob without registering them in `TOOLS`. |
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

### Phase 3 — Host UI and Conversation Map vertical slice (implemented)

1. Build the safe component renderer, form drafts, commands, workspace, and
   generation-keyed disposal.
2. Convert desktop/mobile burger menus to the host command model, then add
   tools, inspector, message, and artifact slots incrementally.
3. Add fixed effect handling and generalized cross-tab invalidation.
4. Add the full-tree database projection/resource adapter, atomic shared branch
   action, and `conversation-tree`.
5. Ship Conversation Map as the first complete reference package.
6. Add shared character-tag normalization and route the existing character API
   through it. This lands first: it is a first-party behavior change, and every
   later step in this phase depends on it existing.
7. Add `list.intersect`, `list.join` with its closed separator set (section
   20 — this replaces the withdrawn scalar-array template rendering rule), the
   cursor-paginated `library.cards` resource behind `library.cards.read`,
   `card.tags.set` writing `ctx.character` with no card argument, the
   `character.card` effect with frontend debouncing, and `ctx.character`
   resolution from validated action input gated on both
   `context.read` for `character` and `library.cards.read`.
8. Ship Tag Librarian as the second reference package, with the sweep loop and
   the cursor walk in the renderer rather than the flow.

Ordering note: steps 1–3 are not independently shippable. A view cannot render
before fixed effect handling and generation-keyed disposal exist, and both the
branch action (step 4) and the sweep (step 8) consume the effect path — so
renderer, effect handling, and generation plumbing land as one slice, and the
two reference packages are the parallelizable tail. Step 6 still lands first,
as stated. The v1.x expansions in section 20 interleave with this phase; the
order list there is normative for them.

Exit gate: renderer XSS/property fuzz tests pass and selecting a tree node has
the same lock, state refresh, and cross-tab behavior as `switchBranch()`. A
sweep cancelled midway leaves every completed card written, no card partially
written, and the next run resuming from the remainder. A tag list the character
API accepts and one `card.tags.set` accepts normalize identically.

### Phase 4 — Network, secrets, Git, and artifacts (implemented)

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

Two decisions this phase made that the earlier text did not anticipate:

- **A recovery flow is its own context.** `OpContext.RECOVERY` was added rather
  than reusing `ACTION`, because the two differ in what they may do and in what
  they must say. A recovery flow's target is the attachment the framework is
  rebuilding, so it names no message; an action's is not implied by anything, so
  it must name one. Reusing `ACTION` would have made that a runtime check, and
  it would have left branch activation and `card.tags.set` reachable from a
  regenerate button.
- **`artifact.emit`'s byte source is a step field, not a value form.** Section 10
  lists a package asset among the three sources. An asset has to be known at
  compile time — it is read, type-checked, hashed into the digest, and served —
  so it is a declared `asset:` path beside `data:`, not a new `$asset` value
  form. Adding a reserved `$` key to the value grammar would have been a change
  to every position a value can appear in, to solve a problem in one.

`OpContext.WRITER_TOOL` (API 2) follows the same rule for the same reason. It is
not `ACTION`: an action runs against a finished conversation with a user
watching it, while a Writer tool runs *inside* an unfinished model turn, where
there is no assistant row to attach to, no draft to replace (the Writer owns the
prose), no UI surface listening for a toast, and no user click to justify a
first-party mutation. Reusing `ACTION` would have admitted every one of those by
accident. Its allowlist is the pure operations plus namespaced state, `model.*`,
and `http.request`; `context.append`, `draft.replace`, `artifact.emit`,
`card.tags.set`, branch activation, all three `ui.*` operations, and
message-scoped state are refused at compile time.

### Phase 5 — Fragment-type contributions (implemented)

This phase completed ahead of the Writer-tool plan's WT2-WT4; see "Sequencing
against Community Writer Tools" at the top of this document.

The package-facing half shipped earlier — descriptor parsing, consent,
`OpContext.REDUCER` compilation, and `type_config` storage. Phase 5 completed
the host-facing work:

1. Added the lower fragment-type catalog and normalized built-in
   string/array/progressive behavior behind descriptors without changing
   feedback/direction-note behavior. Compiled descriptors are carried on the
   workflow record and immutable `RegistrySnapshot`.
2. Finished `type_config` editor support and preserved unavailable namespaced
   types across the fragment editor, API, database rows, and card transport.
3. Integrated validate/reduce/carry-forward/render/persist against one captured
   snapshot. This is the `TurnState.extra_fields` split — collect each
   fragment's raw Director output, then reduce — plus replacing the literal
   `field_type` branches in `tool_registry.py:build_direct_scene_tool` and the
   four in `prompt_builder.py` with pre-resolved schema/string lookups.
4. Shipped Meter as the reference contributed type and tested regeneration,
   branching, configuration edits, provider loss, and card import.

Exit gate: dynamic schemas are built once and byte-identical across all passes;
missing/invalid providers never coerce or erase stored data; and no granted
`fragment_type.contribute` package can install into a state where its declared
types are neither usable nor diagnosed.

### Phase 6 — Hardening and developer experience (implemented, less the CLI)

The `orb-ext` CLI (`init` / `validate` / `test --fixture` / `pack`) is
**deferred**, by decision rather than by omission. Everything else in this phase
landed:

1. **Parser/compiler fuzzing** (`tests/unit/extensions/test_hardening.py`).
   Seeded structural mutation of every reference package's JSON, byte-level
   corruption of the archives, and prefix truncation. The asserted property is
   that only a `PackageError` escapes `compile_package` — that vocabulary is
   what the routes map to status codes, so anything else is a 500 on an install
   request. It found one: a damaged deflate stream raised `BadZipFile` out of
   `ArchiveSource.read`, because the constructor validates the central
   directory while corruption surfaces during decompression. Both are now one
   `PackageParseError`.
2. **Golden fixtures.** The digest, canonical manifest encoding, contract
   fingerprint, and *derived* requirement set of a frozen package, pinned. The
   golden package is written out literally rather than built from
   `tests/extension_packages.py`, so editing a shared fixture cannot silently
   rewrite the expectation. Moving a digest or a fingerprint means every
   installed package asks for fresh consent over bytes that did not change,
   which is a decision, not a diff.
3. **Performance budgets.** A package at its declaration limits (`MAX_ACTIONS`
   flows × `MAX_FLOW_STEPS_DECLARED` steps) compiles inside a wall-clock bound,
   and the depth/breadth bombs are rejected in linear time. The numbers are
   loose on purpose: they catch an accidentally quadratic walk, not a five
   percent regression, because a tight timing assertion on shared CI fails for
   reasons unrelated to the code.
4. **Startup/load diagnostics.** `reconcile()` logs one summary line — counts by
   load status, how many packages are partially granted, and elapsed
   milliseconds — beside the per-package warnings it already emitted. A blocked
   entry point logs at INFO, not WARNING: an under-granted package is a state
   the user chose.
5. **Storage cleanup observability.** `content_store.usage()` reports stored
   revisions and bytes, and `GET /api/storage` carries them. Reported and
   deliberately *not* offered as a cleanup checkbox: extension content's
   lifetime is the install, not an age, so a checkbox would promise a cleanup
   that cannot happen. Staging leftovers count toward the bytes but not the
   revisions — they occupy the disk being shown, under no name anything can
   reference.
6. **Author documentation** — `docs/features/community-extensions.md`, written
   for a package author rather than for the engineer implementing the host.

Reference extensions, all four shipped in earlier phases and exercised by the
fuzz corpus here:

1. Conversation Map — command placement, full-tree resource, workspace, branch
   action.
2. Scene Meter — hook/action coverage plus a bounded contributed Meter fragment
   with config and inspector value views.
3. Tag Librarian — library resource, user-managed vocabulary, renderer-driven
   sweep, first-party card write.
4. API Artifact — origin consent, secret header, byte response, regeneration
   and reroll.

`scripts/build_example_extensions.py` writes all four to `dist/extensions/` from
the same builders the suite asserts against, so a package installed by hand is
the package the tests cover.

#### Why the CLI is deferred

`orb-ext validate` and `orb-ext pack` are `zip -r` plus a compile the install
route already performs and reports in full — the two-phase inspection shows the
derived requirements, the permission diff, and the exact validation error before
anything is installed, which is the feedback the CLI would duplicate over a
worse channel. `orb-ext test --fixture` is the one subcommand with no equivalent,
and it is also the one that would need a supported, versioned fixture format for
`ctx`, action input, state, and stubbed model/HTTP responses — a second contract
to freeze, for a tier whose whole premise is that the host owns the contracts.

The honest sequence is to ship it when there are enough third-party packages to
say what authors actually get wrong. Until then it is tooling maintained against
guesses.

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

### Library tag standardization

- Walking the library resource to a `null` cursor returns every surviving card
  below the first page's high-water mark exactly once. Cards added mid-walk
  wait for the next run; deleting an unvisited card removes only that card and
  never shifts or duplicates a survivor.
- A cursor is opaque: a package-supplied or mutated cursor is rejected rather
  than resolving to an offset, and no page exceeds either its card-count or
  encoded-byte budget.
- It projects only the requesting extension's namespaced slot, never another
  extension's, and no card field outside the declared shape.
- Enumeration requires `library.cards.read`; `context.read` for `character` alone
  returns 403 from the resource route **and** fails an action whose input
  declares a card identifier.
- Resolving `ctx.character` from action input also scopes `character` state
  writes to that card, and a card id naming a nonexistent card fails the
  invocation rather than falling back to the open conversation's card.
- `card.tags.set` accepts no card argument: a flow declaring one fails
  compilation, and the operation writes `ctx.character` and no other row.
- The character API and `card.tags.set` produce byte-identical stored tags for
  the same input list — untrimmed, empty, duplicate-by-case, over-long, and
  over-count cases included.
- `list.intersect` drops model output absent from the current vocabulary, and
  changing the vocabulary between runs changes what a later run writes.
- A sweep cancelled at card N leaves cards 1..N-1 written, card N untouched,
  and the remaining cards listed as unclassified on the next run.
- Revoking `card.write` for `tags` fails the next `card.tags.set` mid-sweep without
  reverting earlier cards.
- Revoking the sweep's character-state read removes the view/placement instead
  of treating every absent bookkeeping value as "unclassified"; revoking a
  `library.card_actions` slot removes it from the catalog and the action route
  refuses the host-card shortcut.
- A `runtime_generation` bump mid-sweep stops the loop; no action is dispatched
  after it, and the workspace reports the completed count rather than silently
  discarding envelopes for writes that committed.
- A sweep emits one `character.card` effect per card and the frontend performs
  a bounded number of library refetches, not one per card.
- A library-scoped action acquires no conversation stream lock: a sweep and a
  live turn in an open conversation make progress concurrently.
- Uninstall leaves written tags in place; purge does not remove them; the
  manager does not claim either would.

### Compatibility

- Existing first-party workflow, artifact, frontend-layer, SSE, fresh-install,
  preset, card-fragment, and pyright tests stay green.
- Enabling an ordinary extension with no dedicated Writer-tool contribution
  does not change the main model tool blob. A future eligible API 2 Writer
  contribution intentionally changes the deterministic Writer/single-model
  schema set described in its separate plan.
- Trusted workflow entries are the only manifest records that reach dynamic
  `import()`.
- Applying old presets and loading old cards with only core fragment types is
  unchanged.

---

## 19. Deferred beyond v1

- WASM or any general-purpose package code.
- Extension-defined Director/Editor tools, arbitrary all-pass tools, or new
  model passes. The bounded Writer-only ABI in
  [Community Writer Tools](community-writer-tools.md) has landed and is the one
  exception; nothing else about this deferral moved. In particular: no
  community name enters `TOOLS`, a package still cannot choose a pass, more
  than one Writer tool is never active at once, and more than one successful
  Writer-tool call never happens in a turn.
- Native text-completion support for optional Writer-tool calls. A text-mode
  endpoint does not render optional tools into the prompt and only synthesizes a
  call when one schema is forced, so the turn takes the ordinary no-tools path
  with a diagnostic rather than advertising a tool it cannot bound.
- Writer tools on providers that encode calls in the content body (Hermes tags,
  Gemma tokens, bare JSON). Those fallbacks cannot be told apart from prose the
  Writer has already streamed to the user.
- Background services, timers, daemons, or startup jobs.
- Extension dependencies.
- Arbitrary DOM/CSS, custom canvas code, iframe applications, or browser
  storage access.
- SSH/private Git authentication, submodules, or LFS.
- Wildcard network permissions.
- General regular expressions.
- General list iteration (`map`, `filter`, `reduce`) or any operation that
  evaluates per element. `list.intersect` is a single bounded set operation and
  must not become the seed of a collection library.
- First-party writes that fail any part of the four-condition rule in section
  13 — in particular writes to card fields that reach the prompt, and any write
  whose target is named by package-supplied input that no grant separately
  covers.
- Any operation taking a card, conversation, or message identifier that the
  invocation was not already handed. `card.tags.set` deliberately takes no
  argument naming its target.
- Automatic or import-time triggers for library-wide work. A sweep is something
  the user starts.
- Arbitrary database tables, migrations, SQL, filesystem paths, or raw HTTP
  routes.
- Automatic update installation.

These may be added only as new, separately threat-modeled capabilities. They
must not be smuggled through permissive v1 fields.

---

## 20. v1.x additive expansions

Everything here is approved surface growth that rides the existing
vocabularies: new entries in `OPERATION_SPECS` and `CAPABILITY_SPECS` — whether
as a new capability or as a new admissible value of an existing one, which is
also how `UI_SLOTS` and the host-resource catalog grow, since both are derived
from that table — feature-detected through
`requires.operations` / `requires.components` so an older Orb rejects a
package that needs them with a diagnostic rather than misbehaving. Nothing in
this section changes the trust model, the quota model, the flow language, or
the hook stages, and nothing requires an `extension_api` bump. Each item
states its security posture and whether it needs first-party refactoring. The
order list at the end is normative.

Writing-mode documents are deliberately not part of v1.x; any document-mode
surface is a separate design with its own threat model.

### The resource admission rule

Section 13 has a four-condition rule for first-party writes. Reads get their
own, generalized from the two Phase 3 resources (conversation tree, library
cards), which already satisfy it. A new host resource is admissible when all
five hold:

1. It is an allowlisted field projection built for the extension surface,
   never a row contract or API response passed through.
2. It is bounded by both an item count and an encoded-byte budget, and
   paginated with an opaque host-owned cursor when the underlying set is
   unbounded.
3. Its scope is fixed by the invocation's own context unless a separate
   enumeration grant conspicuously covers wider reach.
4. It is served as a database projection plus a host-resource adapter, never
   a query primitive handed to a flow.
5. Its consent line names what it reads in user terms — and when the projected
   data can reach a model call or network origin, the manager surfaces that
   combination (see the consent combination banner below).

Condition 5 exists because the three resources below project data that is
prompt-adjacent but was never extension-readable before. Reading is not the
new risk; reading *combined with* `network.request` is, and consent must say
so rather than leaving the user to compose two innocuous-looking grants in
their head.

### `list.join`

A pure operation joining an array of at most 256 scalars into one string,
with `separator` drawn from a closed host-owned set: `", "`, `"; "`, `" "`,
`"\n"`, `"\n- "`. An unknown separator fails compilation. The result is
subject to the existing template/value caps, and the operation is available
in all contexts including reducers, so a fragment type can format a list for
its writer context.

This is the fix section 5 already prescribed, landed proactively: the
scalar-array template interpolation rule is withdrawn before any published
package can depend on its frozen rendering, returning templates to pure
scalar path substitution with no special cases. Like `list.intersect`, this
is a single bounded host-owned operation with no per-element package logic —
it does not open the door section 19 keeps closed on `map`/`filter`/`reduce`.

No capability required (pure). No core refactor.

### Config view convention

A package may declare `views.config`. The Orb-owned extension manager renders
it in the package's detail panel; no slot, placement, or `ui.contribute`
grant is involved, because the surface is the manager itself, which the user
opens deliberately. This closes a real gap: extensions have config state and
form components but nowhere host-standard to surface settings, and without
this every package would burn its one workspace command on a settings page.

Hardening: a config view may bind form controls only to the `config` state
scope — declaring conversation/message/character bindings in it fails
compilation. Rendering never writes state; submission runs the normal
host-generated state action under the usual permission, schema, size, lock,
and transaction checks (section 7 unchanged).

No core refactor. Requires the Phase 3 renderer.

### Consent combination banner

A manager behavior, not a package surface: whenever the approved grant set
contains `network.request` together with any grant that reads conversation,
character, lorebook, persona, or history data, the consent diff and the
package detail panel show one fixed host-authored banner stating that data
the package can read can be sent to its named origins. The banner is derived
from the normalized grant set server-side; package strings never influence
it. This makes the section 11 warning structural instead of something a
careful reader infers.

No core refactor. Frontend manager + one derived flag in inspection results.

### Read resources: lorebook, direction notes, persona

Three clones of the conversation-tree/library-cards pattern — database
projection plus host-resource adapter, each behind its own grant:

| Grant | Meaning |
|---|---|
| `lorebook.read` | Read the lorebook entries of the world bound to the invocation's conversation. |
| `direction_notes.read` | Read the active branch's direction notes. |
| `context.read` for `persona` | Read the active persona's name and description. |

Persona is a `context.read` field rather than a capability of its own because
it is the same decision as reading the character or the history — one field of
the invocation's context, differing in sensitivity rather than in kind. It is
the one field the host serves through a resource route instead of into `ctx`
(it is a bounded singleton a view reads, not a value a flow interpolates), and
`RESOURCE_CAPABILITIES` records that without a second grant.

Lorebook and direction notes stay their own capabilities: they are not fields
of the invocation's context, they are separately paginated projections whose
consent lines describe different data, and `lorebook.read` in particular is the
strongest read in v1.x. `library.cards.read` likewise stays its own capability
rather than becoming a `resource.read` value — it is the *reach* grant, and it
authorizes resolving a package-named card into `ctx.character`, which is more
than any other projection hands out.

**Lorebook.** Scope is the world bound to the invocation's conversation card;
there is no world enumeration grant in v1.x — that would be a
`library.cards.read`-shaped reach grant and waits for a package that needs
it. Projected fields: entry id, keys, `secondary_keys`, `selective`,
`use_regex`, enabled, insertion order, and content bounded per-entry and in
aggregate; paginated with the standard opaque cursor. Note the hazard the
banner exists for: untriggered entries are content the model may never have
seen, so this is stronger than `context.read` for `history` when combined with
network access. Writes are refused permanently: lorebook content reaches the
prompt and fails section 13 condition 3. A "lorebook health" package writes
its findings into its own state and views; the user applies fixes in Orb's
editor. Enables the most-requested community shape (coverage auditors,
keyword-conflict detectors) at read-only risk.

**Direction notes.** Active-branch notes only: id, content, author kind,
timestamps, bounded and cursor-paginated. Write refused for the same
condition-3 reason — notes are injected into prompts.

**Persona.** Name and description of the active persona only, byte-capped.
This is the user's self-description, so the grant is conspicuous on its own
line in consent, and it is the strongest trigger for the combination banner.
Never writable. Section 6's character projection continues to exclude persona
data; this separate grant is the only path to it.

No core refactor for any of the three: each is a new bounded query plus an
adapter registration.

Lorebook and direction-note walks use monotonically increasing database ids as
their mutation boundary: entries added after the current position may appear
later in the same walk, deleted entries disappear, and surviving entries never
shift or repeat. This is intentionally different from a library sweep, whose
potentially expensive per-card model work needs the first-page high-water
snapshot described in section 17. Every paginated adapter stops page assembly
before either the item-count or encoded-byte ceiling and resumes after the
last item actually emitted.

### `library.card_actions` slot

A new `UI_SLOTS` entry: command placements rendered per card in the library
browser. When the user invokes one, the host resolves the clicked card into
`ctx.character` and rebinds the `character` state scope to it — the same
resolution section 6 specifies for action input, with one deliberate
difference: `library.cards.read` is **not** required, because the card
identifier is host-supplied from a user click, never package input. The
package's reach is still exactly one user-chosen card per invocation, so
consent remains "the character a command is run against" and the section 6
reach rule is preserved, not excepted. `ui.contribute` for the slot and
`context.read` for `character` are still required; `card.tags.set` additionally
requires `card.write` for `tags` as always.

This gives single-card tools (re-tag this card, analyze this card) a home
where the cards are, without the library-wide enumeration grant.

**Core refactor required:** `library_browser.js` must adopt the host command
model that Phase 3 step 2 builds for the burger menus. Do not build a second
placement mechanism for the library; the slot waits until that model exists.

### Invocation telemetry

Host-owned observability: per invocation, record wall time, model-call and
HTTP counts, and outcome; Writer-tool invocations additionally record bounded
input/output encoded byte sizes. Aggregate per extension and surface in the
manager's diagnostics alongside load status. This answers "which extension
slows my turns" — the first scaling pressure point, since each pre-hook may
spend two serial model calls before the Writer starts — and produces the
observed data a future per-turn aggregate pre-hook budget would be set against.
That budget is deliberately not enforced now, matching section 8's rule that
limits are widened or added against measurements, not guesses.

Hardening: telemetry is never projected into `ExtensionCtx` or any package-
visible surface — flows get no timing channel. Sanitized like all
diagnostics.

No core refactor: minor plumbing in `adapters.py`/`execution.py` plus a
catalog field.

### Implementation order (normative)

All items below are implemented. The list is retained as the normative landed
order and dependency record.

1. **`list.join` + withdrawal of array interpolation** — landed first, with
   Phase 3 step 7 (which was amended to ship it). Nothing depended on it;
   everything was simpler after it.
2. **Invocation telemetry** — landed while renderer work was in progress so
   the reference packages were measured from day one.
3. **Consent combination banner** — landed with the Phase 3 manager work,
   before any read resource beyond the tree.
4. **Config view convention** — landed immediately after the renderer core as
   the cheapest complete use of the renderer and the first integration test
   for form scope restrictions.
5. **Read resources (lorebook, direction notes, persona)** — landed after the
   tree established the resource-adapter pattern. Persona followed the other
   two after the banner was verified.
6. **`library.card_actions` slot** — landed after the command model, paired
   with Tag Librarian because both touch the library browser.

### Acceptance tests

- `list.join` rejects a separator outside the closed set at compilation, an
  array over 256 members or containing non-scalars at runtime, and a template
  interpolating an array fails as a plain scalar violation — no join
  rendering exists in `values.py`.
- A config view declaring a non-config binding fails compilation; rendering a
  config view performs no state write; submission enforces the 256 KiB slot
  cap.
- The combination banner appears for every grant set containing
  `network.request` plus any data-reading grant, in both the install consent
  diff and the detail panel, and never renders package-provided text.
- Each read resource: 403 without its grant; projects only allowlisted fields;
  respects count and byte budgets; a cursor walk has a documented mutation
  boundary and never duplicates or shifts surviving items; a package-supplied
  or mutated cursor is rejected.
- `lorebook.read` resolves only the world bound to the invocation's
  conversation; an action with no conversation binding fails rather than
  falling back to any world.
- A `library.card_actions` invocation resolves `ctx.character` to the clicked
  card without `library.cards.read`, scopes `character` state writes to that
  card, and a package cannot dispatch the same action with its own card
  identifier absent the section 6 dual grants.
- Telemetry values appear in the catalog diagnostics and never in
  `ExtensionCtx`, flow-visible errors, or any package-readable surface.
