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
| `backend/features/assistant/` | The facade, contracts, the model loop and its prompt, and the conversation the server holds and how an edited request takes it back (`session.py`). It imports no API, pipeline, workflow or peer feature code. |
| `backend/features/assistant/tools/` | The tools over the library's readers and writers, the proposal store, and Apply, Undo and Revise. `__init__.py` holds `build_tools`, which assembles them for one run against a host. |
| `backend/features/assistant/tools/host.py` | What the tools take from the app above them, as protocols the API side implements: `ChatGuards` (a chat's lane and deletion fence), `Writes` (route schemas, the phrase group check, the fragment write rules, chat starts and checkpoints), `Deletes` (Apply's fenced deletes), `AppReads` (context size, scene state, the Judge's readiness, a decision's problems), `WorkflowSwitches` (installed workflows and their switches) and `MediaJobs` (a media workflow's on-demand render as one job of the run). A host refusal reaches a tool as `HostRefusal`, a refused chat admission as `ChatUnavailable`. |
| `backend/features/assistant/tools/targets.py` | Bounded ids and labels, and the checks that resolve a tool's chat, character or reply from its arguments and the view |
| `backend/features/assistant/tools/reads.py` | The sixteen read tools, their projections and paging |
| `backend/features/assistant/tools/toggles.py` | The switch table, `set_pipeline_toggle`, and the switch summary |
| `backend/features/assistant/tools/setters.py` | A lore entry's switches and keywords, a world's name and flags, the Length Guard's limits, the active persona, the phrase bank |
| `backend/features/assistant/tools/fragments.py` | The fragment creates and setters, the stage each fragment type runs in and where Required applies, and the names, projections and checks the fragment reads, edits and deletes share |
| `backend/features/assistant/tools/edits.py`, `deletes.py`, `proposals.py` | The six edit tools; the ten delete tools; the proposal store, Apply, Undo and Revise |
| `backend/features/assistant/tools/actions.py`, `media.py` | Creates, chat starts and settings, chat actions and handoffs; the media tools and what counts as a saved render |
| `backend/api/assistant/` | The binding: `host.py` implements the host over the API, pipeline, workflow and peer-feature code; `__init__.py` binds `build_tools`, Apply, Undo and Revise to it |
| `backend/api/assistant/media.py` | A media render as a workflow job: the on-demand dispatch, its event stream, Stop |
| `backend/api/conversation_start.py` | The solo-chat writer the conversation route and `start_chat` share |
| `backend/api/deletions.py` | The chat, message and document deletes their routes and Apply share, with their fences |
| `backend/api/fragments/` | The interactive fragment write rules the fragment routes and the assistant share: type-scoped settings, defaults and the decision check |
| `backend/api/workflow_dispatch.py` | On-demand workflow dispatch, shared with the trigger route |
| `backend/api/routes/assistant.py` | `POST /api/assistant/run`, `/stop`, `/apply`, `/undo` and `/revise`, and the view check |
| `frontend/assistant_view.js` (layer 1) | Transcript validation, stream-event handling, Apply verdicts and markup; no DOM lookups or requests |
| `frontend/assistant_chat.js` (layer 5) | The panel, its run, Stop, Apply, handoffs and refreshes |

Tools are built per run, bound to the view captured at Send. The assistant's
tools are not in `prompting/tool_catalog.py` and never reach a turn.

## A request

```text
POST /api/assistant/run?operation_id=<id>
  { text, session, request_id?, edit?,
    context: {mode, conversation_id?, character_id?, document_id?, world_id?, entry_id?},
    reasoning?, pending? }
```

`text` is the new request, at most 8,000 characters, and nothing else of the
transcript is sent: the server holds the conversation `session` names (see
[The conversation](#the-conversation)). `context` is the view captured at Send.
The server checks every id and how they relate before the model is called: a
chat view names its chat, a document view names its document and nothing else,
a character must be the chat's own or a member of the group. A group view never
infers a character; tools that need one ask for it.

The lorebook drawer opens over any of these views, so its open world and the
saved entry selected on its Entries tab ride with whichever view is behind it
(`world_id`, `entry_id`); an entry is named only with its own world. The view
note names both, and every tool that takes a
`world_id` or an `entry_id` uses them when the argument is left empty. An entry
named without a world is found in its own world.

`request_id` is the browser's name for the request. With `edit` it names one
already sent, whose place this text takes; see
[An edited request](#an-edited-request).

`pending` names the proposals earlier requests made that the user's cards still
hold. The server keeps those still held and not applied, and ignores the rest;
see [Across requests](#across-requests).

The run streams through `sse_stream` under the reserved key `assistant:run`,
which no conversation id can spell. That makes one run at a time server-wide; a
second run, from another tab, gets `Another generation is already running`.
`POST /api/assistant/stop?operation_id=<id>` aborts only the run that
registered that operation id and waits for it to settle, as a chat's Stop does
(see [Stop, disconnect, and errors](sse-stream.md#stop-disconnect-and-errors)).
The browser registers the run in `S.operations` with wire-style target keys
(`conversation_id`), so a run never appears in a chat's delete dialog.

## The model protocol

Calls go to the Agent lane, with its model and `max_tokens`. Reasoning is off
unless the request sets `reasoning`, which the panel's Think box sends; then
every call of the run thinks, its reasoning replays under the names it streamed
(see [Reasoning can create another lane](kv-cache.md#reasoning-can-create-another-lane)), and the call asks the template to
`preserve_thinking`, so earlier turns keep their reasoning in the rendered
prompt rather than losing it once a newer user message follows. A run picks
one of two protocols when it starts and keeps it:

- **Single call**, where the endpoint sends native tool schemas and honours
  forced tool choice. Each step is one call with `tool_choice: required` over
  every tool's schema plus a `reply` tool, whose `message` is the final answer.
  The model names the tool and its arguments together; a zero-argument tool is
  called with `{}`.
- **Two calls**, everywhere else: structured output and text completion send no
  schemas to choose among, and an endpoint that ignores forced choice may
  ignore `required` too. Each step is two forced calls:
  1. `assistant_step` with `{tool, message}`. `tool` is an enum of the run's
     tools plus `reply`; `message` is a short note to the user, or with `reply`
     the final answer.
  2. For a tool that takes arguments, that tool's own flat schema. A
     zero-argument tool skips this call.

  Where no schemas go on the wire (structured output, text completion), the
  step is decoded under a JSON grammar, which ends a string at the first bare
  `"` the model writes. There `reply` leaves `message` empty and the final
  answer is one more call with no schema and `tool_choice: none`, read from its
  content. On Gemma 4 31B through NanoGPT, 3 of 32 answers written inside
  `message` ended mid-sentence at a quote with a clean finish, and 8 of 16
  thinking-on steps padded whitespace to the token limit; with the plain
  answer call, 0 of 32 steps and 0 of 32 answers did either.

  On an endpoint that does not honour forced tool choice, each call is sent
  only the schema it forces.

The tool runs and its bounded result is appended. Every call has its
own id and exactly one result, including a step call before its argument call.
`run_assistant(single_call=...)` forces either protocol; the benchmark
(`scripts/bench/assistant_bench.py`) uses it to compare them. On Gemma 4 31B
with native tools, single call took 2.5 model calls a request against 4.0 and
8.4 s median against 13.5 s, and chose and filled tools more accurately.

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
executions are spent the model gets one more step to reply. The same tool
called with the same arguments three times in a row ends the run with `limit`
before the third executes: the model is not taking in the results.

### The conversation

The assistant is prompted the way a coding agent is, with different tools. The
system prompt is stable for a given tool set and protocol, and each tool is
described in exactly one place. Under two calls the system prompt lists the
tools, with their signatures, markers (`[changes]`, `[proposes]`,
`[hands off]`) and descriptions, since a text-completion or structured-output
endpoint is sent no tool list; each schema's own description only points
there. Under single call the schemas are always on the wire, so each schema
carries its tool's marker and description and the system prompt lists nothing.

After it comes one conversation that only grows: each request with its view
note after the user's own words, then every call that request made and every
result, whole, as native tool messages, then its answer. The next request is
appended to that. Nothing is shortened, summarized, replaced by a pointer or
read again on the model's behalf between requests: a result the model was shown
once stays where it was, so the model never spends calls finding it again, and
the server's prefix cache holds through the whole conversation (see
[Keep the assistant off the turn's prefix](kv-cache.md#keep-the-assistant-off-the-turns-prefix)).
A result shows what was true when it ran; the prompt says the user may have
changed things since, and the model reads again when a request depends on it.

The server holds the conversation, in process memory, one at a time, under the
`session` id the browser made for it (`session.py`). The browser sends only the
new request, so nothing it stores can be replayed as something the model said
or a tool returned. `start` says whether the request extended the held
conversation (`continued`). It does not after a restart or when another browser
ran in between; the request then starts a conversation of its own, and the
browser marks where the model's memory begins.

However a run ends, the conversation keeps it. A reply ends with its call and a
result closing it, or with the plain answer; a handoff ends at the handoff's
result. A run that was stopped, hit a limit or failed keeps what it did: a call
cut off before its result is answered "no result was received; it may have
happened", and one line on the assistant's side says how the request ended, so
the next request is never read as part of an unanswered one. The prompt allows
a `[changes]` tool only for what the latest request asks.

Each result is at most 6,000 characters as encoded; tools bound their own
output, and an oversized one becomes an error result rather than a cut value.
The conversation, excluding the system prompt and tool schemas, is held to
80,000 characters: a fixed size, since Orb has no setting for an endpoint's
context window. One request can gather at most twelve results, so it always
fits. Only past the budget does anything go: the oldest requests, whole, until
the conversation is a quarter under it, so the cut that rewrites the prefix is
made rarely. A request that cannot fit even alone ends with `limit`.

### An edited request

The reader can reword any request of the log: Edit opens its text in a box, and
Send there sends it again with `edit` and the `request_id` it was first sent
with. The conversation goes back to where that request began, in the log and on
the server, and the new wording extends it from there. The server holds the
shorter conversation as soon as the run is admitted, so it stays the one the
log shows even if the run never reaches the model. The earlier requests are
sent byte for byte as before, so the prefix cache holds up to the edit.

Only the conversation goes back. What the removed requests did is not undone:
a write stays committed, an applied edit stays applied, a reply the chat
generated stays in the chat. The server keeps, with each request's id, the one
line each of its writes committed, and the edited request carries a note
listing the lines of the requests it removed; those are the only changes the
model can no longer see being made. An edit that removed no write carries no
note. The removed cards go with their entries: a change still waiting there is
discarded, as Discard does, and an applied one can no longer be undone from
the panel. The edit box says so before Send.

The chat may have moved on since the request was first sent: new turns, a
regenerated reply, another branch. An edit handles that as any later request
does, and adds nothing for it. It is about the view open now, not the one the
first wording was sent from; its tools read the chat's kept branch as it now
stands; a `#N` it names is the one the chat window shows now; and results still
in the conversation are covered by the standing rule that a result shows what
was true when it ran. The assistant's conversation has no branches of its own:
an edit replaces, and the chat's own branches are where a reply is recovered.

A request the server no longer holds (a restart, another browser, or one the
budget dropped) leaves nothing to go back to. The edit then starts a
conversation of its own, `start` answers `continued: false`, and the log marks
where the model's memory begins, as for any request.

A request with thinking off sends none of the conversation's reasoning back;
with thinking on it sends all of it, unless the model or endpoint has changed,
since reasoning one provider signed is not valid on another. Reasoning never
reaches the browser.

The conversation is about one character at a time. When the reader turns to
another character (a solo chat's or the library's; a group counts as one of its
own), the browser clears the transcript and starts a new session, as Clear
does. A view with no character, a document or the bare library, changes
nothing, and a swap made while a run goes takes effect once it has settled.

## Tools

Every tool has a name, a description, a flat argument model, a behaviour class
and a callable. The model's integer bounds and list sizes go out in the schema
as `minimum`, `maximum`, `minItems` and `maxItems`; string lengths stay out, since a
grammar-constrained endpoint would spell them out character by character, and
the Anthropic route drops every bound because strict tool use rejects them. The
prompt marks each class. Names are a verb and what it acts
on: `read_` one thing, `list_` several, `set_` an explicit state, `create_`,
`edit_` (proposed) and `delete_` (proposed). A lore entry is `lore_entry`
throughout.

| Class | Tools | Behaviour |
|---|---|---|
| read | `read_context`, `list_characters`, `read_character`, `query_library`, `read_messages`, `list_worlds`, `list_lore_entries`, `list_active_lore_entries`, `list_personas`, `list_documents`, `read_document`, `read_chat_state`, `read_settings`, `list_banned_phrases`, `list_fragments`, `read_fragment` | Runs at once; changes nothing. |
| write | `set_pipeline_toggle`, `set_lore_entry`, `set_world`, `set_length_guard`, `set_active_persona`, `add_banned_phrase`, `create_mood_fragment`, `create_interactive_fragment`, `create_decision_fragment`, `set_mood_fragment`, `set_interactive_fragment`, `set_decision_fragment`, `create_lore_entry`, `create_character`, `create_world`, `create_persona`, `set_chat_world`, `checkpoint_chat`, `switch_branch`, `start_chat`, `set_chat`, `generate_image`, `generate_speech` | Commits at once. |
| confirm | `edit_character`, `edit_lore_entry`, `edit_persona`, `edit_message`, `edit_mood_fragment`, `edit_interactive_fragment`, `delete_character`, `delete_chat`, `delete_message`, `delete_world`, `delete_lore_entry`, `delete_persona`, `delete_banned_phrase`, `delete_document`, `delete_mood_fragment`, `delete_interactive_fragment` | Stores a proposal and carries on; the user's Apply commits it after the run. |
| handoff | `regenerate_reply`, `rewrite_reply`, `continue_chat`, `send_as_user`, `open_chat` | Checks a target and ends the run; the chat's own stream, or for `open_chat` the browser, does the work. |

### Results

A result is what the model reads back, encoded as compact JSON, and it carries
only what the call and the request do not already say:

- No echo of an argument, of an id the call named, or of anything the view note
  states. A result names its target by its name (`"world": "Harbour"`), so a
  default taken from the view is visible; `read_context` adds to the view note
  and repeats none of it.
- Paging keys appear only when there is more: `truncated` and `next_offset` (or
  `earlier_from`) on a cut page, `text_offset` and `total_chars` on a paged
  text. A row leaves out an empty text or list. State flags are always present.
- A listing is a row among many: `list_fragments` gives a decision's question
  and outcome names, and `read_fragment` its situation and outcome texts.
- A write answers with what the server decided: new ids, `changed`, a switch's
  resulting state. It does not send back text the model wrote.
- A proposal answers with what it proposes and the lengths before and after; the
  proposal id goes to the browser only.
- A note is conditional: it reports something about this call's outcome. What
  holds for every call of a tool is in that tool's description.

The `summary` and `ids` of a result go to the browser's transcript.

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
searches a branch the user left. Each message carries its `number`, the `#N`
the chat window shows, and every summary naming a message uses it rather than
the id; `number` reads one message by its `#N`, so a request that names one
never pages to find its id. `list_active_lore_entries` lists the entries in effect
for a chat, overlays resolved as the prompt resolves them; nothing records which
of them a given reply drew on. `read_character` reads the alternate greetings
only whole, since an edit replaces the list whole; a list too long to send is
refused rather than cut.

`list_fragments` lists mood fragments by label, then interactive fragments
stage by stage (judge, director, state, editor), each with its `position`, the
order it runs in within its stage. Field types go by the editor's names:
`single` and `list` are stored as `string` and `array`, and a `yes_no` decision
is stored as `noul`. A listing or a create says when a switch keeps fragments
from taking effect: the Director for moods and scene fields, the Agent for
state, feedback and post-processing, a Judge endpoint for decisions.
`read_fragment` reads one fragment with its texts fitted to one result, and a
decision's question, situation and outcomes, each outcome by its key (`true`
and `false`, an option's name, or a level from `0`); naming one text in `field`
reads it whole, in pages.

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
and never switches the guard; `set_lore_entry` sets secondary keywords together
with the entry's Selective flag, as the drawer does, so they always count or are
cleared together; `add_banned_phrase` adds a literal group and does
not add a phrase the bank already holds, and both results say when the switch
that applies them is off.

A fragment create derives its id from the label, numbered past any id taken
and, for an interactive fragment, past the reserved ids, and puts an
interactive fragment last in its stage. `create_decision_fragment` takes a
decision's outcomes as parallel lists: option names for a choice, then each
outcome's meaning and guidance, with guidance copied from the meanings when
left out, as the editor does. Interactive fragment writes go through
`checked_interactive_write` in `backend/api/fragments/`, as the routes do, so a
decision that would not run is refused with its problems, and state and gate
settings stay on their own types. `set_interactive_fragment` changes a type, but
never to or from a decision, whose definition another type would drop; it
clears Required where the new settings make it mean nothing, and refuses
Required on where it means nothing. `position` moves a fragment within its
stage, keeping the stage's own priority slots and making them distinct.
`set_decision_fragment` sets the resolution, threshold, minimum confidence and
inject, clearing or defaulting the threshold as the resolution needs. Chat mutations take
the chat's lane through `idle_chat_admission`, which refuses a chat being
deleted or generating, read their target again once admitted, and hold the lane
only for their own database work. `switch_branch` ends the run once it has
switched, since the chat then reads differently.

Pipeline toggles name a stored switch and the state wanted, never a flip. Each
writes only its own key, and workflow toggles go through `set_workflow_toggle`,
the route's own path with its runtime callbacks. `editor` maps to the Output
Auditor (on) or the Output Auditor and Length Guard (off). The result reports
each switch it set as it now stands, with the effective value beside one a
master overrides, and a master switch that blocks it is named, not changed. `read_settings` reports every switch under the name
`set_pipeline_toggle` takes for it.

## Reviewed changes

An edit or delete tool reads and validates its target and writes nothing. Text edits are
`replace_all`, `find_replace`, where `find` must occur exactly once, or
`replace_every`, which replaces each occurrence. A `find` that matches only
once its JSON string escapes are decoded is taken as that text, since a read
shows a field JSON-encoded. Refused:
text carrying a read's clip marker, a no-op edit, and `replace_all` on a field
longer than 8,000 characters, which takes `find_replace` instead. The result and
the card both give the field's length before and after, so a large unintended
deletion shows at a glance.

A fragment's texts are edited the same way. An interactive fragment's field
must belong to its type: a decision has no description, only post-processing has
a gate question. A decision's outcome text is named by its `outcome` key. Apply
compares that outcome's text with the preview's, then writes its decision column
with that one outcome changed, in one transaction with a comparison of the column
and the decision's type as Apply read them: another outcome edited since the
preview is kept, and this outcome edited since is `stale`. A decision text that would add a problem to
the definition is refused when it is proposed and when the user revises it.

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
return the field to its stored text is refused, and so is an edit identical to
one the run already proposed: a replacement that contains its own `find` would
otherwise apply twice, and a small model would repeat it until the run's budget
ran out. The prompt tells the model never
to run a step that depends on an edit it proposed; the loop does not enforce it.

### Across requests

A card stays open to Apply after its run, and the next request names its
pending proposals (`pending`). Their texts are the server's own: the request
carries a note listing each, with its pending text clipped like a read, so the
model can build on it, and the note says that anything not listed was applied,
cancelled or expired. A further edit to a field one of them holds is made on its
pending text into a new proposal that `replaces` it; the server forgets the
replaced one, and the browser marks its entry superseded. Where the stored text
has moved since that preview, which could then never apply, the new edit starts
from the stored text instead and its result says so.

### Deletes

A delete is never a write the run makes: it cannot be undone, so it is a
proposal the user applies. Its preview says what goes with it: a character's
chats (kept unless `delete_chats`, which deletes its solo chats and every group
it is in), a chat's messages, a message's other versions at that point and
everything after them on every branch, a world's entries, whether a persona is
the active one, a phrase group's variants, a document's saved length. The prompt
allows a delete only when the latest request asks for that thing to go, and
points at turning a lore entry off instead. A delete already pending for the same
target is refused; one with a different choice (`delete_chats`) replaces it.

Apply deletes through the app's own path and fences: the chat and message
deletes in `backend/api/deletions.py`, which their routes share, a lore entry
through the lorebook feature so a Dynamic World records it, a character through
the card's guarded delete. A chat or message delete is `busy` while that chat is
generating, where the chat list's own delete would stop the reply first. A
persona delete also clears the active persona when it named that one, as the
persona list does. Deleting a state fragment leaves the facts chats saved for it
in the Inspector's State tab, as the fragment list's own delete does, and its
preview says so.

### The proposal store

Proposals live in process memory, at most 32, each for 15 minutes from its last
change; a restart forgets them. Eviction takes proposals not yet applied first.
A proposal holds its canonical target, the one field it changes (or that it
deletes the target), and the exact before and after texts. Message macros resolve once, when the proposal is made,
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
server). One stale proposal never stops the rest. An applied delete's resource
carries `deleted: true`.

```text
POST /api/assistant/undo    {proposal_ids: [...]}   → 200 {results: [{proposal_id, verdict, summary?, message?, resources?}]}
POST /api/assistant/revise  {proposal_id, text}      → 200 {proposal_id, verdict, proposal?, message?}
```

Undo writes an applied edit's `before` back where the field still holds its
`after`, compared in one transaction as Apply is, and makes the proposal pending
again: `undone`, or `stale` when the text moved on, `refused` for a delete or an
edit not applied, and `missing`, `busy` or `expired` as for Apply. It lasts as
long as the proposal: 15 minutes from the Apply, in this process.

Revise is the one place the browser sends text: the user's own replacement for
what a pending text edit writes, never a target. It answers the new preview
(`revised`), or `refused` (applied, a list field, a delete, an empty required
field, or the stored text unchanged) or `expired`. A message's macros resolve as
they do when proposed.

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
group a member named by `speaker_member_id` (a member id from `read_context`'s
cast, or a name only one member has) takes a turn; a solo chat whose last message is a reply has nothing to continue,
since its character never replies twice in a row. `send_as_user` takes the
user's next message, written by the model, and checks that the chat is idle
and its last message is a reply. Each is refused when the chat is not the one
the request was sent from, or the view is a document: the browser could not
start it. Each ends the run with `handoff`. Once the
assistant's stream has settled, and never after its Stop, the browser starts
the chat's own Regenerate, Magic Rewrite, continue, speak or Send with the
captured chat, message and direction, speaker or text; that Send leaves the
box and its attachments alone. It starts only while that chat is the one open
and its target unmoved: the reply still on its branch, or the last message
still the one checked. Otherwise the
transcript says why and nothing switches. From then the chat's stream owns Stop
and the reply.

`open_chat` is the one handoff that navigates. The browser opens the chat as the chat list
does, only while the reader is still on the view the request was sent from and
not in Document mode, so a run that ends after they moved on never pulls them
back. The transcript draws the opened chat as a view separator.

## Media

`generate_image` and `generate_speech` make an artifact for one saved reply on
a chat's kept branch. They are listed whenever their workflow (`image_gen`,
`tts`) is registered with an on-demand hook, and refuse while it is effectively
off: the refusal names the switch that is off, the workflow's own or the
`workflows` master, and says the assistant can turn it on if asked.

`generate_image` takes an optional `direction`: what the user wants this image
to show, beside the reply it is made of. The image workflow's composer follows
it over the reply on the moment, framing, focus, pose, mood and lighting, keeping
each subject's identity from the story, and each review of a render holds the
render to the direction first, so a departure the user asked for is never
corrected back toward the reply. The render records it in its metadata. Without
one the image is composed from the reply alone, as the reply's image button
does; a Regenerate from the image composes from the reply again.

A media tool runs the workflow's hook through `run_on_demand`, the same
dispatch, enablement gate, workflow-state and character-state locks, character
resolution, context and failure mapping as
`POST /conversations/{cid}/workflows/{wid}/trigger`. The body is the plug-in's
own on-demand contract (`{action, message_id}`, and for an image an optional
`direction`), and the result is read from
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
| `start` | `{continued}` | The run was admitted. `continued` is false when the server held no conversation under the request's `session`. |
| `progress` | `{label}` | What the run is doing now, or a media job's phase. Not stored. |
| `assistant_message` | `{text, final}` | A step's note (`final: false`) or the answer. |
| `tool_start` | `{call_id, tool}` | A tool is about to run. |
| `mutation` | `{call_id, tool, resources}` | A write committed these resources. |
| `tool_result` | `{call_id, tool, ok, summary, ids}` | Its outcome, paired by `call_id`. |
| `confirm` | `{call_id, tool, proposal}` | A proposal's preview (`action` is `edit` or `delete`); a later one with the same `proposal_id` replaces it, and one naming an earlier request's proposal in `replaces` supersedes that one. |
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

A run and its cards outlive a closed panel. Every control that opens it marks a
run in progress and counts the changes waiting for Apply; the marks are painted
from attributes on `#app`, so a control rendered later shows them too.

The transcript the reader sees lives in `localStorage` under
`orb-assistant-transcript`, at most 100 entries, the session id under
`orb-assistant-session`, whether the panel is open under `orb-assistant-open`, which is
restored at load on the wide layout only, and the Think box under
`orb-assistant-think`. Loading validates every entry from its
known fields; malformed JSON is an empty transcript, and a full or blocked store
leaves the assistant working from memory. What is stored is user text, answers,
tool summaries and ids, and each proposed edit's labels, lengths and state,
never raw results, progress, live operations or proposal texts and ids. The
stored transcript is the reader's record only; none of it is sent back. Clear
empties it and starts a new session. A tool
still waiting when the page closes, or when the run ends, reads "No result was
received; check before asking again."

The changes a run proposed become one card at the end of the run, however it
ended: proposing wrote nothing, so showing them is safe. The card groups them by
what they change and shows each field's diff and lengths, or for a delete what
goes with it and that it cannot be undone. A diff is shown whole, its unchanged
text shortened to the context around each change; in a panel wide enough it is
two columns, the text now and the text proposed. A list field shows the items it
removes and adds and how many it keeps.

Each change has its own controls while this page holds its proposal id: a
pending one has Apply (Delete, for a delete) and Discard, and a text edit has
Edit, which opens the proposed text in a box; Save sends it to `/revise` and the
diff then shows the user's own text. An applied edit has Undo, which makes it
pending again. A card with more than one pending change also has Apply all and
Cancel all. Apply sends the ids it names and marks each change with its verdict;
a `busy` change stays pending, and a partly stale card says which were skipped.
An Apply that ends without verdicts leaves the changes pending with a note to try
again. Discard and Cancel send nothing.

A card stays live across later requests: each request sends the ids of every
pending change on every card (`pending`), and a `confirm` naming one in
`replaces` marks that entry superseded. A reloaded card keeps no ids, so its
changes are historical. A card with a change not applied and none left pending
offers Ask again, which puts the request back in the input.

Each request has Edit beside its bubble, disabled while work runs. It opens the
request's text in place, one request at a time; Enter or Send sends it, Escape
or Cancel puts the request back. See [An edited request](#an-edited-request).

The composer is one box: the input grows with its text, the view label the
next request is about (with the lorebook drawer's world or entry after it, while
the drawer is open) sits under it beside the Think box, and the corner
button sends, or stops while a run goes. Think applies from the next request. An empty transcript offers a few requests suited to the view;
picking one fills the input and sends nothing. Each request captures its view at Send; changing
view affects the next request, not a running one. The transcript shows a separator where the view changes,
and where the model's memory begins when the server no longer held the conversation above. Clear is disabled while work runs.
Send uses `tabLock.js`'s send permission. Assistant prose renders through the
message renderer inside `.msg-body`, where no element can name an action;
proposal diffs, user text and tool lines are escaped.

Committed resources are read again by id through the loaders that own them,
as each `mutation` arrives and after an Apply: worlds and entries, the settings
keys a toggle wrote (through the settings store's queue), characters, personas,
the chat list, a chat's worlds, a branch change, the mood and interactive fragment lists (the
latter with the open chat's state), and a message or media job's chat. A deleted chat, document, world or character is let go of as the app's
own delete does, through helpers its module exports (`forgetConversation`,
`forgetDocument`, `forgetWorld`): the chat view empties, the editor clears or the
drawer closes when it is the one open, and an entry deleted while selected in the
drawer leaves its editor. A chat repaints only while it is the one in view and its view token
unchanged; nothing opens a chat or moves focus. A lore entry open in the drawer
takes a change made elsewhere field by field. When a run ends any way other than
`reply` or `handoff`, or any tool result in it came back failed, the browser
reads again everything cheap once the run has settled: worlds, characters,
personas, both fragment lists, the chat list, the open chat's messages, and every switch key a toggle
can write. Nothing is sent again.
