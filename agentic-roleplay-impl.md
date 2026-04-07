Here's my implementation plan based on the design document:

---

## Technical Implementation Plan

### Tech Stack

**Backend:** Python + FastAPI. Lightweight, async-native, and has clean OpenAPI docs for free. A single `uvicorn` process serves both the API and the static frontend files.

**Frontend:** React (single-page app). Communicates with the backend over REST and SSE (server-sent events for streaming token delivery).

**Persistence:** SQLite via `aiosqlite`. One database file inside the app directory. No external services. When you delete the folder, everything is gone.

**Directory structure:**

```
app/
├── backend/
│   ├── main.py              # FastAPI entry, mounts static frontend
│   ├── config.py             # Pydantic settings (DB path, defaults)
│   ├── database.py           # SQLite schema + migrations
│   ├── routers/
│   │   ├── chat.py           # Conversation endpoints (send, history, delete)
│   │   ├── settings.py       # User config CRUD (endpoint URL, hyperparams, system prompt)
│   │   ├── characters.py     # Character card CRUD
│   │   └── fragments.py      # Fragment library CRUD
│   ├── services/
│   │   ├── orchestrator.py   # The two-pass pipeline (core logic)
│   │   ├── agent.py          # Agent pass: build prompt, parse tool calls
│   │   ├── writer.py      # Writer pass: assemble injection + generate
│   │   ├── llm_client.py     # OpenAI-compatible HTTP client
│   │   └── state.py          # Behavior registry + scene note store
│   └── models/
│       ├── schemas.py        # Pydantic models for API request/response
│       └── db_models.py      # SQLite table definitions
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── components/
│   │   │   ├── ChatPanel.jsx          # Message list + input
│   │   │   ├── MessageBubble.jsx      # Single message rendering (markdown)
│   │   │   ├── SettingsDrawer.jsx     # Endpoint URL, hyperparams, system prompt
│   │   │   ├── CharacterManager.jsx   # Character card CRUD + selection
│   │   │   ├── FragmentManager.jsx    # View/edit/create style fragments
│   │   │   └── DirectorInspector.jsx  # Debug panel: shows agent output, active styles, notes
│   │   ├── hooks/
│   │   │   ├── useChat.js             # Send message, receive SSE stream
│   │   │   └── useSettings.js         # Settings state + persistence
│   │   └── lib/
│   │       └── api.js                 # Fetch wrapper for backend routes
│   └── index.html
├── data/
│   └── app.db                # SQLite (created at first run)
└── run.sh                    # pip install + uvicorn start
```

---

### Database Schema (SQLite)

**`settings`** — Single-row table. Stores the active user config.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Always 1 |
| endpoint_url | TEXT | e.g. `http://localhost:5000/v1` |
| model_name | TEXT | Model string sent in the `model` field |
| temperature | REAL | Default 0.8 |
| min_p | REAL | Default 0.05 |
| top_k | INTEGER | Default 40 |
| top_p | REAL | Default 0.95 |
| repetition_penalty | REAL | Default 1.0 |
| max_tokens | INTEGER | Default 4096 |
| system_prompt | TEXT | Generic system prompt (not character-specific) |

**`fragments`** — The style fragment library.

| Column | Type | Notes |
|---|---|---|
| id | TEXT PK | Slug, e.g. `descriptive`, `terse` |
| label | TEXT | Human-readable name |
| description | TEXT | One-line summary (shown to agent) |
| prompt_text | TEXT | The actual directive injected at depth 0.5 |
| is_builtin | BOOLEAN | Seed fragments can't be deleted, only edited |

**`character_cards`** — Character definitions. Each card describes one character the AI can play.

| Column | Type | Notes |
|---|---|---|
| id | TEXT PK | Slug, e.g. `elena-blackwood` |
| name | TEXT | Display name |
| description | TEXT | Short summary (shown in character list) |
| persona | TEXT | Personality, background, speech patterns |
| scenario | TEXT | World context and starting situation, nullable |
| example_messages | TEXT | Few-shot examples of the character's voice, nullable |
| creator_notes | TEXT | Private notes not sent to the model, nullable |
| created_at | DATETIME | |
| updated_at | DATETIME | |

**`conversations`** — Conversation metadata.

| Column | Type | Notes |
|---|---|---|
| id | TEXT PK | UUID |
| character_card_id | TEXT FK | References `character_cards.id` |
| title | TEXT | Auto-generated or user-set |
| created_at | DATETIME | |
| system_prompt_snapshot | TEXT | Frozen copy of system prompt at creation time |
| character_card_snapshot | TEXT (JSON) | Frozen copy of character card fields at creation time |

