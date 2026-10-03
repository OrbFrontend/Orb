# How a Turn Streams from Backend to Browser

Orb sends a chat turn over **Server-Sent Events (SSE)**. The browser makes one
request; the backend keeps the connection open and sends events as the turn
runs.

This page describes the frontend/backend wire contract. For prompt construction
and cache reuse, see [KV Cache Reuse](kv-cache.md).

> **Animation:** [SSE stream animation](https://orbfrontend.github.io/Orb/architecture/sse-stream-animation.html)
> shows a complete turn and the main stop and error paths.

## One long-lived request

```text
browser ── POST /conversations/{cid}/send ──▶ backend
        ◀──── user_message_created
        ◀──── director_start / director_done
        ◀──── token (many)
        ◀──── writer_done / editor_done
        ◀──── done
```

The stream ends after `done`. Until then, all turn events use the same
connection.

## Frame format

Each frame is plain text:

```text
event: <name>
data: <payload>

```

The backend's `sse_stream` wrapper serializes dictionary data as one-line JSON
and escapes newlines in string data. Keepalive comments prevent an idle
connection from being dropped.

`frontend/sse.js` parses frames and yields `{event, data}`. It does not decide
what an event means or unescape the payload. `chat_stream.js` dispatches by event
name; other streaming features use the same parser with their own handlers.

Token paints are coalesced to one per animation frame. Each snapshot still goes
through the message sanitiser, but the live body patches compatible DOM nodes
in place and uses a stable CSS scope for that bubble. This keeps existing images,
controls and animations alive while text grows. Incomplete tags and style blocks
are held until complete; structural markup changes can still cause layout shifts.
Finalisation and editor rewrites render the authoritative complete body.

Expression Playback's optional expression-based rendering buffers turn text
instead of painting tokens or cosmetic rewrites. After the normal settlement
refetch, the frontend classifies sentence chunks of the saved replies and reveals
consecutive runs of the same resolved expression on click/Space. Group replies
share one ordered playback buffer. This display state never changes stored
message content or the SSE contract; classification failure reveals the full
saved text. While tokens arrive, settled sentences (any sentence with later text
after it) are classified one at a time into a per-turn cache keyed by sentence
text; settlement reuses those labels and classifies only what the saved text
changed. Nothing streamed is ever shown. Classic rendering retains the live
paint path.

The status bar describes the step that is running. `director_start` and
`step_start` mark where each core step begins, and while the turn streams a
workflow hook's `phase_status` label describes its own step. The text holds
until the next step starts, so the indicator follows stream events rather than
timers.

Only `token` is normally raw text. Other payloads are JSON, with `error` also
accepting a legacy string.

## Turn events

Events are conditional unless marked terminal. The frontend must tolerate a
pass being skipped.

| Event | Payload | Purpose |
|---|---|---|
| `user_message_created` | `{id, content}` | Replaces the optimistic user row with its saved id and text. `/send` only. |
| `director_start` | — | Starts the directing phase. |
| `decisions` | `{evaluations, skipped, cooldowns, inherited?}` | Publishes the resolved decision fragments, once per turn and once per group exchange, before the directing phase. Sent only when the turn had a decision to run or to report. `inherited: 1` marks a later speaker's regeneration that reused its exchange's committed result without asking. |
| `step_start` | `{step}` | Names the step that is starting: `judge`, `lorebook`, `state`, `writer`, `output_auditor`, `length_guard`, `post_processing`, `feedback`, `world_changes`, or `sheet_updates`. |
| `reasoning` | `{pass, delta}` | Adds thinking text to a pass's reasoning buffer. |
| `director_done` | Director data | Updates the inspector. |
| `token` | Text delta | Appends visible Writer output. |
| `writer_done` | `{editor_will_run}` | Ends the Writer phase. |
| `draft_update` | `{draft}` | Optional cosmetic Editor or Prose Rewriter progress update. The browser never keeps it as the reply. |
| `writer_rewrite` | `{refined_text}` | Replaces the visible draft after an Editor or workflow change. |
| `editor_done` | Editor data | Updates the inspector. |
| `feedback` | Feature data | Updates feature panels. |
| `state` | `{changes, rejected, dropped}` | This turn's state-fragment changes so far, the operations refused, and the carried corrections dropped. Sent after each state step that changed or refused something, and after carried corrections are applied; each payload replaces the previous one. Shown on the Inspector's Main tab; its State tab refetches after the stream. |
| `world_change_proposed` | `{message_id, changeset}` | Shows a pending Dynamic Worlds proposal. |
| `warning` | Warning data | Shows a non-terminal warning; the turn continues. |
| `error` | JSON object or string | Terminal failure. |
| `done` | — | Terminal success; the stream closes. |

`decisions` carries a projection, not the stored record: each evaluation has its
identity, `outcome`, `guidance`, `answer_source` and `probability`/`draw`, but not
the rendered classifier state, question, criteria or the authored output map. The
Inspector reads those in full from the reply's director-log route, which keeps a
16 KiB rendered state off a stream the client cannot skip. `skipped` entries carry
no outcome at all — a skipped decision resolved to nothing.

Each pass shares one reasoning buffer across repeated calls and sub-steps. The
backend inserts one blank line at each call boundary in both the stream and the
persisted text, so the frontend appends deltas verbatim.

Workflow events such as `phase_status` and `tts_autoplay` use the same stream.
See [Secondary Workflows](secondary-workflow.md).

## Group exchanges

A group request wraps those events in three group events:

| Event | Purpose |
|---|---|
| `speaking_plan` | Announces the speakers and their cues for the exchange. |
| `speaker_start` | Starts the bubble and context for one speaker. |
| `speaker_done` | Confirms that speaker's saved message. |

The ordinary turn events between `speaker_start` and `speaker_done` belong to
that speaker. There is still one request-level `done`. If a later speaker
fails, earlier saved replies remain; the final refetch reconciles the group
exchange.

## Persistence and reconciliation

The internal `_result` event carries the completed reply to the persistence
layer. It is consumed by `consume_pipeline` and never sent to the browser.
The internal `_turn_state` event, emitted just before the Writer starts, hands
persistence the turn's live working state and is consumed the same way. When a
turn fails or is cancelled before `_result`, persistence saves that state as a
finished turn would: the latest authoritative draft (the streamed Writer text,
or the Editor's latest draft) with the Director's moods, cooldowns, decisions,
state changes, and the Inspector log. The `error` event still ends the stream.
A turn with no reply text saves nothing, including the Director's moods.
Persistence happens before `done`, so the browser can trust the server when the
stream closes.

The reply is saved at most once. Once the `_result` save has started, a
cancellation waits for that same save and the fallback never runs; its INSERT
may already have committed. A save that fails is raised as a `saving the reply`
failure, which is reported as `error` even when the turn was stopped.

"Authoritative" advances only with finished work:

- the Editor's result grows with each completed patch batch, whole rewrite, or
  post-processing edit, never with a call that Stop cut short;
- a workflow's result grows with each whole replacement draft, complete
  artifact, or message state that it hands over. Its
  [`draft_update` previews](secondary-workflow.md#a-workflow-inside-a-turn)
  never become part of the result.

`afterStream()` then:

- refetches messages and Director state;
- finds this request's own saved reply: an assistant row that was not on screen
  when the request started (and, in a group, has the same exchange and
  speaker). The branch that a regeneration replaces is therefore never
  mistaken for its result;
- finalizes the streaming bubble with that row's id, or fully rerenders when
  there is none (or for a group exchange). This also reveals a
  hide-until-finished reply;
- redraws the Editor diff against the saved reply, or drops it;
- applies edits that were queued behind the conversation stream lock;
- clears phase indicators.

The stream is optimistic while it runs and authoritative after this refetch.
Reply text that the server did not confirm (because the refetch failed, the Stop
did not settle, or the turn failed) stays on screen as a row without an id. It
cannot be targeted by message actions until the next sync.

## Stop, disconnect, and errors

Stop and a client disconnect signal the same conversation abort token. The
backend stops upstream generation and persists any prose it has already
received. A per-conversation stream lock prevents two generations from running
at once.

Stop interrupts the whole model-call attempt, including connection setup,
waiting for response headers or an error body, and text-mode template
preparation. The pending step is cancelled and awaited, and its generator is
closed before the turn continues to persistence. Deltas already handed to the
turn remain; an interrupted call returns no assembled message or tool call.

Stop does not drop the connection. The browser posts `/stop` and keeps reading:

```text
browser ── POST /stop ─────────────▶ abort token set
        ◀──── writer_rewrite / ...    (stages wind down, nothing new starts)
        ◀──── done                    (after the reply is saved)
stream closes                          lock released
        ◀── /stop: {active, settled}
browser refetches
```

The backend records the stream that owns each conversation lock. `/stop` aborts
that stream, then waits (up to 15 s) until it has settled: the generator,
including its persistence, has finished and the lock has been released.
Its answer is `{ok, active, settled}`. `active: false` means that nothing was
registered when the request arrived, which does not prove that a request still
in flight can never start.

The browser drops the connection only when `/stop` failed, found nothing
active, or did not settle, or when the stream did not close shortly after
settlement. The server treats a dropped connection as Stop, and also checks for
one right after the stream registers, before any generation, so Stop sent ahead
of its request still stops it. After a drop, the browser posts `/stop` again
and refetches only after that response.

For streamed replies, the initial `/stop` request and settlement retry each
have a 20-second deadline, including the response body. A timeout aborts that
request and follows the same fallback as a failed request; a timed-out
settlement retry leaves the reply unconfirmed. EOF without
`done` or `error` is also a disconnect, so a truncated stream cannot claim that
the server finished saving. Its received prose stays visible without an id if
the refetch cannot confirm it.

When the stream completes normally, the browser cancels any pending `/stop`
request and reconciles immediately: the completed stream already confirms
persistence, so its HTTP response must not hold Send disabled until the deadline.

When the client goes away, the request is cancelled but the turn is not. The
turn still finishes stopping: the stream runs its generator to the end with
the token aborted, discards the events, and saves normally, for up to 10 s.
Only then is the pending step cancelled and the fallback save used. The lock
is held throughout, so a queued `/edit`, `/delete`, `/switch-branch`, or the
next turn starts only after that save.

After Stop, no new call or hook starts. A call that Stop cut short is
discarded. Stop does not interrupt the save. The stopped turn ends with `done`,
not `error`. A failure caused by cutting a call short is logged instead. A
failed save is still reported as `error`.

Once Stop is pressed, the browser freezes the bubble and repaints it from the
saved row after settlement. `stopConversation()` is also used outside chat
replies, for example by compression, and needs no bubble.

`error` is terminal. `warning` is optional work that declined and does not stop
the turn. An Editor call that fails is a `warning`: the reply keeps the best
draft the Editor reached, and the turn continues through workflows and the
after-reply steps. Workflow hooks may emit custom events, but names owned by the
core dispatcher or names beginning with `_` are reserved.

## Routes using the stream

`/send`, `/continue`, regenerate, fork-edit, super-regenerate, and Magic Rewrite
all use the same SSE wrapper and event vocabulary. This keeps one frontend
dispatcher responsible for generated turns.

`/prose-rewrite` is the exception. It rewrites an already-saved assistant row
without creating a message or branch, then passes the result through Format
Consistency when that workflow is enabled. It emits optional
`prose_rewrite_update` events and ends with `prose_rewrite_done`; its client loop
is separate from the turn dispatcher. The replacement is saved all at once, or
not at all. Its client follows the same Stop and settlement handshake, then
refetches the saved row. It shows that row whether or not the replacement
committed before Stop.

In one sentence: one request opens the stream, named events carry progress and
results, tokens carry the visible draft, internal events stay server-side, and
`done` is followed by a server refetch.

## Concurrent ownership and deletion

`S.operations` (`operations.js`) registers each running chat, document, media and
library operation before its first await. Per-chat buffers, reasoning, drafts,
queued edits, group speaker state and the Stop handle live in
`S.conversationStates`; the conversation keys on `S` read the chat in view. A
reply keeps streaming while the user browses or generates elsewhere; a background
completion reconciles its own chat and paints nothing. Selection bumps a view
token, and Send stays disabled until history loads.

Streams carry `?operation_id=`, and Stop sends the same id, so a stale Stop
answers `active: false, settled: true` without aborting a newer request.
`beginStream`/`streamEvents`/`settle` give document generation, summaries and
library scans the same settlement protocol as chat. A run is released once
settled, or once settlement is given up on; the server lane lock still refuses a
second run on the same resource.

Queued history edits save in order after the reply and before Send returns; a
failed one stays on its message with Retry and Discard. Media refreshes merge into
their origin chat and wait while a message is being edited.

Conversation, group and document deletion closes admission, stops registered work,
waits for it to settle and holds the turn lanes through the delete; a timeout
answers 409 and deletes nothing. Message deletion settles the media of the
subtrees it removes. Card deletion and duplicate resolution refuse busy chats and
fence them until commit. These in-memory fences assume one uvicorn process.
