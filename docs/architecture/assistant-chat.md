# Panel Assistant

The assistant is a small chat docked at the foot of the open Tools panel or
Inspector. It answers questions about the library and carries out changes the
user asks for, through a fixed set of tools on the Agent lane. It runs outside
the turn pipeline: it never joins a chat's turn, its prompt, or its KV-cache
prefix.

This page describes the contract the code keeps: how a request is run, what
each kind of tool may do, how writes settle, and how the browser stores and
refreshes what changed.

## Layers

| Path | Owns |
|---|---|
| `backend/features/assistant/` | The facade, contracts, the model loop and its prompt. It receives tool callables and imports no API, pipeline, workflow or peer feature code. |
| `backend/api/assistant_tools.py` | The read tools, the two small writes, projections and bounds, and `build_tools`, which assembles every tool for one run |
| `backend/api/assistant_edits.py` | The four reviewed-edit tools, the proposal store, Apply, and the per-run record of texts read whole |
| `backend/api/assistant_actions.py` | The remaining creates, chat actions and handoffs |
| `backend/api/assistant_media.py` | The media tools |
| `backend/api/workflow_dispatch.py` | On-demand workflow dispatch, shared with the trigger route |
| `backend/api/conversation_copy.py`, `database.save_message_edit` | The checkpoint copy and the message edit, shared with their original routes |
| `backend/api/routes/assistant.py` | `POST /api/assistant/run`, `/stop` and `/apply`, and the view check |
| `frontend/assistant_view.js` (layer 1) | Transcript validation, replay, stream-event handling and markup; no DOM lookups or requests |
| `frontend/assistant_chat.js` (layer 5) | The dock, its run, Stop, Apply, handoffs and refreshes |

Tools are built per run, bound to the view captured at Send. The assistant's
tools are not in `prompting/tool_catalog.py` and never reach a turn.

## A request

```text
POST /api/assistant/run?operation_id=<id>
  { items: [... , {kind: "user", text}], context: {mode, conversation_id?, character_id?, document_id?} }
```

`items` is the browser's transcript, at most 100 entries of at most 8,000
characters, ending with the new request. `context` is the view captured at Send.
The server checks every id and how they relate before the model is called: a
chat view names its chat, a document view names its document and nothing else,
a character must be the chat's own or a member of the group. A group view never
infers a character; tools that need one ask for it.

