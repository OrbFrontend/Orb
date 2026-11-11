# Assistant chat implementation plan

Status: proposed. This reviews and revises the supplied right panel assistant plan; it does not implement the feature.

Add a small assistant in Orb's right panel that answers questions about the library and performs explicitly requested actions through the existing Agent lane. The overall design fits Orb: keep it outside the turn pipeline, compose capabilities in the API layer, and reuse existing streams, workflow jobs, and persistence rules. Ship a useful read-only assistant first, then add writes in small increments.

The main gaps are confirmation integrity, concurrent edits, cancellation during writes, and targets changing while a request runs. These need concrete contracts before write tools ship. A general agent framework, durable assistant sessions, and a large preparatory refactor are unnecessary.

## Review findings

| Gap in the supplied plan | Recommended change |
| --- | --- |
| A client supplies `tool`, `arguments`, and `base` to Apply. A hash of old fields does not bind the submitted edit to the preview, prevent replay, or close the gap between checking and updating. | Keep a small, bounded set of server-owned proposals. Apply names a proposal; the database compares and writes atomically. Details below. |
| Abort checks before a commit do not settle a commit already in progress. A disconnected stream can lose the result of a successful additive write. | Finish an admitted write, report committed resource IDs, and reconcile after settlement. Never automatically retry a write with an unknown outcome. |
| Handoff helpers currently use `S.activeConvId`; Magic Rewrite also reads direction from a DOM input. | Extract helpers accepting captured conversation ID, message ID, and direction. Validate again before launching. |
| The plan says chat Stop cancels renders. Current workflow jobs have their own Stop ownership. | Assistant Stop cancels only its active child job; deletion retains the existing job settlement behavior. Chat Stop keeps its existing behavior. |
| Argument coercion and dropped unknown keys can silently change a requested write. Provider schema strictness is not server validation. | Reject invalid types, unknown fields, unsupported targets, and overlong write text. Normalize only documented optional defaults. |
| Per-result bounds do not cap the accumulated model transcript. Replayed `tool` and `outcome` items are browser-controlled. | Bound the whole working context and reconstruct replay as ordinary history; never trust replayed outcomes as proof of a write or approval. |
| Calling the card creation path can fetch embedded expressions from URLs. | Use a narrow text-only create schema, server-generated IDs, and no extension, embedded book, image, or import inputs. |
| A global transcript can carry “this reply” into a different chat or document. | Capture view context at Send, show its target, and preserve context boundaries in history. Never retarget an in-flight request. |
| The advertised tool count does not match the inventory. | The table contains **26 named tools**, not 24. Track the available names per milestone. |
| Multi-tab handling already exists in `frontend/tabLock.js`. | Reuse its send-permission guard for the assistant rather than creating a second coordination scheme. |

## Product behavior

Keep the supplied decisions:

- One `#assistant-dock` at the bottom of the open `#tools-panel` or `#inspector`. Hide it when neither panel is open; skip Inspector in document mode. Remember expansion state.
- Reads, additive writes, explicit toggle changes, and explicit branch selection execute immediately. Existing-content edits require an inline Apply/Cancel preview.
- Store the transcript in browser localStorage with Clear. Add no transcript table, migration, or user setting.
- Use `assistant` in feature names, routes, actions, CSS, and operation kind. Keep the model on the Agent lane.
- Automatic pipeline toggles do not prohibit an explicitly requested assistant capability. Missing setup still returns a useful tool error.

The scope label identifies the captured chat, character, or document. Switching views affects the next user request, not a request already running or an existing proposal. Do not infer a character for a group from the currently inspected member: expose the cast and require an explicit character target when ambiguous. Chat tools are unavailable in document mode unless the user explicitly names a chat.

`read_document` reads saved content. It must not silently save or assume access to unsaved editor text. Mention this distinction when it affects an answer.

Keep one transcript in v1. Store context metadata with each user request, display a separator when its target changes, and include that boundary in model replay. Clear is disabled until active work settles; collapsing or moving the dock does not stop it. Historical confirmation cards cannot become actionable merely by loading them from localStorage.

## Tool scope

The full staged scope remains 26 tools. The first milestone contains only the ten read tools.

