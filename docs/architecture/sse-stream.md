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

The backend's `_sse_stream` wrapper serializes dictionary data as one-line JSON
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
| `draft_update` | `{draft}` | Optional cosmetic Editor or Prose Rewriter progress update. |
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
layer. It is consumed by `_consume_pipeline` and never sent to the browser.
The internal `_turn_state` event, emitted just before the Writer starts, hands
persistence the turn's live working state and is consumed the same way. When a
turn fails or is cancelled before `_result`, persistence saves that state as a
finished turn would: the latest authoritative draft (the streamed Writer text,
or the Editor's latest draft) with the Director's moods, cooldowns, decisions,
state changes, and the Inspector log. The `error` event still ends the stream.
A turn with no reply text saves nothing.
Persistence happens before `done`, so the browser can trust the server when the
stream closes.

`afterStream()` then:

- refetches messages and Director state;
- finalizes the streaming bubble, or fully rerenders a group exchange;
- applies edits that were queued behind the conversation stream lock;
- clears phase indicators.

The stream is optimistic while it runs and authoritative after this refetch.

## Stop, disconnect, and errors

Stop and a client disconnect signal the same conversation abort token. The
backend stops upstream generation and persists any prose it has already
received. A per-conversation stream lock prevents two generations from running
at once.

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
is separate from the turn dispatcher.

In one sentence: one request opens the stream, named events carry progress and
results, tokens carry the visible draft, internal events stay server-side, and
`done` is followed by a server refetch.
