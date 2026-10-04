# Secondary Workflows

A workflow is an optional feature that plugs into Orb without adding feature
logic to the core turn pipeline. It is a Python record in a process-local
registry, plus any hooks, state, attachments, and frontend code it needs.

Built-in examples are `prose_rewriter`, `tts`, `image_gen`, and
`format_consistency`.

## What a workflow can do

A workflow may:

- add work before or after a generated turn;
- expose a conversation-scoped action or a conversation-less query;
- accept a file the user uploads for a character, such as a voice clip;
- produce an attachment for a message and later regenerate, reroll, rehydrate,
  activate, or delete it;
- keep state at conversation, message, character, or global config scope;
- register frontend cards, buttons, widgets, SSE handlers, audio, text effects,
  or click actions.

The framework owns registration, routing, persistence, locks, and common UI
chrome. The workflow owns its feature logic.

## Where things live

### Backend

| Path | Purpose |
|---|---|
| `backend/workflows/registry.py` | Workflow records, subscriptions, plug-in discovery, lookups, and state access |
| `backend/workflows/contracts.py` | Hook types, context dataclasses, and `ToolSpec` |
| `backend/workflows/toolkit.py` | Stable imports for workflow authors |
| `backend/prompting/tool_catalog.py` | Ordered tool lookup and workflow-tool registration |
| `backend/workflows/attachment_cache.py` | Attachment storage, variants, budget, and eviction |
| `backend/workflows/__init__.py` | Plug-in discovery and host-adapter hook bindings |
| `backend/pipeline/workflow_bridge.py` | Pipeline hook dispatch and attachment staging |
| `backend/api/routes/workflows.py` | Workflow and attachment routes |

Each workflow is a package such as `backend/workflows/tts/`, named for its id.

Code under `backend/workflows/<id>/` is a plug-in slice. It may import its own
package and `backend.workflows.toolkit`, but not other framework modules,
application layers, or peer workflows. Root modules directly under
`backend/workflows/` are host adapters and own the integration with prompting,
inference, persistence, and the pipeline. The toolkit must be consumed through
explicit names in its literal `__all__`; wildcard imports, importing the module
object, and private names are rejected. The backend layer checker enforces this
boundary.

### What belongs in `analysis/` and what belongs in the workflow

`analysis/` answers questions about text that more than one consumer asks.
A workflow owns the policy it applies to those answers: which reading wins,
what to change, and what to report.

`format_consistency` is the worked example. Reading a message's roleplay
markup is shared — `analysis/text/markup.py` classifies the dialogue and
narration axes for markup repair, for TTS speech selection, and for the image
camera's narration extraction, and `analysis/text/roleplay.py` and
`analysis/text/roleplay_segmentation.py` hold the span parser all three read.
Repair is not shared: `workflows/format_consistency/normalization.py` owns the
baseline window vote, the rewrite rules, the skip policy, and
`FormatDriftReport`. Nothing outside the workflow imports them, and the toolkit
does not re-export them — a toolkit entry would hand another plug-in this
workflow's repair policy by accident and would import the workflow back into
its own API.

The line to apply to a new workflow: publish through the toolkit the
primitives a plug-in needs to act on shared text, and keep in the workflow the
decisions only that feature makes.

### Frontend

| Path | Purpose |
|---|---|
| `frontend/workflow_api.js` | Public plugin facade |
| `frontend/workflow_loader.js` | Loads one module per manifest entry |
| `frontend/state.js` | Workflow registries and UI state |
| `frontend/chat.js` | Chat facade used by the workflow API |
| `frontend/chat_stream.js`, `frontend/chat_workflow.js` | SSE dispatch and workflow presentation/attachment actions |
| `frontend/workflows/<id>/index.js` | Workflow entry point |
| `frontend/default_widget.js` | Fallback image, audio, video, or download view |

Frontend workflow code imports `/static/workflow_api.js` and its own relative
modules. It should not import core frontend modules directly.

