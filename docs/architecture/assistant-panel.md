# Assistant Panel

The assistant is a chat in a right panel of its own, beside the chat, the
document editor or the lorebook drawer. It answers questions about the library
and carries out changes the user asks for, through a fixed set of tools on the
Agent lane. It runs outside the turn pipeline: it never joins a chat's turn, its
prompt, or its KV-cache prefix.

This page describes the contract the code keeps: how a request is run, what
each kind of tool may do, how writes and proposed edits settle, and how the
browser stores and refreshes what changed.

## Layers

| Path | Owns |
|---|---|
| `backend/features/assistant/` | The facade, contracts, the model loop and its prompt. It receives tool callables and imports no API, pipeline, workflow or peer feature code. |
| `backend/api/assistant/` | The tools over the existing readers and writers, and Apply. `__init__.py` holds `build_tools`, which assembles every tool for one run. |
| `backend/api/assistant/targets.py` | Bounded ids and labels, and the checks that resolve a tool's chat, character or reply from its arguments and the view |
| `backend/api/assistant/reads.py` | The twelve read tools, their projections and paging |
| `backend/api/assistant/toggles.py` | The switch table, `set_pipeline_toggle`, and the switch summary |
| `backend/api/assistant/setters.py` | A lore entry's switches and keywords, a world's name and flags, the Length Guard's limits, the phrase bank |
| `backend/api/assistant/edits.py`, `proposals.py` | The four edit tools; the proposal store and Apply |
| `backend/api/assistant/actions.py`, `media.py` | Creates, chat starts and settings, chat actions and handoffs; the media tools |
| `backend/api/conversation_start.py` | The solo-chat writer the conversation route and `start_chat` share |
| `backend/api/workflow_dispatch.py` | On-demand workflow dispatch, shared with the trigger route |
| `backend/api/routes/assistant.py` | `POST /api/assistant/run`, `/stop` and `/apply`, and the view check |
| `frontend/assistant_view.js` (layer 1) | Transcript validation, replay, stream-event handling, Apply verdicts and markup; no DOM lookups or requests |
| `frontend/assistant_chat.js` (layer 5) | The panel, its run, Stop, Apply, handoffs and refreshes |

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
(`conversation_id`), so a run never appears in a chat's delete dialog.

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

`run_assistant(single_call=True)` is a second protocol, used today only by the
benchmark (`scripts/bench/assistant_bench.py`): each step is one call with
`tool_choice: required` over every tool's schema plus a `reply` tool, so the
model names the tool and its arguments together, and a zero-argument tool is
called with `{}`. It needs an endpoint that sends native tool schemas;
structured-output and text-completion modes send none.

Arguments are read strictly: only a whole JSON object as the one call in
`tool_calls`, or one whole call in content (bare, as the only item of a list,
in Hermes tags, in a code fence, or one complete Gemma native call). A salvaged
or partial object, several calls, a repeated key, or `finish_reason=length` is
unusable. An unusable reply is retried once against the same prefix, then the
run ends with `error`; nothing in it runs, and the retry counts toward the
model-call budget. When it made several calls, the retry adds a note to its
last message telling the model to make exactly one call now; no call from the
refused reply is replayed. Every assistant call also asks for one call per reply
where the request carries native tools on an OpenAI-shaped route
(`parallel_tool_calls: false`). Arguments are validated by a strict pydantic
model: unknown fields and coerced types are refused and come back as a
correction, and a strict-schema `null` for an optional field is the only
normalization.

A run allows 12 tool executions and 30 model calls, retries included. When the
executions are spent the model gets one more step to reply.

### Working context

The system prompt is stable for a given tool set and lists the tools itself,
since a text-completion or structured-output endpoint is sent no tool list. It
is the one place a tool's description appears: each schema's own description
only points there. Replayed history comes next, then the latest request with
its view note after the user's own words.

Replay and run together are held to 20,000 characters, excluding the system
prompt. When they pass it, the oldest full tool results are replaced by their
summaries and ids, then the oldest history goes; the newest result and the
request are never dropped. A request that cannot fit at all is refused. Each
result is at most 6,000 characters as encoded; tools bound their own output, and
an oversized one becomes an error result rather than a cut value.

Browser history is untrusted. Tool and outcome entries replay as quoted
records on the assistant's side, never as `tool` messages. A user entry with no
answer after it replays as a quoted record of an interrupted request, so it can
never authorise a write again. Only results produced inside the current run are
native tool messages.