| Tools | Behavior and limits |
| --- | --- |
| `get_context` | Current target IDs, chat title, active leaf, cast, effective worlds, and bounded context-size summary. The API adapter may call `pipeline/context_size.py`; the feature slice may not import it. |
| `list_characters`, `get_character` | Bounded search and allowlisted authored text fields. No avatars, extensions, workflow state, or full database rows. |
| `query_library` | Reuse `run_library_query` with its read-only connection, view authorizer, deadline, and result caps. Return conversation/message IDs with findings. SQL searches can see inactive branches; distinguish them from active-path history. |
| `read_messages` | Last 20 messages at most from the named chat's active path. Project content and identity fields; exclude attachment bytes and internal state. |
| `list_worlds`, `list_lorebook_entries`, `list_personas` | Bounded listings with IDs. Allow filtering and an explicit truncation indicator; large libraries must be narrowable. |
| `read_document` | List bounded document summaries when no ID is supplied; read a bounded saved body when one is supplied. |
| `get_settings_summary` | Explicitly allowlisted pipeline switches and sampler values; report stored and effective enablement when master switches differ. |
| `create_character`, `create_world`, `create_lorebook_entry`, `create_persona` | Small authored-content schemas, server-generated IDs, existing validation and persistence behavior. Creating a world does not silently make it global or attach it to a chat. |
| `set_chat_world`, `set_pipeline_toggle` | Explicit desired state, never a relative “flip.” Validate the target and preserve unrelated settings keys. Return the actual effective state. |
| `switch_branch`, `checkpoint_chat` | Require a chat and validated message ownership where applicable. A checkpoint returns its new ID without navigating the UI. Branch selection changes subsequent context, so finish the assistant run after it succeeds. |
| `regenerate_reply`, `rewrite_reply` | Handoff to the existing chat stream; include exact chat/message IDs and rewrite direction. No custom generation or persistence path. |
| `edit_character`, `edit_lorebook_entry`, `edit_persona`, `edit_message` | Preview and Apply only. Initially allow character description/personality/scenario/first message/examples/creator notes, lore entry name/content, persona name/description, and message content. Defer other fields. |
| `generate_image`, `generate_speech` | On-demand workflow dispatch for an existing saved message, preserving configuration checks, ownership, locks, jobs, and artifact persistence. |

Text edits support whole-field replacement or literal find-and-replace matching exactly once. Reject empty find strings, no match, and multiple matches. Do not silently truncate inputs or proposed changes. A tool that creates a duplicate is still additive: do not turn it into an upsert or edit based on a matching name.

Give toggle names stable, explicit meanings. An “Editor” label may cover several settings; the adapter must define the mapping and report master gates rather than enabling unrelated switches to make the label appear on. Workflow toggles use the same host behavior as their route, including runtime callbacks such as Prose Rewriter unload/warmup. Nested toggle updates must preserve concurrent changes to other keys.

Keep the original exclusions: deletion; credentials, endpoints, models and lane routing; preset/reset/import/export operations; workflow configuration; caller-supplied URLs; bulk library operations; local model management; and GPU controls. Pipeline-internal tools remain private to turn orchestration. Document edits, compression, fragments, state operations, and Dynamic Worlds changesets remain later work.

## Backend design

Use a feature slice plus API composition:

- `backend/features/assistant/`: a small public facade, contracts, model loop, and its prompt. The loop receives tool callables and never imports the API, pipeline, workflows, or peer features.
- `backend/api/assistant_tools.py`: concrete adapters, target checks, field projections, and tool construction using typed `Settings`.
- `backend/api/routes/assistant.py`: run, Stop, and later Apply. Add request models to `api/schemas.py` and register the router normally.
- Add focused API helpers for shared write or workflow behavior only as those tools are implemented. Keep proposal state at this integration boundary.

Do not pre-create a generic schema interpreter, mutation bus, or separate module for every small helper. Use the existing schema validation stack for the small tool argument models, with flat provider schemas. A tool record needs its name, description, parameter schema, behavior class, and callable; add confirmation support with the edit milestone.

Expose `POST /api/assistant/run`, `POST /api/assistant/stop`, and later `POST /api/assistant/apply`. Run accepts bounded transcript items and `{mode, conversation_id?, character_id?, document_id?}` context; validate the IDs and their relationships server-side. Apply accepts `{proposal_id}`. Use existing application authentication and shared HTTP error handling.