Use the facade's `responseError(response)` for a failed HTTP response and
`sseError(data)` for a terminal core `error` event. Both preserve useful messages
from structured payloads. Lazy workflow streams can emit a core `error` when a
hook raises, so handle it alongside the workflow's own terminal events. See the
[shared failure contract](sse-stream.md#shared-failure-handling).

## Declare a workflow

A plug-in package declares its workflow as `WORKFLOW` in its `__init__.py`: a
`Workflow` record whose `id` is the package name, carrying its hook
subscriptions. `subscription(...)` builds each one and holds the hook to the
signature its slot calls it with.

```python
from ..toolkit import HookType, Workflow, subscription
from . import hooks

WORKFLOW = Workflow(
    id="my_workflow",
    display_name="My workflow",
    tools=[],
    config_defaults={},
    config_schema=None,
    produces_artifacts=False,
    subscriptions=[
        subscription(HookType.POST_PIPELINE, hooks.post_pipeline, priority=0),
    ],
)
```

`id` is the boundary key used in URLs, JSON, tools, and static module paths.
Tool names must be unique and must agree across `ToolSpec.name`, the schema,
and `tool_choice`. A workflow binds each hook type at most once, and only a
`produces_artifacts=True` workflow may bind `REGENERATE`, `REROLL_GEN`, or
`EXPORT`.

Adding a workflow needs no edit to a host file. When `backend.workflows` is
imported, it registers every package directly under `backend/workflows/` in
package-name order, then calls `finalize_registry()`, which verifies that an
artifact-producing workflow has both regeneration hooks. Modules beside those
packages are host modules and are never registered. Startup stops with the
error when a package fails to import, lacks a `WORKFLOW` record, declares an id
other than its package name, or declares an invalid subscription, and when a
directory of Python modules has no `__init__.py`.

Registration order is the manifest order. The frontend loads workflow modules
in that order, so it also orders the workflow rows in the Tools panel and the
workflow buttons on a message. Re-registering a workflow keeps its position.

A hook that needs a layer below the toolkit lives in a host adapter, which
binds it to the plug-in's id with `subscribe` in `backend/workflows/__init__.py`.
The Prose Rewriter's post hook is bound this way because it runs the local model
runtime.

Workflow tools append after the fixed built-in tool order, in registration
order. Re-registering an existing tool replaces its contract without changing
its position; removing a tool on workflow replacement removes it through the
framework-owned catalog API. The catalog itself is not part of the plug-in API.

### Hook types

| Hook | Runs | Return shape |
|---|---|---|
| `PRE_PIPELINE` | During a turn, before the main passes; all hooks in priority order | Async stream of events or pipeline instructions |
| `POST_PIPELINE` | During a turn, after the main passes; all hooks in priority order | Async stream of events, draft changes, state, or attachments |
| `ON_DEMAND` | Conversation-scoped trigger route | A JSON object or `WorkflowEventStream` |
| `REGENERATE` | Attachment regeneration route | A list of new attachment records |
| `REROLL_GEN` | Attachment reroll and rehydrate routes | Bytes, or bytes plus consumption metadata |
| `QUERY` | Global configuration/discovery route | One response object |
| `UPLOAD` | Character-scoped file upload route | One response object |
| `EXPORT` | Attachment export route; optional | An `ExportedFile`, or `None` when nothing is left to export |

`QUERY` has no conversation or LLM client. It is for setup and discovery, such
as checking an external server before a conversation exists. `UPLOAD` receives
one file for one character, also without a conversation or client, and its
query-string parameters as a second argument. The framework checks the card
and a 25 MB cap, and holds no lock while the hook runs: the hook takes the
toolkit lock for any state it rewrites, so slow processing of the file blocks
nothing. The TTS voice clone is the worked example. The message-level
regenerate route reruns the normal turn pipeline; `REGENERATE` is only for an
attachment.

## Enablement

The settings row is the source of truth:

| Setting | Meaning |
|---|---|
| `workflows_globally_enabled` | Master switch |
| `workflow_enabled` | JSON map of per-workflow overrides; missing means enabled |

Effective state is `global_on AND local_on`. The backend applies it to pipeline
hooks and hook-firing routes. Config, manifest, query, and attachment-consumption
routes remain available so a disabled workflow can be configured and existing
attachments can still be viewed.

The frontend mirrors the same predicate and hides disabled workflow controls.
Existing attachment renderers remain available by design.

## Hook contexts

Contexts are frozen dataclasses. Their mutable fields are read-only views, with
two deliberate exceptions: `turn_scratch` and the service objects used by the
framework.

| Context | Provides | Notes |
|---|---|---|
| `PreCtx` | Conversation, history, current user text, settings, prefix, tool map, client, cache tracker | `turn_scratch` is shared with PostCtx |
| `PostCtx` | Conversation, final history, effective user text, Director output, merged tools, prefix, client, cache tracker, resolved Agent execution target (`agent_client`, `agent_model_name`) | May stage a draft, state, or attachment |
| `OnDemandCtx` | Conversation, history, current user text, settings, client, character | Trigger actions |
| `RegenCtx` | Conversation, message and attachment ids, pre-anchor history, settings, client, character, `phase(label)`, `keep(attachment)`, `emit(event, data)` | Attachment regeneration |
| `RerollGenCtx` | Conversation, message and attachment ids, settings, client, prior consumption metadata, `replay` | Shared by reroll and rehydrate |
| `QueryCtx` | Settings | No conversation and no client |
| `UploadCtx` | Settings, character id and card, filename, file bytes | No conversation, client, or lock |
| `ExportCtx` | Attachment id, the row without its bytes, decoded consumption metadata, `stored_bytes()` | Bytes load only when the hook asks for them |

Every context, and every control-event `type` a hook yields (`EV_ENABLE_TOOLS`,
`EV_SYSTEM_PROMPT`, `EV_DRAFT_REPLACED`, `EV_ATTACH_ARTIFACT`,
`EV_SET_MESSAGE_STATE`), is a toolkit export, so a plug-in annotates its hooks
without reaching past the toolkit. Both `subscription` and `subscribe` are
typed per hook type: Pyright rejects a hook whose signature does not fit its
slot, such as a post hook that returns instead of yielding. The host's
`get_subscription` and `iter_subscriptions` preserve that callable type through
lookup and route gating, so dispatch is checked too.

Annotate pre/post generators with `AsyncIterator[PreEvent]` or
`AsyncIterator[PostEvent]`, and on-demand event streams with
`AsyncIterator[PublicEvent]`. These types are toolkit exports, as are their
individual control-event types. Pyright checks required payload keys and the
instructions allowed in each slot; for example, a pre-hook cannot replace a
draft. Public events require `event: str` and optionally `data: str | dict`;
workflow-specific JSON remains open. The runtime still validates output from
untyped plug-ins and drops malformed events.

```python
from collections.abc import AsyncIterator

from ..toolkit import EV_SYSTEM_PROMPT, PreCtx, PreEvent


async def pre_pipeline(ctx: PreCtx) -> AsyncIterator[PreEvent]:
    yield {"type": EV_SYSTEM_PROMPT, "block": "A workflow instruction."}
```

Context collections are annotated as read-only mappings and tuples, and
clients and cache trackers use their concrete service types. `turn_scratch`
remains a mutable dictionary for workflow-owned data. Static contract
regressions in `tests/unit/workflows/test_static_contracts.py` check valid
declarations and dispatch alongside deliberately invalid examples.

For group work, `character` identifies the relevant speaker. A
`RerollGenCtx` with `replay=True` reproduces stored generation parameters;
`replay=False` lets a new variant use current workflow settings.

## State and locks

State is JSON-backed and accessed through the toolkit. Use a matching lock for
read-modify-write operations.

| State | Scope | Lock |
|---|---|---|
| `workflow_state` | Conversation + workflow | `workflow_state_lock(cid, wid)` |
| `workflow_message_state` | Message + workflow | Owning conversation lock |
| `workflow_character_state` | Character + workflow | Conversation lock, then `workflow_character_state_lock` |
| `workflow_config` | Workflow | `workflow_config_lock()` |
| Attachments | Root attachment group | Framework's root lock |

The primary runtime import surface is `backend.workflows.toolkit`. Hook contexts
carry the LLM clients; the toolkit provides semantic host operations, read-only
database queries, state getters/setters, `forced_tool_call`, attachment
insertion, and workflow locks. Raw prompting, inference, and tool-catalog
objects are intentionally not exposed to plug-ins. Mutating core database
helpers are also excluded.

## A workflow inside a turn

The bridge gives every turn one scratch dictionary, client, cache tracker, and
schema override map. It then follows this flow:

```text
PRE_PIPELINE hooks
        ↓
Director → Writer → Editor
        ↓
retain post-Editor draft
        ↓
POST_PIPELINE hooks
  Prose Rewriter (-20)
  Format Consistency (-10)
  later text/artifact hooks (0+)
        ↓
persist assistant message, state, and attachments
        ↓
SSE done
```

Pre-hooks can add system blocks, enable tools, or emit public events. Post-hooks
can replace the draft, set message state, stage attachments, or emit public
events. Hooks run in subscription priority order, and equal priorities run in
registration order. The Prose Rewriter is a registered post-hook; its negative
priority puts it before Format Consistency and artifact workflows. Its standard
workflow toggle turns the rewriter on for both automatic runs and the
saved-message rewrite route, and its `automatic` config gates the post-hook
alone. Its workflow card manages the model through the generic Local ML
routes, which also own the shared llama-server runtime. A hook failure is
isolated so the main reply and other workflows can continue.

Stop is checked before each hook and after its locks are acquired, so no hook
starts once the turn is stopped. The running hook is interrupted: its pending
step is cancelled and its generator closed, so a local rewrite or remote
render is torn down rather than waited for. Events it would have yielded after
the stop, such as an auto-play cue, are dropped. What it had already handed
over stays with the reply: a whole `draft_replaced` draft, a complete
`attach_artifact`, and `set_message_state`. A hook's own `draft_update`
previews never do. Pre-hooks follow the same rule. A hook needs no cancellation
code of its own beyond letting `CancelledError` propagate, although its
`finally` blocks do run.

Use `forced_tool_call` for a one-shot tool call. Pass the context's prefix,
enabled tools, schema overrides, client, and cache tracker so the call follows
the same prompt and cache rules as the main turn. Its budget is the Agent lane's
configured `max_tokens`, unchanged; a workflow does not pick its own.

Public hook events pass through to SSE after envelope and turn-ownership
validation at both `PRE_PIPELINE` and `POST_PIPELINE`. Names beginning with `_`
are internal. The turn host protects terminal verdicts (`done`, `error`), message
identity (`user_message_created`), group events (`speaking_plan`, `speaker_start`,
`speaker_done`), authoritative content (`token`, `writer_rewrite`), and its pass
and persistence reports (`director_start`, `director_done`, `step_start`,
`writer_done`, `editor_done`, `decisions`, `feedback`, `state`,
`world_change_proposed`, `workflow_attachments_rejected`). A hook cannot publish
these directly. Invalid events are dropped with a logged reason; that hook and
later hooks continue. A `draft_replaced` control still asks the bridge to publish
its own `writer_rewrite`; attachment and message-state controls stay internal.

Four shared events are supported: `phase_status` requires a string `channel`
and a string `label` or `state`; `reasoning` requires string `pass` and `delta`;
`draft_update` requires a string `draft`; `warning` requires a string `headline`.
When supplied, warning text fields and phase fields must be strings, and
`warning.status` must be an integer. Feature-owned JSON extensions remain open,
as do custom event payloads. A cosmetic `draft_update` never changes the saved
reply; only a completed replacement control does. This turn policy does not
apply to on-demand, regeneration, document, or library event streams, whose
hosts own different contracts.

A useful shared event is `phase_status` with a channel that
starts with `workflow:<id>`. On a turn stream its label becomes the status
bar's text for the running step, so keep it a short description of the work
(`Rewriting prose…`); outside a turn it shows as a separate pill.

## Attachments

Artifact-producing workflows write through `insert_workflow_attachment` or
yield an `attach_artifact` instruction from `POST_PIPELINE`. An attachment has
a workflow id, filename, MIME type, and exactly one byte source (`data` or
`path`). The framework validates it before persistence.

Attachments are arranged as a flat variant group: one root and its siblings.
The active sibling is user-selectable. The cache stores bytes in
`workflow_attachments` and enforces a configurable byte budget. When space is
needed, older accessed rows are evicted by replacing their bytes with the
`[evicted]` marker.

The message listing (`GET /api/conversations/{cid}/messages`) carries each
attachment without its bytes: every other column, plus `evicted` (1 when the
bytes are the marker). Bytes load from the content routes below, which answer
with an ETag the browser revalidates, support byte ranges, and return 410 for
an evicted row. A frontend widget builds the URL with `workflowAttachmentUrl`;
audio plays through `playAudio` segments of `{ row }`, or
`{ row, byte_start, byte_end }` for one clip packed inside a larger attachment.

Supply a seed and JSON generation metadata when an artifact can be recreated.
That lets the user rehydrate evicted bytes. The same `REROLL_GEN` hook handles:

- **reroll** — a new seed and a new sibling using current settings;
- **rehydrate** — the stored seed and parameters, restoring the same row.

`REGENERATE` also creates siblings, while `activate` and `delete` only change
the variant group. A regenerate hook that makes several variants in one run
saves each through `await ctx.keep(attachment)` as it lands, rather than
returning them all at the end, so Stop keeps what was already made. An
on-demand hook does the same with `insert_workflow_variant`, passing the ids it
has saved so far as the `group` and the last one as `shown`. Either way, a new
variant becomes the active one only while the previous one from the same run is
still on show, so a user who paged to an earlier variant mid-run stays on it.
`set_workflow_consumption_metadata` rewrites a saved row's consumption metadata
when something about it, such as a review, is known only after it was saved.
Existing artifacts remain readable when their workflow is disabled.

A workflow whose stored bytes are not the best copy of an artifact can
subscribe `EXPORT` to serve a download. The hook receives the row without its
bytes and a `stored_bytes()` loader, so it can fetch the file from where it was
made without reading the stored copy. It returns an `ExportedFile` with bytes,
MIME type, filename, and an optional `note` the route sends as
`X-Orb-Export-Note`. Use the note to say when the file is not the best version
and why. Image generation fetches the original from ComfyUI and converts the
stored copy only when no original is available.

## HTTP surface

The framework exposes:

```text
GET  /api/workflows
GET  /api/workflows/{wid}/config
PUT  /api/workflows/{wid}/config
POST /api/workflows/{wid}/enabled
POST /api/workflows/{wid}/query
POST /api/characters/{card_id}/workflows/{wid}/upload
POST /api/conversations/{cid}/workflows/{wid}/trigger
POST /api/conversations/{cid}/workflows/stop
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/activate
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete
GET  /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/in-flight
POST /api/conversations/{cid}/workflow-attachments/access
GET  /api/workflow-attachments/{aid}/content
GET  /api/workflow-attachments/{aid}/export
```

User uploads have the same split: `GET /api/user-attachments/{aid}/content`.

A hook reports a failure the user can act on by raising the toolkit's
`WorkflowUserFacingError`; its message becomes the response detail, with status
502. Two subclasses narrow the status: `WorkflowInputError` (400) for input the
user supplied that cannot be used, and `WorkflowUnavailableError` (503) for
something not set up yet, such as a model that is not downloaded. Any other
exception is logged and answers 500 with a generic message.

`in-flight` reports `{"in_flight": bool}` for the attachment's canonical root:
whether a request still holds the group's lock. Regenerate, reroll-gen,
rehydrate, and delete hold that lock for their whole duration and release it
only after their write commits, so `false` means nothing in flight can still
change the group. It exists for a client whose own connection died mid-render,
which otherwise cannot tell a running render from one the server already failed.
A single `false` does not prove failure -- a queued request has not reached the
lock yet -- so clients confirm it across consecutive polls.

With `Accept: text/event-stream`, `regenerate` streams each `ctx.phase(label)` as
`phase_status` and each `ctx.keep` that saved a row as `regenerate_sibling`
(`{attachment_id}`), then `regenerate_done` (the JSON body, whose `attachments`
include the kept rows) or `regenerate_error` (`{status, detail}`). The render
outlives a dropped stream.

`ctx.emit(event, data)` adds a workflow's own structured event to that stream,
for widget data that does not belong in the `phase_status` label. The name must
start with `<workflow_id>_` and must not be one of the four names above, and
`data` must be a JSON-serializable dict. An event that breaks these rules is
dropped and logged once, never raised, so a malformed status event cannot abort
the render. The client dispatches each one to the handler registered for it with
`registerWorkflowEventHandler`, called as `(data, null)`, the same way a turn
stream calls it. Outside a stream, `emit` does nothing.

Image-generation review reasons travel only in the next revision's `phase_status`
labels, including ComfyUI queue and rendering updates. They are transient progress:
review reasons and timeline metadata are not persisted on attachments or emitted
as separate review events. On-demand generation and regeneration share this path;
their normal phase cleanup removes the reason on success, Stop, or failure.

Workflow renders run beside the chat, so the chat Stop button leaves them
alone; the button that started a render is its Stop button while it runs.
Regenerate, reroll-gen, rehydrate, and the on-demand trigger run as
per-conversation jobs. Each of those routes takes an optional `?job=<id>` the
client picks, and `workflows/stop?job=<id>` cancels that job alone (without
`job`, every job in the conversation), answering `{stopped, settled}` once
they have ended (bounded); the stopped request then answers 409. `stopped: 0`
means no job ran under that id yet or any more. A job already writing its
sibling or restored bytes is waited for, not cancelled; an on-demand hook
that writes its own attachment may still commit it as it is cancelled, so
after a stop the client refetches and the saved rows decide. An on-demand
stream is stopped by closing it instead, which cancels the hook; the hook
must let its cleanup finish (the stream awaits it). A cancelled ComfyUI
render withdraws its prompt by id; cloud image APIs are synchronous, so a
stopped cloud render only drops the request and may still be billed.

The manifest returns workflow identity and config form metadata. Config is a
full replacement; a workflow's `config_normalizer` owns its valid shape and is
used on both read and write.

`get_workflow_config` returns the stored slot when non-empty, otherwise an
independent copy of `config_defaults`, including nested objects and lists. It
does not merge partial stored configuration with defaults. A workflow that adds
settings over time supplies missing values in its normalizer. Clearing the
stored config with `{}` restores the defaults.

## Frontend integration

At boot, the frontend fetches the manifest and imports
`/static/workflows/<id>/index.js` for each entry. Top-level registration calls
run when the module loads.

The facade in `workflow_api.js` is the frontend ABI. It is additive-only: new
exports may be added, with a `WORKFLOW_API_VERSION` bump, but existing names and
signatures do not change. Common registration points are:

```js
registerWorkflowInspectorCard(wid, render)
registerWorkflowToolsPanelCard(wid, render)
registerWorkflowMessageButton(wid, render)
registerWorkflowEventHandler(wid, event, handler)
registerAttachmentRenderer(wid, render)
registerWorkflowPipeline({ id: wid, passes })
registerTextEffect({ id, label })
registerClickHandler({ id, claims, onClick })
```

Inspector cards and pipeline reasoning render at the bottom of the Inspector's
Main tab.

Use `registerAttachmentRenderer` for the workflow's own widgets. It is not
enablement-gated so stored artifacts remain visible. Other workflow-owned cards,
buttons, and event handlers are gated by workflow id.

Buttons and inputs use delegated actions rather than globals or inline event
handlers:

```html
<button data-wf-action="my_workflow:refresh">Refresh</button>
```

```js
registerAction("my_workflow", "refresh", (element, event) => { /* ... */ });
```

The handler receives the element carrying the action and the event. Click is
the default; `data-wf-on` names other events, space-separated: `change`,
`input`, `keydown`, or `dragover dragleave drop` for a drop target. The core UI
uses the same mechanism, so a workflow's markup may also name a core action. The
lint step fails on an action name that nothing registers.
Handlers may return a promise; synchronous throws and asynchronous rejections
are logged with the action name.

The facade also provides API helpers, modal and notification helpers, workflow
phases, shared audio controls, text effects, message access, group cast data,
and conversation repaint/refetch helpers.

A button that starts a long render is that render's Stop button while it
runs. `startWorkflowJob({ convId, title })` returns a job: `job.url(path)`
names it on the request, `job.show(button)` turns the button over in place,
`job.stop()` cancels it (`startWorkflowJob({ title, controller })` aborts the
controller instead, for a stream), and `job.end()` turns every copy of the
button back. A renderer that may repaint the button mid-render draws it from
`stopButtonState(job, idleTitle)`. `workflowActionJob(msgId, attId)` is the
job of the core regenerate, reroll, or restore running on an attachment, for a
workflow that draws its own control for those. `stopWorkflowJob(jobId)` stops a
live job by id (the `data-wf-job` a Stop button carries), so a control the
framework did not render can stop a framework-owned job; an ended id is ignored.

An attachment renderer receives `{ att, buttons, defaultHtml, siblings, msgId,
rootId, job }`: the shown attachment, the group's attachments in display order,
the message and group root ids, and the group's running regenerate job (or
null). Treat them as read-only. `activateWorkflowVariant(msgId, rootId,
siblingId)` shows another variant through the arrow buttons' own path. A widget
that draws its own controls instead of `buttons` reaches the same operations
through `stepWorkflowVariant(msgId, rootId, delta)`,
`regenerateWorkflowAttachment(msgId, attId, button)`,
`rehydrateWorkflowAttachment(msgId, attId, button)` (*button* becomes the
render's Stop button), and `deleteWorkflowAttachment(msgId, rootId)`, which asks
before deleting the shown variant or the whole group.
`registerRegenerateSettled(wid, (msgId, rootId) => …)` is called when a
regenerate ends, on every outcome, because a failed or stopped run repaints
nothing; a widget holding live run state clears it there.

`refreshConversationMessages` and regenerate sibling refreshes also work while
a chat reply streams. They merge only workflow attachments into existing
message objects and patch the artifact areas in place; live prose, pending
messages, and their DOM nodes stay intact. Once the reply finishes, normal full
message refreshes resume.

## Authoring checklist

1. Create the package `backend/workflows/<id>/`; its name is the workflow id.
2. Implement hooks with the context and return shapes above.
3. Export the `Workflow` record as `WORKFLOW` from the package's `__init__.py`,
   with a `subscription(...)` for each hook. Discovery registers it.
4. Use the toolkit and matching locks for state changes.
5. If producing artifacts, implement `REGENERATE` and `REROLL_GEN`, and store
   recovery metadata where rehydrate is useful.
6. Create `frontend/workflows/<id>/index.js` and import only the facade plus
   relative modules.
7. Register only non-reserved SSE events and use `data-wf-action` for controls.
8. Add config defaults/schema and normalize the effective config if needed.

## Quick lookup

| Need | Start here |
|---|---|
| Add a hook | `contracts.py`, `registry.py`, and `workflow_bridge.py` |
| Call a tool | `toolkit.forced_tool_call` |
| Store workflow state | Toolkit state helpers and the matching lock |
| Produce an attachment | `attach_artifact` and `attachment_cache.py` |
| Add a custom stream event | Hook event plus `registerWorkflowEventHandler` |
| Accept a file for a character | `UPLOAD` hook; `tts/hooks.py:upload` |
| Add UI | `workflow_api.js` registrars and `registerAction` |


## Media ownership across edits

Media generation stores its source text in generation metadata. A commit for an
older source keeps the artifact but does not activate it; conditional sibling
activation also preserves a variant selected during rendering. Speech consumption
metadata carries its own text and blocks, and karaoke against changed message text
is disabled. The automatic TTS hook snapshots the voice profile before generation,
while playback preferences remain live. API event streams stay registered as
workflow jobs until they settle, so deletion and restore account for their renders.