## Tools

Every tool has a name, a description, a flat argument model, a behaviour class
and a callable. The prompt marks each class.

| Class | Tools | Behaviour |
|---|---|---|
| read | `get_context`, `list_characters`, `get_character`, `query_library`, `read_messages`, `list_worlds`, `list_lorebook_entries`, `list_active_lore`, `list_personas`, `read_document`, `read_state`, `get_settings_summary` | Runs at once; changes nothing. |
| write | `set_pipeline_toggle`, `set_lorebook_entry`, `set_world`, `set_length_guard`, `add_banned_phrase`, `create_lorebook_entry`, `create_character`, `create_world`, `create_persona`, `set_chat_world`, `checkpoint_chat`, `switch_branch`, `start_chat`, `set_chat`, `generate_image`, `generate_speech` | Commits at once. |
| confirm | `edit_character`, `edit_lorebook_entry`, `edit_persona`, `edit_message` | Stores a proposal and carries on; the user's Apply commits it after the run. |
| handoff | `regenerate_reply`, `rewrite_reply`, `continue_chat`, `open_chat` | Checks a target and ends the run; the chat's own stream, or for `open_chat` the browser, does the work. |

Reads project explicit allowlists, never whole rows: no avatars, extensions,
prompt overrides, workflow state, endpoints, models or credentials. The
projections are the privacy boundary; `query_library` reads allowlisted SQL
views with the library's read-only caps, and its rows may come from branches the
user left, which the result says; its `lorebook_entries` view spans every
world. Listings page with `offset`, report `total` and `truncated`, and clip
long text with a `…[+N chars]` marker. Naming one field, message, entry or
persona reads it whole, 4,000 characters a page. `read_messages` reads the
latest messages on the kept branch, those before `before_message_id` (each page
names the next one as `earlier_from`), or those containing `query`; it never
searches a branch the user left. `list_active_lore` lists the entries in effect
for a chat, overlays resolved as the prompt resolves them; nothing records which
of them a given reply drew on. `get_character` reads the alternate greetings
only whole, since an edit replaces the list whole; a list too long to send is
refused rather than cut.

### Writes

A write never runs on a retried or refused reply, nor once Stop has landed: the
abort is checked again, with no await before the write starts. Once admitted it
settles whole through a Stop or a dropped connection, because a cancelled await
proves nothing about whether SQLite committed. A `mutation` event names what
committed only after the commit. An identical non-idempotent write is not made
twice in one request; the refusal tells the model it already happened. Toggles,
setters, chat settings, chat-world choices and branch selection set an explicit
state and may repeat; each writes only the fields that differ from what is
stored, and a write that changes nothing commits nothing.

Creates go through each resource's own schema and writer with server-generated
ids, and a narrow text-only schema: no image, URL, extension or embedded book.
A create that matches an existing name still adds a new one. `start_chat` uses
the conversation route's own solo-chat writer, greeting and alternate greetings
included, and opens nothing. A setter's flag is `on`, `off` or `unchanged`, so
it can never flip a state it did not read. `set_length_guard` keeps the Length
Guard's own bounds (50 to 4,000 words, 1 to 20 paragraphs) in its argument model
and never switches the guard; `add_banned_phrase` adds a literal group and does
not add a phrase the bank already holds, and both results say when the switch
that applies them is off. Chat mutations take
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

## Reviewed edits

An edit tool reads and validates its target and writes nothing. Text edits are
`replace_all` or `find_replace`, where `find` must occur exactly once. Refused:
text carrying a read's clip marker, a no-op edit, and `replace_all` on a field
longer than 8,000 characters, which takes `find_replace` instead. The result and
the card both give the field's length before and after, so a large unintended
deletion shows at a glance.

A card's `tags` and `alternate_greetings` are list fields: an edit adds
`items` (`add_items`), removes them (`remove_items`) or makes them the whole new
list (`replace_all`), and the proposal holds the complete list either way. Apply
compares the stored list as decoded JSON. An item carrying a read's clip marker
is refused, as text is.

A run holds one proposal per target and field. A second edit to a field already
proposed in the run is made on that proposal's pending text, not the stored
text, and updates it in place: `before` stays the stored text, `after`
accumulates, and the id is unchanged. Its result says the edit stacked, and that
reads still show the stored text until the user applies. An edit that would
return the field to its stored text is refused. The prompt tells the model never
to run a step that depends on an edit it proposed; the loop does not enforce it.