Preserve the [backend dependency rules](../architecture/prompting.md), [stream ownership contract](../architecture/sse-stream.md), [workflow lifecycle](../architecture/secondary-workflow.md), and [cache boundaries](../architecture/kv-cache.md). `inference/drafting.py` already provides forced-call execution; extract only the shared tool-message constructors and quoting helper still owned by Card Generator.

### Model protocol

Reuse `forced_turn` and Deep Card's handling of forced-call replay and cut-off replies. The assistant adds a two-call selection and argument protocol:

1. Force `assistant_step` with `{tool, message}`, where `tool` is an enum of this milestone's available tools plus `reply`. `message` is a brief user-facing update or final answer. Omit the proposed `thought` field.
2. For a parameterized tool, force that tool's flat schema in a second call. Zero-argument tools skip it.
3. Validate, execute, and append the bounded result. Every replayed tool call has a unique ID and matching result, including the step call before requesting arguments.
4. `reply` finishes. A confirmation or handoff pauses execution and finishes the stream.

Use Agent lane model selection and max tokens, with reasoning off initially. Keep schemas and the short assistant system prefix stable and ordered. Do not add assistant tools to `prompting/tool_catalog.py` or the turn cache. Put captured view context after the latest user text; it is context, not a new instruction from the user.

Allow at most 12 tool executions and 30 model calls per run, including retries. Retry an unusable or cut-off model response once before any execution. Reject `finish_reason=length` even if salvaged JSON looks valid. Tool validation errors become corrective results; expected unavailable/busy conditions become tool errors; unexpected defects use Orb's shared failure handling. Never retry a side effect automatically. On a budget limit, finish with an explicit limit outcome and any confirmed work already done.

Treat card, lore, document, message, and SQL text as untrusted data. Require a current user request for writes; retrieved instructions and old outcomes cannot authorize them. Fencing and the restricted tool surface reduce prompt-injection risk but do not guarantee that an allowed immediate write will be appropriate. Keep progress messages as intentions and claim success only after execution results.

### Context and result bounds

Accept at most 100 replay items with 8,000 characters per text item. Keep the proposed 20,000-character budget for replay and accumulated tool data combined, excluding stable instructions; honor a smaller configured input budget when needed and reserve space for the next output. Replace old result bodies with summaries and resource IDs, preserving valid call/result pairs. Preserve the newest request and its context; if they cannot fit, return a clear error. Do not add a model-based history compactor.

Browser history is untrusted input. Reconstruct old tool/outcome entries as quoted history rather than native privileged roles or approval state. Only tool results produced inside the current run become native tool messages.

Project results to explicit allowlists before serialization. Apply list, cell, and total bounds and mark truncation. A recursive secret-key scrubber is a backstop, not the privacy boundary: SQL aliases and exception strings can evade key-name matching. Return expected errors through existing redaction seams; never send raw config objects or provider response bodies to the model. Authored prose remains user content, not a promise of general secret detection.

## Streams and operation ownership

Use `beginStream("assistant", capturedTarget, "/assistant/stop")`, `streamEvents`, and `settle`. Run the backend through `sse_stream` under a reserved assistant resource key that cannot collide with a chat ID. Both run and Stop carry `operation_id`; a stale Stop cannot cancel newer work. This follows the existing single-process lock contract.

The event set can stay small: `start`, `progress`, `assistant_message`, `tool_start`, `tool_result`, `mutation`, `confirm`, `handoff`, and terminal `done` or `error`. Use server-assigned call IDs to pair results with tool steps. A confirmation or handoff event is followed by `done` with a reason such as `awaiting_apply` or `handoff`. Stop likewise settles with an explicit stopped outcome. EOF alone never means success.

Chat mutations capture their chat ID, refuse deletion or busy generation, and take the existing idle stream lock. Re-read ownership and target state after admission. Do not hold that lock while waiting for a model, user confirmation, or media rendering. Library edits use their own target checks and preserve the normal database invariants.

Once a short write is admitted, let its full logical operation settle even if Stop arrives. Cancellation of an awaiting coroutine does not prove SQLite rolled back. Message edits must preserve macro handling, greeting-template invalidation, Writer draft clearing, and changeset invalidation. Card edits retain conversation synchronization; lore edits retain content and entry revision behavior. Emit mutation IDs only after the operation commits.

