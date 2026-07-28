# Community Writer Tools — Implementation Plan

Status: **planned; not implemented. Community-extension Phase 5's exit gate is
met, so implementation may proceed in WT0→WT4 order.**

Baseline: community-extension Phases 0–5 are implemented, including the network
client, write-only secrets, `artifact.emit`, and bounded Git installation. This
plan is an independent follow-on slice and does not reopen those phases.

### Sequencing against Phase 5

Phase 5 (fragment-type contributions) is complete. The ordering argument lives in
[Community Extensions](community-extensions.md) under "Sequencing against
Community Writer Tools"; its resulting seams are now the baseline for this plan:

- **WT0 should land first.** Versioned
  manifest dispatch, the core ABI values, `writer.tool.contribute`, and
  `OpContext.WRITER_TOOL` touch no pipeline code. WT0 is also the piece that
  degrades with delay: it exists because v1 models use `extra="forbid"`, so a
  host without it misreports a v2 package as malformed rather than as a package
  from a future API.
- **WT1 is likewise pipeline-free** — registry binding, activation persistence,
  the activation route, catalog projection, and the manager control — and may
  land in the same window.
- **WT2's Phase 5 dependency is satisfied.** Fragment schemas and prompt text
  now arrive through pre-resolved snapshot bindings, and `TurnState` separates
  raw Director fields from normalized/persisted fragment state. The per-lane
  split in section 7 can build on that path.

The `extension_api: 2` bump was a second reason for this order. Its
compatibility story depends on API 1 naming one complete frozen contract, and
API 1 is now complete because `fragment_type.contribute` has a runtime consumer.