**`messages`** — Conversation history (only user + writer messages, never agent output).

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| conversation_id | TEXT FK | |
| role | TEXT | `user` or `assistant` |
| content | TEXT | |
| turn_index | INTEGER | Monotonically increasing per conversation |
| created_at | DATETIME | |

**`conversation_logs`** — Per-turn pipeline audit log. Records agent pass output and director decisions for debugging and the inspector panel.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| conversation_id | TEXT FK | |
| turn_index | INTEGER | Matches the corresponding message turn |
| agent_raw_output | TEXT | Full agent response (content + tool calls as JSON) |
| tool_calls | TEXT (JSON) | Parsed tool call array |
| active_styles_after | TEXT (JSON) | Snapshot of active styles after applying tool calls |
| persistent_notes_after | TEXT (JSON) | Snapshot of persistent notes after applying tool calls |
| momentary_note_after | TEXT | Momentary note after applying tool calls, nullable |
| injection_block | TEXT | The assembled `<current_scene_direction>` block sent to writer |
| agent_latency_ms | INTEGER | Time taken for the agent pass |
| created_at | DATETIME | |

**`director_state`** — Per-conversation agent state. One row per conversation.

| Column | Type | Notes |
|---|---|---|
| conversation_id | TEXT PK FK | |
| active_styles | TEXT (JSON) | List of active fragment IDs |
| persistent_notes | TEXT (JSON) | Array of persistent note strings |
| momentary_note | TEXT | Current momentary note, nullable |

---

### Core Pipeline: `orchestrator.py`

This is the heart of the system. One function, `handle_turn`, runs the full two-pass flow:

```
async def handle_turn(conversation_id, user_message) -> AsyncIterator[str]:
```

**Step 1 — Load state.** Read conversation history, current director state (active styles, notes), settings, and the conversation's character card snapshot from the DB.

**Step 2 — Build the agent prompt.** Assemble the shared prefix: system prompt, then character card (persona + scenario + example messages), then full message history. Append the OOC agent prompt from the design doc, substituting in the current active styles, persistent notes, momentary note, user message, and available fragment list. Include the three tool definitions (`set_writing_styles`, `update_persistent_note`, `set_momentary_note`) as OpenAI-format function schemas.

**Step 3 — Agent pass (non-streaming).** Call `llm_client.complete()` with `tool_choice: "required"` (or `"auto"` depending on server support). Parse the response for tool calls. This call is **not** streamed to the user — it's consumed internally. Expect ~200-400 tokens output, so it's fast.

**Step 4 — Apply tool calls.** Process each tool call through `state.py`:
- `set_writing_styles` → update `active_styles` in director state
- `update_persistent_note` → apply keep/append/remove/replace logic
- `set_momentary_note` → overwrite momentary note

Save the mutated state to the DB.

**Step 5 — Assemble the depth-0.5 injection.** Look up the `prompt_text` for each active style fragment. Build the `<current_scene_direction>` XML block with styles + persistent notes + momentary note.

**Step 6 — Build the writer prompt.** Same shared prefix as step 2 (system prompt + character card + history). Append the injection block, then the user's actual message.

**Step 7 — Writer pass (streaming).** Call `llm_client.stream()` with the user's configured hyperparams. Yield tokens back to the caller as they arrive.

**Step 8 — Persist.** After streaming completes, save the user message and the full assistant response to the `messages` table. Write the agent's raw output, parsed tool calls, post-mutation state snapshots, and the injection block to `conversation_logs` for the inspector. The injection block and agent output are *not* included in future prompt construction — they vanish from the model's perspective.

---

### LLM Client: `llm_client.py`

A thin async HTTP wrapper around the OpenAI `/v1/chat/completions` endpoint.

Two methods:

`complete(messages, tools, **params) -> dict` — Non-streaming. Used for the agent pass. Sends `stream: false`. Parses the response JSON and returns the message object (content + tool_calls).

`stream(messages, **params) -> AsyncIterator[str]` — Streaming. Used for the writer pass. Sends `stream: true`. Yields content deltas from SSE chunks.

Both methods accept the full hyperparam set. Non-standard params (`min_p`, `top_k`, `repetition_penalty`) are passed in the request body at the top level — this is how llama.cpp server, TabbyAPI, and vLLM handle them. If the server ignores unknown fields, they're harmless. The client doesn't validate server compatibility; it just sends them.

**Tool call format:** Uses OpenAI's `tools` array with `type: "function"`. The three agent tools are defined as JSON Schema function descriptions. The response parser extracts `tool_calls[].function.name` and `tool_calls[].function.arguments`.

---

### Frontend Architecture

**ChatPanel** — The primary view. Message list with auto-scroll. Input box at the bottom. Sends messages via POST to `/api/chat/{conversation_id}/send`. Receives the response as an SSE stream (the backend proxies the LLM stream). Shows a subtle "directing..." indicator during the agent pass (the backend can emit a non-content SSE event for this).