The run streams through `sse_stream` under the reserved key `assistant:run`,
which no conversation id can spell. That makes one run at a time server-wide; a
second run, from another tab, gets `Another generation is already running`.
`POST /api/assistant/stop?operation_id=<id>` aborts only the run that
registered that operation id and waits for it to settle, as a chat's Stop does
(see [Stop, disconnect, and errors](sse-stream.md#stop-disconnect-and-errors)).
The browser registers the run in `S.operations` with wire-style target keys
(`conversation_id`), so a read-only run never appears in a chat's delete dialog.

## The model protocol

Each step is two forced calls on the Agent lane, with its model and
`max_tokens` and reasoning off:

1. `assistant_step` with `{tool, message}`. `tool` is an enum of the run's
   tools plus `reply`; `message` is a short note to the user, or with `reply`
   the final answer.
2. For a tool that takes arguments, that tool's own flat schema. A
   zero-argument tool skips this call.

The tool runs and its bounded result is appended. Every replayed call has its
own id and exactly one result, including the step call before the argument
call. On an endpoint that does not honour forced tool choice, each call is sent
only the schema it forces.

Arguments are read strictly: only a whole JSON object as the one call in
`tool_calls`, or one whole call in content (bare, as the only item of a list,
in Hermes tags, in a code fence, or one complete Gemma native call). A salvaged
or partial object, several calls (in `tool_calls` or in content), a repeated
key, or `finish_reason=length` is unusable: taking the first of several would
drop the rest unseen. An unusable reply is retried once against the same
prefix, then the run ends with `error`; nothing in it runs, and the retry
counts toward the model-call budget. When it made several calls, the retry adds
a note to its last message, the request or a tool result, telling the model to
make exactly one call now and the rest one at a time; no call from the refused
reply is replayed. Every assistant call also asks for one call per reply where
the request carries native tools on an OpenAI-shaped route
(`parallel_tool_calls: false`); a server that refuses the field by name sheds it
like any other optional field, and the Messages translation drops it. Arguments are validated by a strict pydantic model: unknown fields and
coerced types are refused and come back as a correction, and a strict-schema
`null` for an optional field is the only normalization.

A run allows 12 tool executions and 30 model calls, retries included. When the
executions are spent the model gets one more step to reply.

### Working context

The system prompt is stable for a given tool set and lists the tools itself,
since a text-completion or structured-output endpoint is sent no tool list. It
is the one place a tool's description appears: each schema's own description
only points there, so a native tool endpoint is not sent every description
twice on every call. Replayed history comes next, then the latest request with
its view note after the user's own words.

Replay and run together are held to 20,000 characters, excluding the system
prompt. When they pass it, the oldest full tool results are replaced by their
summaries and ids, then the oldest history goes; the newest result and the
request are never dropped. A request that cannot fit at all is refused. Each
result is at most 6,000 characters as encoded; tools bound their own output, and
an oversized one becomes an error result rather than a cut value.

Browser history is untrusted. Tool and outcome entries replay as quoted
records on the assistant's side, never as `tool` messages or approvals. A user
entry with no answer after it replays as a quoted record of an interrupted
request, so it can never authorise a write again. Only results produced inside
the current run are native tool messages.

## Tools

Every tool has a name, a description, a flat argument model, a behaviour class
and a callable. The prompt marks each class.

| Class | Tools | Behaviour |
|---|---|---|
| read | `get_context`, `list_characters`, `get_character`, `query_library`, `read_messages`, `list_worlds`, `list_lorebook_entries`, `list_personas`, `read_document`, `get_settings_summary` | Runs at once; changes nothing. |
| write | `create_lorebook_entry`, `set_pipeline_toggle`, `create_character`, `create_world`, `create_persona`, `set_chat_world`, `checkpoint_chat`, `switch_branch`, `generate_image`, `generate_speech` | Commits at once. |
| confirm | `edit_character`, `edit_lorebook_entry`, `edit_persona`, `edit_message` | Stores a proposal and ends the run; Apply commits it. |
| handoff | `regenerate_reply`, `rewrite_reply` | Checks a target and ends the run; the chat's own stream does the work. |

Reads project explicit allowlists, never whole rows: no avatars, extensions,
prompt overrides, workflow state, endpoints, models or credentials. Listings
page with `offset`, report `total` and `truncated`, and clip long text with a
`…[+N chars]` marker. Naming one field, message, entry or persona reads it whole
a page at a time. `query_library` uses the library's read-only SQL path with its
caps; its rows may come from branches the user left, which the result says. A
recursive scrubber redacts values under secret-named keys (`api_key`,
`password`, `secret`, `authorization`, `credentials`, `cookie`, a bare `token`,
and any key whose last word is `token`, `key`, `auth` or `secret`, such as
`authToken`, `hf_token` or `card_source_auth`; `tokens_est` and `max_tokens`
stay readable) as a backstop; the projections are the privacy boundary.

### Writes

A write never runs on a retried or refused reply, nor once Stop has landed: the
abort is checked again, with no await before the write starts. Once admitted it
settles whole through a Stop or a dropped connection, because a cancelled await
proves nothing about whether SQLite committed. A `mutation` event names what
committed only after the commit. An identical non-idempotent write is not made
twice in one request; the refusal tells the model it already happened. Toggles,
chat-world choices and branch selection set an explicit state and may repeat.

Creates go through each resource's own schema and writer with server-generated
ids, and a narrow text-only schema: no image, URL, extension or embedded book.
A create that matches an existing name still adds a new one. Chat mutations take
the chat's lane through `idle_chat_admission`, which refuses a chat being
deleted or generating, read their target again once admitted, and hold the lane
only for their own database work. `switch_branch` ends the run once it has
switched, since the chat then reads differently.

Pipeline toggles name a stored switch and the state wanted, never a flip. Each
writes only its own key, and workflow toggles go through `set_workflow_toggle`,
the route's own path with its runtime callbacks. `editor` maps to the Output
Auditor (on) or the Output Auditor and Length Guard (off). The result reports
stored and effective state, and a master switch that blocks it is reported, not
changed.

### Unknown outcomes

A write's `tool_start` carries `write: true` and the `target` it may change.
If the stream ends before its result, the browser records "Outcome unknown;
check the target", reads that target again once the run has settled, and never
resends the request. Replayed later, such an entry says the change may have
happened. A write whose `mutation` arrived but whose result did not is recorded
as done.

## Reviewed edits

An edit tool reads and validates its target and writes nothing. Text edits are
`replace_all` or `find_replace`, where `find` must occur exactly once. Text that
carries a read's clip marker is refused, as is a no-op edit.

`replace_all` needs the current text to have been read whole in this run. Each
run keeps a record of `(kind, id, field, hash of the text)`: a read adds a field
it returned unclipped, and paged reads that join up from the start cover a field
once they reach its end. Reading one lore entry or persona by its id shows its
name whole (up to 1,000 characters), so a long name can be replaced whole too;
a create records the texts it stored as sent. A field longer than 200 characters (the shortest clip
any read applies) may be replaced whole only when the record holds its current
text; a field over 4,000 characters never may. The refusal tells the model to
read the field whole or use `find_replace`. A continuation after Apply is a new
run with an empty record.

### The proposal store

Proposals live in process memory, at most 32, each for 15 minutes; a restart
forgets them. A proposal holds its canonical target, the field it changes, the
exact before and after texts, and the request and view it answers. Message
macros resolve once, when the proposal is made, so Apply commits the text the
preview showed.

```text
POST /api/assistant/apply  {proposal_id}
```

Apply names a proposal and nothing else. It compares the expected text and
writes in one transaction, through each target's write path: messages keep
greeting-template, Writer-draft and changeset invalidation under the chat's
lane; cards keep their chats in step; lore entries keep their revision. A
missing target answers 404, changed text 409 `stale`, a busy chat 409 `busy`
(retryable), and an unknown or expired proposal 410. Concurrent Applies commit
once, and a repeated Apply returns the first result.

When the stored text already equals the approved text, a card, lore entry or
persona counts as applied. A message re-saves nothing: if an earlier attempt of
this proposal raised without saying whether it committed, that attempt did, with
its invariants; otherwise another writer saved the same text, and the result
says nothing was written (`written: false`).

### Continuation

After a confirmed Apply, the browser may continue the request once:
`continue_proposal_id` on `/run` reuses the proposal's own request and view,
checked again, and the latest request carries the server's note of every
approval in that request. Replayed history never supplies one. The continuation
is claimed only once its stream is admitted, and given back if the run ended
before doing anything.

## Handoffs

`regenerate_reply` and `rewrite_reply` check that the reply is on the chat's
kept branch and the chat is idle, then end the run with `handoff`. Once the
assistant's stream has settled, and never after its Stop, the browser starts
the chat's own Regenerate or Magic Rewrite with the captured chat, message and
direction. It starts only while that chat is the one open and the reply still on
its branch; otherwise the transcript says why and nothing switches. From then
the chat's stream owns Stop and the reply.

## Media

`generate_image` and `generate_speech` make an artifact for one saved reply on
a chat's kept branch. They are present only when their workflow (`image_gen`,
`tts`) is registered with an on-demand hook.

A media tool runs the workflow's hook through `run_on_demand`, the same
dispatch as `POST /conversations/{cid}/workflows/{wid}/trigger`, with the same
workflow-state and character-state locks, character resolution, context and
failure mapping. The public route still refuses a disabled workflow; the
assistant skips only that gate, since the user asked explicitly. The body is
the plug-in's own on-demand contract (`{action, message_id}`), and the result is
read from what the hook returns: `attachment_id` or `error`, or a stream of
`phase_status`, `<wid>_render`, `<wid>_error` and `<wid>_done` events. Nothing
imports a plug-in, and the hooks stamp the source text as they do for the
button.

One exception keeps a toggle meaningful: an image is refused while Image
Generation is effectively off when the render would have swapped the GPU had it
been on. The swap plan is off whenever the workflow is, so that render would
load ComfyUI beside the chat model. It would have swapped only when the ComfyUI
setup asks for GPU swap, a Writer or Agent lane runs on a server Orb can unload
other than a llama-swap that hosts ComfyUI itself, and the default style, which
an on-demand render uses, renders on ComfyUI; the image workflow's own `styles`
query says where. A cloud render takes no GPU lease and is never refused. The
check runs before any prompt is composed, through `gpu_swap_host`; the refusal
says why and changes no setting.

The dispatch and any lazy stream it returns run inside one workflow job,
`start_workflow_job(cid, …, job="assistant:<operation_id>", message_id=mid)`,
until the stream settles; the stream is never returned as an HTTP response.
That job is linked to the run:

- the assistant's Stop (or a dropped connection) aborts the run's token, and
  the tool cancels the jobs filed under its run and waits for them to end,
  past the workflow Stop's bound if it must; a render the user started from
  the message is left alone;
- the chat's own Stop does not end it, as it does not end any render;
- deleting the message or the chat finds it by source, settles it, then
  deletes, as for any media job;
- the whole loop is not held under a turn hold, so a render can take its GPU
  lease between model calls.

A media tool is a write: it settles whole and reports progress. While it runs,
each hook `phase_status` label becomes a `progress` event. Its result names the
attachment ids its own job reported saving and a short status, never bytes;
a failure after the save is in its summary too. Only those ids count, since a
render the user started on the same message saves rows as well. A job that
started and saved nothing is a tool error. A job stopped or cut short that
reported nothing while new rows appeared on the message claims none of them: the
error says an artifact may have been saved as it ended and to check the message,
and commits no ids. A Stop waits for the job to end, past the workflow Stop's
bound if it must, so the result and the browser's re-read come after it has
settled. Missing setup, a refused or failed hook, and a render cut short are
tool errors with the hook's message.

A media job is media work in its chat while it runs: the dock registers it in
`S.operations` as `media` with `conversationId`, so that chat's delete dialog
names it. It does not appear on the message's own image or speech button, whose
re-click stops only the render the button started; the dock's Stop is its Stop.
Speech saved while Text-to-Speech is switched off plays at once: its player is
the workflow's message button, which the core still draws for a disabled
workflow on a message already holding that workflow's `actions`-placed
attachments. Its Regenerate and Restore items stay disabled until the workflow
is on again, since those routes refuse it. An image shows at once.
Because the hold on the assistant lane is server-wide, no other assistant
request runs during a long render.

## Events

| Event | Payload | Meaning |
|---|---|---|
| `start` | `{}` | The run was admitted. |
| `progress` | `{label}` | What the run is doing now, or a media job's phase. Not stored. |
| `assistant_message` | `{text, final}` | A step's note (`final: false`) or the answer. |
| `tool_start` | `{call_id, tool, write?, target?}` | A tool is about to run; a write names what it may change. |
| `mutation` | `{call_id, tool, resources}` | A write committed these resources. |
| `tool_result` | `{call_id, tool, ok, summary, ids}` | Its outcome, paired by `call_id`. |
| `confirm` | `{call_id, tool, proposal}` | A preview with its proposal id. |
| `handoff` | `{call_id, tool, handoff}` | The checked chat target to start. |
| `done` | `{reason, message}` | Terminal: `reply`, `stopped`, `limit`, `awaiting_apply` or `handoff`. An early end names the changes already committed; a write that found its state already set is not one. |
| `error` | Failure | Terminal: an unusable model reply, a request that cannot fit, or a provider failure. |

EOF without `done` or `error` is a lost connection, never an answer.

## The browser

The transcript lives in `localStorage` under `orb-assistant-transcript`, at most
100 entries, and the dock's expansion under `orb-assistant-open`. Loading
validates every entry from its known fields; malformed JSON is an empty
transcript, and a full or blocked store leaves the dock working from memory.
What is stored is user text, answers, tool summaries and committed ids, never
raw results, progress, live operations or an actionable proposal: a reloaded
proposal card is historical, and only the one proposal this page holds can be
applied. A new request ends that proposal's Apply.

Each request captures its view at Send; changing view affects the next request,
not a running one. The transcript shows a separator where the view changes,
and replays the view label with each request. Clear is disabled while work runs.
Send uses `tabLock.js`'s send permission. Assistant prose renders through the
message renderer inside `.msg-body`, where no element can name an action;
proposal diffs, user text and tool lines are escaped.

Committed resources are read again by id through the loaders that own them:
worlds and entries, the settings keys a toggle wrote (through the settings
store's queue), characters, personas, the chat list, a chat's worlds, a branch
change, and a message or media job's chat. A chat repaints only while it is the
one in view and its view token unchanged; nothing opens a chat or moves focus.
A lore entry open in the drawer takes a change made elsewhere field by field:
fields the reader left alone take the new value, and the notice appears only
when a field they changed was changed too. After a Stop, an error or a lost
connection, once the run has settled, the browser reads again every committed
resource, every write target whose outcome is unknown, and the message of every
media job, which may have saved an artifact as it ended. A run that answers
after a media job that committed nothing still reads that job's message again.
