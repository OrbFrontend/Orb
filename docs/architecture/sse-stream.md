# How a Turn Streams from Backend to Browser

This doc explains, in plain English, how Orb sends a single chat turn from the backend to the frontend over **Server-Sent Events (SSE)** — what events cross the wire, in what order, and what the browser does with each one.

Audience: someone who can read code but hasn't traced the chat stream end to end. The focus is the **contract between frontend and backend** — the events on the wire — not what happens *inside* the backend passes. For the internals (how the Director/Writer/Editor prompts are built and cached), see [KV Cache Reuse](kv-cache.md).

> **Animation:** [sse-stream-animation.html](https://orbfrontend.github.io/Orb/architecture/sse-stream-animation.html) is a stepped, self-contained walkthrough of one full turn on the wire — every event from the opening `POST` to the closing `done`, plus the stop/disconnect and error paths. Open it in a browser.

---

## 1. It's a stream, not a request/response

A turn is **not** a normal request that returns a body. The browser sends **one** `POST` and the backend never closes it — instead it holds the connection open and *pushes* events as the turn unfolds. That's SSE: a one-way stream of `text/event-stream` frames from server to client.

```
browser ──POST /conversations/{cid}/send──▶ backend
        ◀──── event: user_message_created ─┐
        ◀──── event: director_start ───────┤
        ◀──── event: director_done ────────┤  one long-lived
        ◀──── event: token (×N) ───────────┤  connection
        ◀──── event: writer_done ──────────┤
        ◀──── event: done ─────────────────┘
```

Everything for the turn travels down this single connection until the backend yields `done` and returns.

---

## 2. The frame format

The wire format is plain text. Each event is two lines plus a blank line:

```
event: <name>
data: <payload>
            ← blank line terminates the frame
```

On the backend, `handle_turn` is an `async` generator that `yield`s `{"event": ..., "data": ...}` dicts. The `_sse_stream` wrapper in `backend/api/deps.py` serialises each one:

- `data` that is a `dict` is JSON-encoded (single-line `json.dumps`).
- `data` that is a string has its newlines escaped to literal `\n` (so a multi-line frame can't break the parser).
- Silent stretches emit a `: keepalive\n\n` comment frame so proxies don't drop the connection.

Because string data is newline-escaped and dict data is single-line JSON, a payload **never contains a real newline** — which makes `\n\n` an unambiguous frame terminator and `\n` an unambiguous line terminator inside a frame.

On the frontend, one module parses this wire format for **every** streaming route: `sse.js`. Its `sseEvents(body, {signal})` async generator splits the byte stream into frames, skips keepalive comments, and yields `{event, data}` pairs — where `data` is the **raw** payload string. It is transport-only: it knows the frame shape and nothing about event names, and it **never un-escapes** `data` (a string channel's `\n` escaping and a JSON channel's raw payload are opposite rules, so un-escaping is the consumer's call via `unescapeSSE`). The chat dispatcher (`processSSEStream` → `handleSSEEvent` in `chat_stream.js`) consumes `sseEvents` and routes each pair through one big `switch` keyed on the **event name**; the conversation-summary and document-generate readers consume the same `sseEvents` with their own tiny event handling. The name is the entire contract; the payload just fills in detail.

---

## 3. The events, in the order they fire

A typical `/send` turn with reasoning on (Director + Writer), an Editor pass, and a TTS workflow attached produces this sequence. Every event is independent — **the frontend must tolerate any of them being absent.**

| # | Event | Dir | Data | What the frontend does |
|---|-------|-----|------|------------------------|
| 1 | `user_message_created` | BE→FE | `{ "id": 412 }` | Patches the optimistic user bubble (`id: null`) with the real DB id. `/send` only — `/continue` skips it. |
| 2 | `director_start` | BE→FE | *(none)* | Phase → **directing**; clears stale inspector data. |
| 3 | `reasoning` | BE→FE | `{ "pass": "director", "delta": "…" }` | Appends thinking tokens to the named pass's buffer; lights its dot. |
| 4 | `director_done` | BE→FE | `{ "tool_calls": [...], ... }` | Stores director data for the inspector; advances dot to Writer. |
| 5 | `token` (×N) | BE→FE | bare text `delta` | The visible reply. First token reveals the bubble + phase → **generating**; each one is appended and re-rendered. |
| 6 | `writer_done` | BE→FE | `{ "editor_will_run": true }` | Authoritative end-of-writer marker; phase → **refining** if an editor pass follows. |
| 7 | `writer_rewrite` | BE→FE | `{ "refined_text": "…" }` | *Optional.* Editor's patched prose; FE diffs vs. the draft and swaps the bubble. |
| 8 | `editor_done` | BE→FE | `{ "tool_calls": [...] }` | Merges editor tool calls into the inspector. |
| 9 | `feedback` | BE→FE | `{ "values": {...} }` | *Optional.* User-facing notes; display-only, re-renders the inspector. |
| 10 | `direction_notes` | BE→FE | `{ "notes": [...] }` | *Optional.* The Director's persistent notes recorded this turn; display-only, re-renders the inspector's Direction Notes block. |
| 11 | `phase_status`, `tts_autoplay`, … | BE→FE | varies | Secondary-workflow passthrough (see §6). |
| 12 | `done` | BE→FE | *(none)* | Terminal. Stream closes; FE runs `afterStream()`. |

The only event whose `data` is **not** JSON is `token` — it's a raw text delta, with newlines escaped to `\n` and un-escaped on arrival.

---

## 4. Two events that never reach the browser

### `_result` (internal)

The pipeline's last event is `_result`, carrying the fully assembled reply (final text, tool calls, reasoning). `_consume_pipeline` intercepts it, **persists the assistant message and the conversation log to disk**, and does *not* forward it. The underscore prefix marks it internal. The browser already has every token, so it doesn't need `_result` — but the backend needs it to commit the turn before the stream ends. (Other underscore-prefixed events like `_PipelineResult` follow the same convention.)

### Why the persist happens before `done`

`done` is yielded *after* `_result` has been persisted. So by the time the frontend sees end-of-stream, the turn is already durable on the server — which is exactly what lets `afterStream()` (§5) trust a refetch.

---

## 5. After the stream closes: `afterStream()`

The frontend renders **optimistically** during the stream (it shows tokens as they arrive, before anything is confirmed). Once the stream closes, `afterStream()` reconciles that optimistic UI against the server's truth:

- **Refetches** the message list and director state (`GET …/messages`, `GET …/director`).
- **Finalizes** the streaming bubble in place — stamping the real assistant `id` onto the DOM node, with no destroy/re-render flash.
- **Flushes queued edits.** A `/edit` issued mid-stream blocks on the per-conversation stream lock for the whole turn; `afterStream()` runs once the lock frees and persists them.
- **Clears** the phase chip and any lingering workflow pills.

This is plain request/response — the stream is over.

---

## 6. Workflows extend the protocol without touching the core

Secondary workflows `yield` their own SSE events (e.g. `phase_status` for a "Synthesizing…" pill, `tts_autoplay`, `workflow_attachments_rejected`, or custom ones). The orchestrator passes hook-emitted events straight through the stream. On the frontend, the `switch`'s `default` branch looks the event name up in `S.workflowEventHandlers` and dispatches there — so a workflow can add new events **without editing the core switch**. See [Secondary Workflows](secondary-workflow.md).

To prevent collisions, the orchestrator drops any hook attempt to emit a reserved internal event name (the underscore-prefixed ones).

---

## 7. Edge cases: stop, disconnect, error, concurrency

- **Stop.** Clicking Stop sends `POST …/stop`, which fires the conversation's `abort_token`. The backend breaks its LLM loop and closes the upstream connection cleanly — no task cancellation needed.
- **Disconnect.** If the user closes the tab without clicking Stop, a background watcher polling `request.is_disconnected()` trips the same `abort_token` as a backstop.
- **Partial persistence.** Whether the turn finishes, aborts, or errors, `_consume_pipeline`'s `finally` runs exactly once — persisting whatever prose streamed so far, so an interrupted turn is never lost.
- **Errors** arrive as a single `error` event whose `data` is a human-readable string; the frontend toasts it. There is exactly one error channel — no separate mid-stream HTTP status.
- **Concurrency.** A per-conversation lock (`_conversation_stream_locks`) allows only one active stream per conversation. A second concurrent `/send` gets an immediate `error` ("Another generation is already running") rather than racing.

---

## 8. Every generating route reuses this one stream

`handle_turn` is the entry point for `/send` and `/continue`, but regenerate, fork-edit, super-regenerate, and magic-rewrite all stream through the **same** `_sse_stream` wrapper and emit the **same** event vocabulary. That's why the frontend dispatcher is written once: learn the events here and you've learned every chat route.

The whole contract, in one sentence: **one `POST` opens an SSE stream; the backend pushes control (`user_message_created`, `done`, `error`), meta (`director_*`, `writer_done`, `writer_rewrite`, `editor_done`, `feedback`, `reasoning`, workflow events), and `token` events; the frontend routes each by name in one switch; underscore-prefixed events stay server-side.**