After an error, Stop, or lost connection, refresh affected data once the operation settles; if a write was in flight, reconcile its target even without a received mutation event. Mark an unacknowledged write as “outcome unknown; check the target,” and do not auto-resend the request. This intentionally avoids a durable run ledger or exactly-once claim.

### Confirmation and Apply

Replace the proposed client-supplied stateless Apply payload with a bounded in-memory proposal store. Orb already assumes one server process, so this adds no persistent session or schema migration.

1. The edit tool reads and validates the target without writing. Store its canonical target, field allowlist, expected before values, final after values, originating context, and a random proposal ID. Keep at most 32 proposals with a 15-minute expiry; clean them on access.
2. Send the exact before/after preview to the browser. Render all changed fields as escaped text diffs, outside model-controlled markup. Apply submits only the proposal ID; it cannot substitute arguments or after values.
3. Apply revalidates the target and acquires the usual admission locks. Compare expected values and perform the edit in one database transaction or conditional update. A Python re-read followed by an unconditional update is insufficient for cards, personas, and lore entries edited through other routes.
4. A missing target returns 404; changed fields return 409; expired or restarted-server proposals return an expired response. A stale card offers Ask again. Concurrent Apply requests admit one commit; retain the completed result until expiry so a repeated Apply can return it without writing again.
5. Cancel makes the card inert and performs no write. Keep at most one active proposal in this browser; starting a new user request invalidates its local Apply control. Reloaded cards are historical and require Ask again.

Resolve message macros before preview and commit those same displayed after values through the shared edit behavior. Recomputing random macros on Apply would make the saved content differ from the approved diff.

After Apply, append the confirmed outcome and allow one continuation run anchored to the proposal's original context. Recheck targets, never repeat the committed edit, and permit another preview only within normal limits. If Apply was not acknowledged, retry that same proposal ID or reconcile; do not start model continuation from an assumed success. Cancel ends the request.

### Handoffs and media

Handoffs name `{conversation_id, message_id, direction?}`. Settle the assistant stream before invoking extracted chat helpers. Those helpers accept these values directly, verify the target is still on the expected active path, and retain ordinary group/speaker and busy checks. If the target chat is no longer open, show a handoff note and require a fresh user action; do not redirect the current chat. The chat stream owns Stop and authoritative reply settlement after launch. Regeneration and Magic Rewrite retain their existing branch-producing behavior; direct `edit_message` still requires Apply.

Extract shared on-demand dispatch only when implementing media. Skip only workflow enablement for assistant-origin calls, keeping the existing public route gated. Preserve workflow-state and character-state locks, source ownership, jobs, configuration checks, and source-text metadata. Consume a lazy `WorkflowEventStream` inside a registered job through settlement, rather than returning it as an HTTP response from a tool.

Link the child job to the assistant operation and abort token. Assistant Stop cancels and waits for that job alone; message/chat deletion can find it by source. A committed artifact remains and is refetched after settlement. Do not wrap the whole assistant loop in `turn_hold()`; model calls already obtain LLM leases, and image rendering must be able to obtain its own lease between them.

Retain the proposed GPU exception: refuse assistant image generation when Image Gen is effectively disabled and its ComfyUI configuration requests GPU swap. Report the reason without changing toggles. Tests must cover both this refusal and a configured, safe disabled-workflow dispatch. A successful tool result returns saved artifact IDs; a started job alone is not success.

## Frontend design

Start with `assistant_view.js` at layer 1 for pure transcript parsing/rendering and `assistant_chat.js` at layer 5 for orchestration. Register both in the layer checker and initialize from `app.js`. Shared assistant state belongs in `state.js`; running ownership belongs in `S.operations`. DOM references and other private implementation details can remain local.

Keep one dock node outside panel content that existing renderers replace. A narrow class-attribute observer for the two panel roots and `#app` is sufficient; move the node only when its parent needs to change. No new panel event framework is needed. Use flex columns with `min-height: 0`, bounded log height, `.asst-` CSS, and one existing `ui_sizing.js` BOXES entry. Verify `frontend/mobile.css` and import the new stylesheet through the normal CSS entrypoint.

Render assistant text through `renderMessageHtml` within `.msg-body`; keep controls and confirmation cards outside it. Render user text and field diffs with escaping. Register actions through `actions.js`. An element-level keyboard handler is appropriate for preventing document/mobile shortcut propagation; ignore Enter while IME composition is active, use Shift+Enter for newline, and give Send/Stop and expansion controls accessible names and state.