### The proposal store

Proposals live in process memory, at most 32, each for 15 minutes from its last
change; a restart forgets them. Eviction takes proposals not yet applied first.
A proposal holds its canonical target, the one field it changes, and the exact
before and after texts. Message macros resolve once, when the proposal is made,
so Apply commits the text the preview showed.

```text
POST /api/assistant/apply  {proposal_ids: [...]}
→ 200 {results: [{proposal_id, verdict, summary?, message?, resources?, repeated?}]}
```

Apply names proposals and nothing else. Each is compared and written on its own,
under its own lock, in one transaction through its target's write path:
messages keep greeting-template, Writer-draft and changeset invalidation under
the chat's lane; cards keep their chats in step; lore entries keep their
revision. The verdicts are `applied`, `stale` (the text changed after the
preview), `missing` (the target is gone), `busy` (the chat is generating or
being deleted; the one verdict worth retrying) and `expired` (unknown to the
server). One stale proposal never stops the rest.

The result is kept the moment the guarded write returns, and a repeated Apply
returns it (`repeated: true`) without comparing again; concurrent Applies wait
for the first. A card's chat sync runs in its own commit after the write, and a
repeated Apply of a character proposal runs it again, so a sync that failed once
is retried. A write that commits and then raises is answered `stale` on the next
Apply; the browser's refresh shows the new text.

## Handoffs

`regenerate_reply` and `rewrite_reply` check that the reply is on the chat's
kept branch and the chat is idle. `continue_chat` checks that the chat is idle
and names its last message: an unanswered user message is answered, and in a
group a member named by `speaker_member_id` (a member id from `get_context`'s
cast, or a name only one member has) takes a turn; a solo chat whose last message is a reply has nothing to continue. Each
ends the run with `handoff`. Once the assistant's stream has settled, and never
after its Stop, the browser starts the chat's own Regenerate, Magic Rewrite,
continue or speak with the captured chat, message and direction or speaker. It
starts only while that chat is the one open and its target unmoved: the reply
still on its branch, or the last message still the one checked. Otherwise the
transcript says why and nothing switches. From then the chat's stream owns Stop
and the reply.

`open_chat` is the one handoff that navigates. Its description limits it to a
request that asks to open a chat. The browser opens the chat as the chat list
does, only while the reader is still on the view the request was sent from and
not in Document mode, so a run that ends after they moved on never pulls them
back. The transcript draws the opened chat as a view separator.

## Media

`generate_image` and `generate_speech` make an artifact for one saved reply on
a chat's kept branch. They are listed whenever their workflow (`image_gen`,
`tts`) is registered with an on-demand hook, and refuse while it is effectively
off: the refusal names the switch that is off, the workflow's own or the
`workflows` master, and says the assistant can turn it on if asked.

A media tool runs the workflow's hook through `run_on_demand`, the same
dispatch, enablement gate, workflow-state and character-state locks, character
resolution, context and failure mapping as
`POST /conversations/{cid}/workflows/{wid}/trigger`. The body is the plug-in's
own on-demand contract (`{action, message_id}`), and the result is read from
what the hook returns: `attachment_id` or `error`, or a stream of
`phase_status`, `<wid>_render`, `<wid>_error` and `<wid>_done` events. Nothing
imports a plug-in.

The dispatch and any lazy stream it returns run inside one workflow job,
`start_workflow_job(cid, …, job="assistant:<operation_id>", message_id=mid)`,
until the stream settles. That job is linked to the run:

- the assistant's Stop (or a dropped connection) cancels the jobs filed under
  its run and waits for them to end; a render the user started from the
  message is left alone;
- the chat's own Stop does not end it, as it does not end any render;
- deleting the message or the chat finds it by source, settles it, then
  deletes, as for any media job.

A media tool is a write: it settles whole and reports progress, each hook
`phase_status` label becoming a `progress` event. Its result names the
attachment ids its own job reported saving, never bytes. A job that started and
saved nothing is a tool error. A job stopped or cut short that reported nothing
while new rows appeared on the message claims none of them: the error says an
artifact may have been saved and to check the message. Because the hold on the
assistant lane is server-wide, no other assistant request runs during a long
render.

## Events