Originating use case: [Orb issue #121](https://github.com/OrbFrontend/Orb/issues/121).

This document defines the implementation plan for a bounded Writer-only tool
ABI. It amends the direction in
[Community Extensions](community-extensions.md), whose frozen v1 contract does
not allow community packages to add tools to any main pipeline pass.

The intended interaction is:

```text
Writer streams a draft prefix
  -> optionally calls the one active Writer tool
  -> Orb validates and executes a declarative extension flow
  -> Orb returns a bounded structured result
  -> Writer resumes from the same transcript
  -> Orb persists only the final prose reply
```

The extension never runs package code, registers an arbitrary inference
callback, chooses a pipeline pass, or writes chat protocol messages. Orb owns
the ABI, tool schema compilation, prompt policy, invocation limits, transcript,
and continuation.

---

## 1. Goals and non-goals

### Goals

- Give the Writer one optional, host-mediated extension tool per turn.
- Let a tool resolve an uncertain action or similar narrative event and return
  structured data that the Writer can incorporate before finishing.
- Preserve the single-model shared-prefix strategy: all passes on that model
  receive one byte-identical tool blob, while the trailing OOC request narrows
  what the Writer may call.
- Preserve the dual-model strategy: the Writer lane receives Writer tools, and
  the agent lane receives Director/Editor tools.
- Execute the tool through the existing declarative compiler, interpreter,
  permission model, quotas, cancellation, lifecycle coordination, and
  immutable registry snapshot.
- Keep the accumulated draft under host control. The model describes the
  situation in tool arguments; Orb supplies the exact prose already emitted.
- Fail closed on an unexpected tool name, invalid arguments, stale grants,
  provider incompatibility, or extension failure.

### Non-goals for the first implementation

- Director or Editor tools contributed by community packages.
- More than one active Writer tool in a turn.
- More than one successful Writer-tool call in a turn.
- Package-authored pipeline passes, chat messages, SSE event names, or model
  transport logic.
- Native text-completion support for optional tool calls.
- Persisting the hidden tool transcript as canonical conversation history.
- Letting a Writer tool replace the draft directly. The Writer owns the prose
  continuation.
- General extension-to-extension tool discovery or invocation.

---

## 2. Fixed design decisions

| Question | Decision |
|---|---|
| API boundary | Introduce `extension_api: 2` while continuing to accept v1 packages. Do not change v1's meaning or merely replace `Literal[1]` with `Literal[2]`. |
| Core surface | Add generic immutable Writer-tool ABI values to `backend/core/`. The built-in Writer-tool set is initially empty. Package compilation, grants, flows, and dispatch remain in their owning higher layers. |
| Registry | Add a dedicated Writer-tool binding collection to workflow records and immutable registry snapshots. Do not route community tools through the mutable inference `TOOLS` registry. |
| Package contribution | A v2 package may declare at most one Writer tool. Multiple installed packages may contribute tools, but the user selects at most one active resolver. |
| Activation | Availability and activation are separate. All eligible contributions exist in the immutable snapshot, but only the one locally selected contribution enters the Writer tool blob and is named in the turn tail. |
| Writer choice | No active tool uses `tool_choice="none"`. One active tool uses `tool_choice="auto"` plus a host-authored OOC allowlist. |
| Enforcement | The model instruction is guidance; the captured host allowlist is authority. Orb never invokes a returned Director, Editor, disabled, stale, or non-selected tool. |
| Call budget | At most one successful Writer-tool call per turn. Continuation after that call uses `tool_choice="none"`. |
| Draft | Orb supplies `ctx.draft` from accumulated Writer content. The package cannot require the model to echo the draft in arguments. |
| Tool result | The flow returns a value validated against its compiled output schema. Orb sends canonical bounded JSON in the tool-role result. |
| Transport | First implementation supports chat transports that return standard structured `tool_calls`. Native text completion and content-encoded fallback calls are incompatible until separately designed. |
| Persistence | Persist only the concatenated final prose. Keep a sanitized Writer transcript ephemerally for same-turn continuation and downstream pass replay. |
| Extension state | A successful Writer-tool flow remains its own invocation transaction. Namespaced state commits may survive a later Writer abort; message-scoped state and first-party mutations are unavailable in this context. |

The extension API bump is for compatibility behavior, not because the runtime
mechanism intrinsically requires a major version. Current v1 models use
`extra="forbid"`, so an old host would otherwise report a new contribution
field as a malformed v1 package instead of a package from a future API.

---

## 3. Current seams that must change

The current pipeline deliberately prevents Writer calls in two places:

1. `build_writer_content()` adds “Do not use tool or function calls this
   turn” when the Writer shares an agent tool blob.
2. `writer_pass()` sends `tool_choice="none"` whenever its `CachedBase`
   contains tools.

The current `writer_enabled_tools` value also conflates two different facts:

- which agent tools belong in the shared single-model blob; and
- which tools the Writer is allowed to invoke.

In dual-model mode it becomes an empty mapping because every existing tool is
an agent tool. That is an optimization for the current registry, not a
permanent “Writer has no schemas” invariant.

The Writer currently streams `content` and `reasoning` events but discards the
terminal `done.message`. The client already assembles standard tool calls in
that message, so the transport contract is sufficient for a chat-mode Writer
loop; the pass and state model are not.

The Editor, feedback step, and post-turn direction-note step reconstruct the
Writer response as one user message followed by one assistant draft. That
reconstruction is no longer exact after the Writer has emitted an assistant
tool call, received a tool result, and continued in a second assistant
message.

Native text completion is a separate constraint from single-model mode. It does
not render optional tools into the prompt and only synthesizes a tool call when
the caller forces one schema. `tool_choice="auto"` plus an OOC nudge is
therefore not a structured optional-call protocol in text mode.

---

## 4. Core Writer-tool ABI

Add a small downward-safe module such as `backend/core/writer_tools.py`. It
owns only canonical host values and validation that must have one identity
across `workflows/`, `pipeline/`, and `features/extensions/`.

Conceptually:

```python
@dataclass(frozen=True, slots=True)
class WriterToolKey:
    owner_id: str
    local_id: str


@dataclass(frozen=True, slots=True)
class WriterToolSpec:
    key: WriterToolKey
    wire_name: str
    schema: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class WriterToolInvocation:
    call_id: str
    arguments: Mapping[str, JsonValue]
    draft: str


@dataclass(frozen=True, slots=True)
class WriterToolResult:
    value: JsonValue
```

The exact names may change during implementation, but the ownership boundary
must not:

- Core does not parse manifests.
- Core does not read grants, registries, databases, settings, files, or
  extension state.
- Core does not know flow paths or interpreter operations.
- Core does not hold a mutable global registry.
- The empty initial state is an empty Writer-tool collection in the built-in
  registry/snapshot, not a module-global list that extensions mutate.

`workflows/` owns a binding of `WriterToolSpec` to an async callable, analogous
to a subscription binding. `features/extensions/` creates that callable from a
compiled flow. `pipeline/` invokes the callable obtained from the captured
snapshot and therefore never imports the extension feature sideways.

---

## 5. Extension API 2 contract

Orb must continue to parse and run existing `extension_api: 1` packages. Add a
versioned manifest parser rather than changing the single supported literal in
place:

```text
raw version check
  -> parse ExtensionManifestV1 or ExtensionManifestV2
  -> normalize common runtime fields
  -> compile version-specific contributions
```

A v2 contribution has one optional Writer tool:

```json
{
  "extension_api": 2,
  "id": "outcome-resolver",
  "name": "Outcome Resolver",
  "version": "1.0.0",
  "requires": {
    "operations": ["return"]
  },
  "permissions": [
    {
      "capability": "writer.tool.contribute"
    },
    {
      "capability": "context.read",
      "field": "draft"
    }
  ],
  "contributions": {
    "writer_tool": {
      "id": "resolve_outcome",
      "label": "Resolve outcome",
      "description": "Resolve an uncertain action when success or failure should not be chosen by the Writer alone.",
      "flow": "flows/resolve-outcome.json",
      "input_schema": {
        "type": "object",
        "properties": {
          "action": {
            "type": "string",
            "maxLength": 1000
          },
          "stakes": {
            "type": "string",
            "maxLength": 1000
          }
        },
        "required": ["action"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "outcome": {
            "type": "string",
            "maxLength": 4000
          }
        },
        "required": ["outcome"],
        "additionalProperties": false
      }
    }
  }
}
```

Contract rules:

- The package declares no provider-facing function name. Orb derives a stable,
  collision-free name from extension ID and local tool ID and validates it
  against the strictest supported provider grammar and length.
- The function description and property descriptions are bounded
  package-authored model input. They require explicit consent because they can
  influence generation even when no call occurs.
- Input and output use the existing closed local JSON Schema subset, with
  dedicated aggregate byte/property/depth limits for Writer use.
- The tool input schema contains semantic request fields only. `draft`,
  conversation ID, character ID, grants, and host metadata are not
  model-supplied arguments.
- The compiler walks the referenced flow under `OpContext.WRITER_TOOL`,
  derives its transitive requirements, and includes the contribution in the
  consent-contract fingerprint.
- A v1 package cannot declare this field. A v2 package without it behaves like
  v1 with respect to the main model tool blob.

### Capability

Add an unparameterized `writer.tool.contribute` entry to `CAPABILITY_SPECS`.
Suggested consent copy:

> Add a callable tool and its instructions to the Writer. Its result can
> directly influence the reply even though the extension cannot write the
> reply itself.

The contribution consumes that grant before its schema enters a runtime
snapshot and again immediately before invocation. Other data and effects keep
their existing grants:

- Reading the accumulated draft requires `context.read` for `draft`.
- Reading history, character, persona, or input projections requires the
  corresponding existing grant.
- Flow-owned model calls require `model.call` for the selected lane.
- Network calls require the existing exact-origin `network.request` grants,
  which Phase 4 implemented.
- Namespaced state follows the existing per-scope read/write grants.

Changing a tool descriptor or schema is an inspected package update and changes
the contract fingerprint even if it does not add a new capability name.

---

## 6. Snapshot publication and local activation

Keep `Workflow.tools` and the global inference registry closed to community
packages. Add a distinct optional `writer_tool` binding to a workflow record
and a deterministic mapping to `RegistrySnapshot`.

Publication validates:

- only available community records publish a binding;
- the contribution's grant set is complete;
- provider-facing names are globally unique;
- the callable, schema, package digest, and extension ID belong to one compiled
  revision;
- ordering is deterministic by extension ID and local tool ID;
- the aggregate schema count and encoded-byte budget fit the host cap.

Lifecycle replacement swaps Writer-tool bindings with the rest of the
community overlay. A turn captures one generation and cannot execute a tool
from a newer or older revision than the schema it sent.

Availability is not activation. Add a local-only “active Writer resolver”
selection to extension package runtime metadata:

- At most one installed package is selected.
- Selection is usable only while the package is enabled, available, and
  granted.
- Disabling or permission revocation makes it inactive immediately but may
  retain the local preference.
- Uninstall removes the selection with the package row, so another future
  package claiming the same ID is not silently activated.
- Selecting one package clears the prior selection transactionally and bumps
  `runtime_generation`.
- The selection does not travel in shareable presets.

Expose the selection through the existing extension catalog/detail model and a
host-owned manager control. The control is a radio-style “Use as Writer
resolver,” not a package-rendered action.

---

## 7. Tool blob and KV-cache rules

Split the current per-turn configuration into:

- `agent_tool_schemas`;
- `writer_tool_schemas`;
- `active_writer_tool`;
- `writer_tool_policy`.

Assemble bases as follows:

| Topology | Writer base | Agent base |
|---|---|---|
| Single model | deterministic union of agent and Writer schemas | the exact same `CachedBase` object |
| Dual model | Writer schemas only | agent schemas only |

Consequences:

- Agent enablement must not disable Writer tools. The present context logic
  that forces the whole `enabled_tools` mapping false when the agent is off
  cannot own Writer eligibility.
- Director and Editor continue to force their exact host tool choices.
- Selecting, deselecting, updating, or making a Writer contribution ineligible
  intentionally changes the Writer/single-model tool blob. The selected schema
  is byte-stable within the captured turn; unselected package-authored schema
  text does not influence the model merely because the package is installed.
- An ordinary extension with no Writer contribution still cannot change the
  main tool blob.
- Tool order, canonical schema encoding, and union collision checks must be
  deterministic and covered by KV parity tests.

Within an interactive Writer turn, every Writer continuation extends the same
base and message transcript. In single-model mode, downstream passes can reuse
that exact transcript because their shared base also contains the selected
Writer schema. In dual-model mode, the agent base intentionally does not
contain Writer schemas and has no Writer-lane cache to reuse; downstream agent
passes receive a normalized user message plus the canonical final draft
instead of historical Writer tool-call messages.

The next conversation turn normally loads only persisted user/assistant prose,
not the hidden tool transcript. It therefore cannot byte-extend the complete
interactive transcript from the prior request. Accept the cache fork after the
shared prefix for the first implementation; do not persist hidden tool
messages merely to chase a cache hit.

---

## 8. Tail OOC policy

Replace the current `enabled_tools`-truthiness check in
`build_writer_content()` with an explicit Writer policy.

When no tool is active, retain a host no-tools instruction as defense in depth
and use `tool_choice="none"`.

When one tool is active, append a closed host-authored OOC block after the
effective user request:

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

The host may include the bounded package description and a schema-derived
parameter summary, but the surrounding authority, exclusivity, call budget,
and continuation wording are fixed Orb text.

The block must be the semantic tail:

- Without attachments, it is the final text in the Writer user message.
- With attachments, construct content parts so the OOC policy is a final text
  part after the image parts. The current helper places all images after one
  combined text part and is insufficient for this invariant.
- Build the complete Writer input once and retain it verbatim for trace replay.

The prompt is not the security boundary. It is expected to improve tool choice,
especially in single-model mode where the shared base also contains agent
schemas. The host validates every returned call against the captured
`active_writer_tool`.

---

## 9. Writer ReAct loop

Replace the one-shot `writer_pass()` with a bounded chat-mode loop.

### Initial call

1. Build one Writer user message, including the final OOC policy.
2. Call the shared Writer base with `auto` when a tool is active and `none`
   otherwise.
3. Stream ordinary reasoning and prose deltas as today.
4. Retain the terminal assembled assistant message.

### No call

If the terminal message has no standard structured `tool_calls`, finish. The
accumulated prose is the Writer draft.

Do not reinterpret arbitrary narrative JSON as a Writer tool call. The generic
`parse_tool_calls()` content-body fallbacks are useful for forced, non-streamed
agent calls but unsafe after Writer content has already been emitted to the
frontend.

### Valid selected call

For exactly one standard call to the captured active wire name:

1. Validate the raw call ID, function name, argument JSON, schema, and aggregate
   byte limits.
2. Compute `ctx.draft` from all Writer prose emitted before the call, including
   prose in the same assistant message.
3. Invoke the captured binding with cancellation and live grant re-checks.
4. Validate the returned value against the compiled output schema.
5. Append a sanitized assistant message containing its content and structured
   tool call.
6. Append one tool-role message with canonical JSON:

   ```json
   {
     "status": "ok",
     "result": {}
   }
   ```

7. Continue from the same base/transcript with `tool_choice="none"`.
8. Stream and concatenate the continuation. No second tool call is permitted.

If the continuation still contains a standard tool call despite
`tool_choice="none"`, execute nothing and stop after retaining any ordinary
continuation prose. Never issue a third model completion.

### Invalid or unexpected calls

- Never invoke an unselected, agent, disabled, stale, or unknown tool.
- If the assistant returns multiple calls, execute none.
- Append one host-owned error tool result for every standard call ID so the
  chat transcript remains protocol-valid.
- A call with a missing or unusable ID cannot be replayed as a valid tool
  exchange. Recover from a clean trailing branch containing the accumulated
  assistant prose plus a fixed host OOC “continue without tools” request;
  never fabricate a provider call ID.
- Continue once with `tool_choice="none"` using either the fixed error results
  or the clean recovery branch above; do not let the model retry another tool.
- Do not expose raw arguments or extension errors in SSE, toasts, or user
  prose.

### Extension failure

Timeout, cancellation, permission revocation, invalid output, and sanitized
`FlowError` all produce a bounded result such as:

```json
{
  "status": "error",
  "code": "resolver_unavailable"
}
```

The Writer receives no internal exception text. Unless the user cancelled the
whole turn, Orb attempts the one no-tools continuation so the Writer can finish
without inventing a successful resolution. A separate transport failure during
that continuation keeps the pipeline's existing turn-failure semantics.

---

## 10. Writer-tool flow context

Add `OpContext.WRITER_TOOL` as a separate interpreter profile. Reusing
`ACTION` would accidentally admit UI and first-party mutation operations whose
semantics do not make sense inside an unfinished model turn.

Initial allowlist:

- pure value, predicate, text, JSON, list, math, and deterministic random
  operations;
- `return`;
- namespaced `state.get`, `state.set`, and `state.delete` for config,
  conversation, and character scopes;
- `model.text` and `model.structured`;
- `http.request`, through the Phase 4 host client and its exact-origin grants.

Initial denylist:

- `context.append`;
- `draft.replace`;
- `conversation.branch.activate`;
- `card.tags.set`;
- `artifact.emit`;
- package-selected UI events, invalidations, or toasts;
- message-scoped state, because no assistant message row exists yet.

The successful flow invocation is the transaction boundary for namespaced
state. Its committed state is not rolled back if the subsequent Writer
continuation fails or the user aborts. This must be documented in consent/help
text and tested. External model/HTTP calls are already non-rollbackable.

The invocation seed must include the conversation, turn identity, extension
digest, tool key, and call ordinal so deterministic random operations neither
collide with hooks nor vary with process scheduling. Regenerate behavior must
follow the same macro/turn identity policy used by the rest of the pipeline.

---

## 11. Transcript, downstream passes, and persistence

Add a `writer_trace` value to `TurnState`. It contains only messages safe to
send back to a model:

- the initial Writer user message;
- sanitized assistant content/reasoning/tool-call messages;
- host-authored tool results;
- the final assistant continuation.

Do not carry provider-only `finish_reason`, usage, raw response chunks,
extension diagnostics, or unvalidated call data into replay.

`state.resp_text` remains the concatenation of Writer content across the
pre-call and continuation assistant messages. Tool arguments and results never
become prose.

Editor, feedback, and post-turn direction-note calls must accept both the trace
and canonical concatenated draft rather than reconstructing context
unconditionally.

- In single-model mode, replay the sanitized trace so the call extends the
  Writer cache. The final OOC request must identify the canonical concatenated
  draft explicitly, because the immediately preceding assistant message may
  contain only the post-tool continuation.
- In dual-model mode, do not send historical Writer tool-call messages to an
  agent base that does not declare that tool. Preserve the current normalized
  `writer_user_msg + canonical assistant draft` shape; there is no Writer-lane
  cache to reuse on the other model.

For the single-model canonical-draft instruction, two acceptable
implementations are:

1. include the canonical draft in the downstream OOC request; or
2. define and test wording that tells the model to treat all Writer assistant
   content around tool messages as one draft.

Prefer the first unless measurements show the duplicated draft cost is
material; it is less ambiguous for exact patch matching.

Persistence continues to store:

- final concatenated assistant prose;
- ordinary reasoning/audit fields already persisted by Orb;
- staged pipeline/workflow state already permitted by existing contracts.

It does not store the hidden Writer tool transcript or raw extension output.
Server debug logging must use sizes, names, status codes, and sanitized
summaries rather than full draft/argument/result payloads.

---

## 12. Transport and streaming admission

Writer tools require a transport capability check distinct from
single-model/dual-model selection.

Eligible in the first implementation:

- chat transport;
- standard structured `tool_calls` returned separately from content;
- valid call IDs usable in assistant/tool message replay;
- support for `tool_choice="auto"` and `tool_choice="none"`, or a verified
  endpoint profile with equivalent behavior.

Ineligible initially:

- native text-completion transport;
- providers that emit tool calls only as Hermes/Gemma/plain-JSON content;
- endpoint profiles that remove tool control without a verified safe
  equivalent;
- templates that cannot replay assistant tool calls plus tool-role results.

When the selected Writer resolver is incompatible with the active endpoint:

- omit Writer schemas from that request rather than advertise a tool that
  cannot execute safely;
- use the normal no-tools Writer path;
- expose a host diagnostic in the extension manager and turn inspector;
- do not fail the user’s whole turn.

This endpoint-dependent omission necessarily produces a different base tool
blob from a capable endpoint. The model/endpoint identity already separates
those cache lineages.

Optional native text-mode support is a separate design task. It must choose
between buffering/structured envelopes, a forced decision call, or a
transport-specific chat fallback and specify how control syntax is prevented
from leaking into streamed prose.

---

## 13. UI, API, SSE, and observability

### Manager and catalog

The extension catalog/detail response adds host-derived fields:

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

Add a route such as:

```text
PUT /api/extensions/{id}/writer-tool-active
```

with `{ "active": true|false }`. It uses the lifecycle lock, updates the
local-only selection transactionally, publishes one runtime generation, and
returns the ordinary fixed catalog/effect envelope.

The manager:

- shows the explicit Writer-tool consent line;
- identifies the selected resolver;
- explains endpoint incompatibility;
- prevents activating an unavailable or under-granted contribution;
- never renders package descriptions as markup.

### Turn status

Use one fixed host-owned status channel while the resolver runs. Packages do
not choose the event name or channel key. The frontend may display:

```text
Resolving outcome with Outcome Resolver…
```

The status is always cleared in `finally`. Do not send tool arguments, draft,
results, or internal errors over SSE. Existing token streaming continues before
and after the resolver pause.

### Telemetry

Count Writer-tool invocations in the existing extension telemetry:

- started/completed/failed/cancelled;
- wall time;
- nested model/HTTP counts;
- input/output encoded byte sizes;
- endpoint incompatibility skips;
- unexpected-call rejections.

Telemetry remains host-only and contains no prompt or result content.

---

## 14. Security and failure invariants

- A package cannot select its own activation or activate another package.
- A tool schema enters a snapshot only after compilation, consent coverage,
  grant approval, enablement, and availability checks.
- A turn invokes only the binding captured with the exact schema generation it
  sent.
- Live revocation prevents the next privileged operation and tool invocation.
- Provider-returned names resolve only through the captured active wire name,
  never a global dynamic lookup.
- Tool arguments validate before any flow step runs.
- Tool output validates and fits its byte budget before it reaches the model.
- The host supplies draft and entity identity; model arguments cannot redirect
  the invocation to another conversation, card, message, package, or flow.
- Tool descriptions, property descriptions, results, and errors have explicit
  character/byte/depth caps.
- An extension failure is converted to the fixed tool error and does not itself
  abort the pipeline. Independent model transport, cancellation, Editor, and
  persistence failures retain their existing semantics.
- Disable, update, uninstall, purge, and shutdown reuse the existing invocation
  lifecycle coordination. Purge drains a running Writer tool before deleting
  its state.
- No package string becomes a Python import, callback name, route, DOM
  selector, HTML, CSS class, SSE event name, or provider-selected function
  outside the compiled host namespace.

---

## 15. Concrete codebase change map

| Area | Planned change |
|---|---|
| `backend/core/writer_tools.py` | Add immutable generic ABI values and strict provider-name/value invariants. No registry, grants, or extension behavior. |
| `backend/features/extensions/contracts/` | Add versioned v1/v2 manifest parsing, the optional v2 Writer-tool descriptor, `OpContext.WRITER_TOOL`, operation allowlist, schema limits, and `writer.tool.contribute` in `CAPABILITY_SPECS`. |
| `backend/features/extensions/compiler.py` | Load the referenced Writer flow, validate schemas/context, derive grants, fingerprint the contribution, and produce the immutable spec/binding inputs. |
| `backend/features/extensions/adapters.py` | Add the bounded Writer-tool invocation adapter, context projection, lock plan, output validation, fixed error mapping, telemetry, and cancellation. |
| `backend/features/extensions/runtime.py` | Publish the available Writer binding into the community workflow record and expose catalog diagnostics. |
| `backend/workflows/contracts.py` / `registry.py` | Add a Writer-tool binding distinct from `ToolSpec`; validate names/count/size; include deterministic bindings in `RegistrySnapshot`; keep community `Workflow.tools` forbidden. |
| `backend/database/` | Store the local active-resolver selection with extension runtime metadata; enforce at most one selection transactionally; preserve migration/fresh-install/preset symmetry. |
| `backend/api/routes/extensions.py` / schemas | Add the activation route and catalog/detail projection. |
| `backend/pipeline/context.py` / `config.py` / `state.py` | Resolve eligible Writer schemas and one active binding from the captured snapshot; separate agent eligibility; add Writer policy and ephemeral trace. |
| `backend/pipeline/passes/writer.py` | Build the true tail OOC content, preserve multimodal order, consume terminal messages, validate calls, run the one-call ReAct loop, and concatenate prose safely. |
| `backend/pipeline/passes/editor/**` and direction notes | Replay the sanitized Writer trace in single-model mode; retain normalized user/final-draft context in dual-model mode; provide the canonical concatenated draft to downstream OOC prompts. |
| `backend/inference/` | Add deterministic schema-union helpers and explicit Writer-tool transport capability checks. Do not register community Writer tools in `TOOLS`. |
| `frontend/extension_manager.js` | Show contribution/consent/compatibility and the single active-resolver control. |
| `frontend/chat_stream.js` / inspector | Render and clear the fixed host Writer-tool status without exposing package event names. |
| Tests/docs | Add contract, lifecycle, prompt/KV, transport, ReAct, permission, concurrency, SSE, persistence, and compatibility tests; update the architecture docs listed in section 18 when implementation lands. |

---

## 16. Implementation sequence

Use `WT` phase names so this work does not renumber the existing
community-extension phases.

Interleaving with community-extension Phase 5, per "Sequencing against Phase 5"
at the top of this document:

| Phase | Touches pipeline? | May run with Phase 5 |
|---|---|---|
| WT0 | No | Yes — preferably before it |
| WT1 | No | Yes |
| WT2 | Yes | No — after Phase 5's exit gate |
| WT3 | Yes | No |
| WT4 | Yes | No |

### WT0 — Core ABI and versioned contracts

1. Add the empty core Writer-tool ABI and registry binding contract.
2. Generalize package version dispatch to support both API 1 and API 2.
3. Add the v2 descriptor, capability, operation context, limits, canonical
   provider name, and malicious contract fixtures.
4. Keep runtime publication empty: no Writer behavior changes in this phase.

Exit gate:

- Every current v1 fixture compiles byte-identically.
- A v1 manifest with `writer_tool` is invalid.
- A v2 package is recognized as compatible but publishes no executable binding
  until later phases.
- Core import-layer tests pass with no feature vocabulary or dependencies
  admitted upward.

### WT1 — Compilation, snapshot, and activation

1. Compile and fingerprint the Writer tool and its flow.
2. Add the dedicated binding collection to workflow records/snapshots.
3. Add active-resolver persistence, lifecycle mutation, catalog projection,
   manager consent, and radio selection.
4. Prove update/rollback/disable/revoke/uninstall atomically change new-turn
   visibility without changing an in-flight snapshot.

Exit gate:

- The selected eligible binding and its digest are immutable within a captured
  generation.
- Ordinary extensions leave the main tool schema set unchanged.
- Selecting a Writer resolver changes only host metadata/snapshot generation;
  it does not yet let the Writer call it.

### WT2 — Tool blob, tail policy, and chat Writer loop

1. Split agent schemas, Writer schemas, and active Writer policy in pipeline
   config.
2. Implement deterministic single/dual model schema assembly.
3. Add the literal tail OOC policy, including multimodal content ordering.
4. Consume terminal Writer messages and implement the bounded standard-call
   loop with a stub host binding.
5. Add `writer_trace` and update Editor/feedback/direction-note replay.

Exit gate:

- Single-model Director, Writer, Editor, and post-Writer calls share the exact
  intended schema blob and prefix.
- Dual-model Writer never receives agent-only schemas, and the agent never
  receives Writer-only schemas.
- Wrong/multiple calls execute nothing and recover with a no-tools
  continuation.
- Tool syntax never appears in persisted prose or SSE.
- Text-mode and incompatible endpoints take the unchanged no-tools path with a
  diagnostic.

### WT3 — Declarative flow execution

1. Implement the Writer-tool adapter and `ExtensionCtx` projection.
2. Enable the strict `WRITER_TOOL` operation profile, state transaction,
   cancellation, quotas, deterministic seed, output validation, and fixed error
   result.
3. Connect the captured binding to the Writer loop.
4. Add fixed status and invocation telemetry.
5. Ship an Outcome Resolver fixture exercising structured input, host-supplied
   draft, deterministic random or an isolated model call, and structured
   output.

Exit gate:

- The issue #121 path works end-to-end: the Writer emits prose, pauses for the
  resolver, receives the result, and continues without repeating the draft.
- Revocation or extension failure at every operation boundary executes no
  forbidden effect and still permits a safe Writer continuation.
- Disable/update/purge/shutdown concurrency tests pass while the resolver is
  running.

### WT4 — Hardening and documentation

1. Add provider compatibility probes/profiles and hostile streamed-response
   fixtures.
2. Fuzz schemas, call IDs, argument fragments, results, tool descriptions, and
   transcript replay.
3. Measure schema/token overhead and invocation latency; adjust hard caps only
   with recorded evidence.
4. Update public author docs and all affected architecture invariants.

Exit gate:

- Full backend, frontend, pyright, import-layer, KV, SSE, preset, migration,
  fresh-install, and extension suites pass.
- No v1 extension or no-tool turn changes behavior.
- Documentation describes Writer tools as implemented only after this gate.

---

## 17. Acceptance tests

### Contract and compatibility

- Existing API 1 manifests still compile and produce the same canonical
  manifest/digest behavior.
- API 2 requires the exact Writer capability and every transitive flow grant.
- Unknown Writer fields, excessive descriptions, unsupported schemas, duplicate
  names, and over-budget aggregate blobs fail compilation.
- An older API 1-only Orb reports an API 2 package as incompatible based on the
  raw version before strict parsing.

### Registry and lifecycle

- Enable/update/rollback/permission changes publish workflow metadata,
  Writer-tool bindings, digests, and generation as one transition.
- A turn captured before update invokes only the old binding; the next turn
  sees only the new binding.
- Selecting resolver B clears resolver A atomically.
- Disable/revoke makes a retained selection ineligible; uninstall removes it.
- Community `Workflow.tools` remains rejected and no Writer contribution calls
  `register_tool`.

### Prompt and cache

- Single-model bases contain one deterministic union in byte-identical order
  across all passes.
- Dual-model bases contain only their lane's schemas.
- Changing the active selection changes both the selected schema and OOC tail
  in the next captured generation; every pass in that turn still receives the
  topology's byte-identical schema blob.
- No contribution produces the current no-tools behavior and request shape.
- With image attachments, the host OOC policy is the final content part.
- Agent off does not disable an otherwise eligible Writer resolver.

### Writer loop

- Prose before a standard tool call streams once and becomes the exact
  `ctx.draft`.
- A valid call executes once; continuation uses `none`; final prose is the
  exact concatenation without tool bytes.
- A tool call plus content in one assistant message preserves the content
  prefix and the call ID.
- Unknown, agent, stale, duplicate, or multiple calls execute nothing.
- Invalid JSON/schema/size returns the fixed error result and one safe
  continuation.
- Aborting before, during, and after the flow releases all locks/tasks and does
  not start another completion.

### Interpreter and permissions

- Every allowed operation succeeds only with its current live grant.
- Every denied operation fails compilation in `WRITER_TOOL` context.
- The model cannot supply draft or entity IDs through arguments.
- Output schema/byte violations never reach the Writer.
- Namespaced state commits only after successful flow completion and may
  survive a later Writer abort as documented.
- Message state and first-party mutations are unreachable.

### Downstream and persistence

- Editor, feedback, and direction notes extend the sanitized Writer trace in
  single-model mode and use normalized final-draft context in dual-model mode.
- A dual-model agent request contains no historical call to a Writer tool absent
  from its agent schema blob.
- The Editor receives one unambiguous canonical full draft for patch matching.
- Database content contains final prose only, with no tool call, result,
  extension error, or hidden transcript.
- Regeneration and branching keep the ordinary conversation model unchanged.

### Transport, SSE, and privacy

- Native text mode and content-only tool-call providers skip the feature
  without leaking tagged/JSON control output.
- Tool arguments, draft, output, and raw errors never appear in SSE, telemetry,
  catalog diagnostics, or logs.
- The fixed status clears on success, error, abort, disconnect, and exception.
- A provider that ignores `tool_choice="none"` still cannot execute a second
  or non-selected call.

---

## 18. Documentation updates when implementation lands

Update these together with the implementation:

- [Community Extensions](community-extensions.md): status, decision table,
  package contract, capability vocabulary, snapshot contents, operation
  contexts, concrete change map, compatibility, and deferred list.
- [KV Cache Reuse](kv-cache.md): schema union, Writer transcript replay, and
  accepted cross-turn cache fork.
- [SSE Turn Stream](sse-stream.md): fixed Writer-tool status and the pause /
  continuation sequence.
- [Secondary Workflows](secondary-workflow.md): the dedicated Writer binding
  carried by registry snapshots, distinct from trusted `ToolSpec`.
- `AGENTS.md`: Writer-tool ABI ownership, lane assembly, activation, and
  permission source of truth.

Until WT4 is complete, those documents must describe this feature as planned,
not available.