Store at most 100 bounded items under `orb-assistant-transcript`, with expansion under `orb-assistant-open`. Validate loaded data, catch malformed JSON and quota/storage failures, and continue in memory when storage is unavailable. Persist summaries and committed IDs, not raw tool responses, live operation objects, or authoritative Apply state.

Refresh by affected resource IDs. Use the existing loaders and `settings_store.js` notifications; do not write `S.settings` directly or reload endpoint/model configuration for a toggle. Repaint conversation-scoped UI only when the operation still owns that view, following `ownsView`/view-token conventions. Settings and library lists can refresh without stealing focus or opening a different chat.

## Delivery sequence

| Milestone | Work | Completion condition |
| --- | --- | --- |
| 1 Read assistant | Loop, ten read adapters, run/Stop, dock, persistence, bounded history, ownership and error handling. Move the shared tool-message constructors and quoting helper only as needed. Put reusable library view metadata beside the SQL projection, with no database-owned prompt prose. | Library Q&A works across supported provider modes; Stop, panel switching, corrupt storage, and view changes behave correctly. |
| 2 Small writes | Add `create_lorebook_entry` and `set_pipeline_toggle`, their narrow shared behavior, commit settlement, mutation refresh, and regression tests. | “Add lore for Y” and “turn the Editor off” work and visibly reconcile. No unknown-outcome retry. |
| 3 Reviewed edits | Proposal store, atomic expected-value updates, Apply/Cancel cards, stale handling, and the four text edit tools. Extract message/card write helpers with their existing invariants. | The approved preview is what commits, concurrent edits are preserved, and repeated Apply writes once. |
| 4 Remaining actions | Other creates, chat-world selection, checkpoint, branch selection, and generation handoffs. Move conversation copy helpers only here. | Targets remain correct across navigation, branches and group chats; normal chat settlement owns generated replies. |
| 5 Media | Shared on-demand dispatch and toggle handling, image/speech tools, linked jobs, lazy streams, GPU refusal. | Explicit media generation works independently of automatic toggles where supported, and Stop/deletion settle correctly. |

Keep behavior-preserving extractions with their first consumer, and run the existing regression checks after each. Do not make a broad P0 refactor a prerequisite for the read assistant. Milestones 1 and 2 form the first useful release; the later milestones can ship independently as their contracts pass.

## Verification

Prioritize tests of observable behavior and failure boundaries:

- **Loop and adapters:** step → arguments → execution → reply; zero-argument tool; matched call/result replay; invalid or cut-off model response retried before execution; strict write arguments; bounded accumulated context; expected tool failure; cancellation and call limits.
- **Writes and Apply:** no write during proposal; preview matches committed macro-expanded text; ordinary API edit between preview and Apply returns 409; an edit racing the actual Apply cannot be overwritten; double Apply has one effect; unknown/restarted proposal is inert; Stop during commit settles and preserves committed IDs.
- **Targeting and projections:** message/document/entry ownership, group ambiguity, active versus inactive branches, forbidden fields/URL inputs, settings secrets, and errors containing credentials. Test the restricted SQL path rather than duplicating its existing sandbox tests.
- **Frontend:** malformed/quota-failing storage, escaped diffs, inert reloaded cards, IME Enter, operation-scoped Stop, no action dispatch from model markup, view change during Apply/handoff, and target-aware refresh.
- **Media:** safe disabled-workflow dispatch, GPU-swap refusal, missing setup as a tool error, lazy stream lifetime registered as a job, assistant Stop affecting only its child, and deletion during generation.

Manually check both panels, collapse/reload, log and panel resizing, document mode, and mobile widths at or below 900px. Exercise an action while changing views and Stop after a write has started. Provider smoke checks should include text completion, a normal tool endpoint, and the structured-output adapters already supported by Orb; verify the step enum at the current milestone's tool count. Use scripted clients for automated tests and configured providers for smoke checks. Do not assume a committed browser harness exists.

For implementation milestones, run the repository checks:

```sh
./scripts/format_backend.sh
./scripts/format_frontend.sh
./scripts/lint.sh
./scripts/tests.sh all
```

After behavior ships, document the actual contract in `docs/architecture/assistant-chat.md` and add only relevant notes to SSE, cache, and extension docs. This plan stays under `docs/plans/`, which is already excluded from the published documentation site.
