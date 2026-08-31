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

Only `token` is normally raw text. Other payloads are JSON, with `error` also
accepting a legacy string.

## Turn events

Events are conditional unless marked terminal. The frontend must tolerate a
pass being skipped.

| Event | Payload | Purpose |
|---|---|---|
| `user_message_created` | `{id, content}` | Replaces the optimistic user row with its saved id and text. `/send` only. |
| `director_start` | — | Starts the directing phase. |
| `reasoning` | `{pass, delta}` | Adds thinking text to a pass's reasoning buffer. |
| `director_done` | Director data | Updates the inspector. |
| `token` | Text delta | Appends visible Writer output. |
| `writer_done` | `{editor_will_run}` | Ends the Writer phase. |
| `draft_update` | `{draft}` | Optional cosmetic Editor progress update. |
| `writer_rewrite` | `{refined_text}` | Replaces the visible draft. |
| `editor_done` | Editor data | Updates the inspector. |
| `feedback` / `direction_notes` | Feature data | Updates feature panels. |
| `world_change_proposed` | `{message_id, changeset}` | Shows a pending Dynamic Worlds proposal. |
| `warning` | Warning data | Shows a non-terminal warning; the turn continues. |
| `error` | JSON object or string | Terminal failure. |
| `done` | — | Terminal success; the stream closes. |

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
the turn. Workflow hooks may emit custom events, but names owned by the core
dispatcher or names beginning with `_` are reserved.

## Routes using the stream

`/send`, `/continue`, regenerate, fork-edit, super-regenerate, and Magic Rewrite
all use the same SSE wrapper and event vocabulary. This keeps one frontend
dispatcher responsible for generated turns.

`/prose-rewrite` is the exception. It rewrites an already-saved assistant row
without creating a message or branch. It emits optional `prose_rewrite_update`
events and ends with `prose_rewrite_done`; its client loop is separate from the
turn dispatcher.

In one sentence: one request opens the stream, named events carry progress and
results, tokens carry the visible draft, internal events stay server-side, and
`done` is followed by a server refetch.