| Event | Payload | Meaning |
|---|---|---|
| `start` | `{}` | The run was admitted. |
| `progress` | `{label}` | What the run is doing now, or a media job's phase. Not stored. |
| `assistant_message` | `{text, final}` | A step's note (`final: false`) or the answer. |
| `tool_start` | `{call_id, tool}` | A tool is about to run. |
| `mutation` | `{call_id, tool, resources}` | A write committed these resources. |
| `tool_result` | `{call_id, tool, ok, summary, ids}` | Its outcome, paired by `call_id`. |
| `confirm` | `{call_id, tool, proposal}` | A proposal's preview; a later one with the same `proposal_id` replaces it. |
| `handoff` | `{call_id, tool, handoff}` | The checked chat target to start. |
| `done` | `{reason, message}` | Terminal: `reply`, `stopped`, `limit` or `handoff`. An early end names the changes already committed. |
| `error` | Failure | Terminal: an unusable model reply, a request that cannot fit, or a provider failure. |

EOF without `done` or `error` is a lost connection, never an answer.

## The browser

`#assistant-panel` is the third utility panel in `panels.js`, beside Tools and
the Inspector, and opening any one of them closes the others. Five controls
toggle it through the one `assistant:toggle` action: the chat header's
Assistant button, its twin in the document header, the document header's
narrow-layout icon, the narrow layout's header actions menu, and the lorebook
drawer's header. The panel stays open across chat, document and library views.
It shares the Inspector's width (`--inspector-width`) and `ui_sizing.js`
region, so dragging or scaling any right panel sizes all three. On the narrow
layout it is a slide-over like the other panels: a tap beside it or Back closes
it, and so does a handoff that starts, since the chat writes its reply under the
panel. Opening it focuses the input on the wide layout only, where no keyboard
rises.

A run and its card outlive a closed panel. Every control that opens it marks a
run in progress and counts the edits waiting for Apply; the marks are painted
from attributes on `#app`, so a control rendered later shows them too.

The transcript lives in `localStorage` under `orb-assistant-transcript`, at most
100 entries, and whether the panel is open under `orb-assistant-open`, which is
restored at load on the wide layout only. Loading validates every entry from its
known fields; malformed JSON is an empty transcript, and a full or blocked store
leaves the assistant working from memory. What is stored is user text, answers,
tool summaries and ids, and each proposed edit's labels, lengths and state,
never raw results, progress, live operations or proposal texts and ids. A tool
still waiting when the page closes, or when the run ends, reads "No result was
received; check before asking again."

The edits a run proposed become one card at the end of the run, however it
ended: proposing wrote nothing, so showing them is safe. The card groups the
edits by what they change, shows each field's diff and lengths, and has one
Apply and one Cancel. A diff is shown whole, its unchanged text shortened to the
context around each change; in a panel wide enough it is two columns, the text
now and the text proposed. A list field shows the items it removes and adds and
how many it keeps. Apply sends the ids still pending and marks each edit with
its verdict; a `busy` edit stays pending, so Apply stays and sends only those,
and a partly stale card says which were skipped. An Apply that ends without
verdicts leaves the card pending with a note to try again. Only the card of the
latest run can be applied: a new request closes it, and a reloaded card is
historical. A card with an edit not applied offers Ask again, which puts the
request back in the input.

The composer is one box: the input grows with its text, the view label the
next request is about sits under it, and the corner button sends, or stops
while a run goes. An empty transcript offers a few requests suited to the view;
picking one fills the input and sends nothing. Each request captures its view at Send; changing
view affects the next request, not a running one. The transcript shows a separator where the view changes,
and replays the view label with each request. Clear is disabled while work runs.
Send uses `tabLock.js`'s send permission. Assistant prose renders through the
message renderer inside `.msg-body`, where no element can name an action;
proposal diffs, user text and tool lines are escaped.

Committed resources are read again by id through the loaders that own them,
as each `mutation` arrives and after an Apply: worlds and entries, the settings
keys a toggle wrote (through the settings store's queue), characters, personas,
the chat list, a chat's worlds, a branch change, and a message or media job's
chat. A chat repaints only while it is the one in view and its view token
unchanged; nothing opens a chat or moves focus. A lore entry open in the drawer
takes a change made elsewhere field by field. When a run ends any way other than
`reply` or `handoff`, or any tool result in it came back failed, the browser
reads again everything cheap once the run has settled: worlds, characters,
personas, the chat list, the open chat's messages, and every switch key a toggle
can write. Nothing is sent again.