**SettingsDrawer** — Slide-out panel. Fields for endpoint URL, model name, all hyperparams (sliders for float values, number inputs for integers), and a textarea for the generic system prompt. Changes are saved to the backend on blur or explicit save.

**CharacterManager** — Character card CRUD panel. Lists all character cards with create/edit/delete. Each card form has fields for name, persona (large textarea), scenario, and example messages. Users select a character card when creating a new conversation.

**FragmentManager** — List of all fragments with inline editing. Each fragment shows its ID, label, description, and the full prompt text. Users can add custom fragments, edit builtins, or soft-delete customs.

**DirectorInspector** — A collapsible debug panel (hidden by default). Reads from `conversation_logs` to show the agent's raw tool call output for any turn, the active styles (highlighted in the fragment list), the injection block that was sent, and the current persistent + momentary notes. This is essential during development and useful for power users who want to understand why the model's style shifted.

**Streaming UX:** The frontend opens an `EventSource` to `GET /api/chat/{conversation_id}/stream/{turn_id}`. The backend emits three event types: `director_start` (agent pass beginning), `director_done` (agent pass finished, includes the parsed tool calls for the inspector), and `token` (writer output delta). The frontend appends token deltas to the current message bubble in real time.

---

### API Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/settings` | Get current settings |
| PUT | `/api/settings` | Update settings |
| GET | `/api/fragments` | List all fragments |
| POST | `/api/fragments` | Create a custom fragment |
| PUT | `/api/fragments/{id}` | Edit a fragment |
| DELETE | `/api/fragments/{id}` | Delete a custom fragment |
| GET | `/api/characters` | List all character cards |
| POST | `/api/characters` | Create a character card |
| GET | `/api/characters/{id}` | Get a single character card |
| PUT | `/api/characters/{id}` | Edit a character card |
| DELETE | `/api/characters/{id}` | Delete a character card |
| GET | `/api/conversations` | List conversations |
| POST | `/api/conversations` | Create a new conversation (requires `character_card_id`) |
| DELETE | `/api/conversations/{id}` | Delete a conversation |
| GET | `/api/conversations/{id}/messages` | Get message history |
| POST | `/api/conversations/{id}/send` | Send a message, returns SSE stream |
| GET | `/api/conversations/{id}/director` | Get current director state (for inspector) |
| GET | `/api/conversations/{id}/logs` | Get conversation logs (agent output per turn) |
| GET | `/api/conversations/{id}/logs/{turn}` | Get log for a specific turn |

---

### Key Design Decisions and Tradeoffs

**Why not WebSockets?** SSE is simpler, unidirectional (which is all we need for streaming responses), and works through more proxies. The user sends messages via POST; they receive the stream via SSE. No need for bidirectional communication.

**Agent pass hyperparams.** The agent pass should use low temperature (0.2-0.3) and a shorter max_tokens (512) regardless of user settings. It's making analytical decisions, not creative writing. These are hardcoded in `agent.py`, not exposed to the user.

**Tool call fallback.** Some OpenAI-compatible servers have spotty function-calling support. If the agent response doesn't contain parseable tool calls, the system should attempt to extract JSON from the text content as a fallback (many models will output the tool call as JSON in their message body). If that also fails, skip the agent pass entirely for this turn — use the previous director state and proceed to the writer pass. Never block the user from getting a response.

**Prefix caching.** The design doc emphasizes KV cache reuse. We can't *control* server-side caching, but we can *cooperate* with it by ensuring the shared prefix (system prompt + character card + history) is byte-identical between the agent and writer passes. This means the system prompt text, character card fields, and history messages must be exactly the same strings in both requests, in the same order. `orchestrator.py` builds the prefix once and reuses the list object for both calls.

**No conversation branching or editing in v1.** Regeneration and message editing would require careful cache invalidation logic. Defer to v2.

---

### Seed Data

Ship with four built-in fragments from the design doc (descriptive, talkative, theatre-play, internal-monologue) plus two more practical ones (terse, tense). The agent prompt's available-styles list is built dynamically from whatever fragments exist in the DB, so users can expand the library without touching code.

---

### Open Questions for Implementation

1. **Agent prompt tuning.** The agent prompt in the design doc is a starting point. It will need iteration — especially around how aggressively it should change styles vs. maintaining stability. Consider adding a "style momentum" instruction: "only change styles if there's a clear tonal shift, not just a minor variation."

2. **Multiple characters.** The design doc implies single-character play. Supporting multiple NPC characters would require per-character notes and potentially per-character style directions. Flag as a v2 concern.
