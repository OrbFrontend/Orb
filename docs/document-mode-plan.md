# Document Mode — free-form LLM-assisted writing (mikupad-style)

## Context

Orb is a turn-based RP chat frontend. This feature adds a second, orthogonal writing surface: **Document mode** — a blank, MS-Word-like editor where the user writes free-form prose and the LLM continues it from the cursor. A small switch button in the sidebar header (next to the "Orb" brand, `index.html:18`) toggles the entire UI except the left sidebar. Generation bypasses the Director→Writer→Editor pipeline entirely and prefers the **text-completion transport** (llama.cpp native `/completion` with a raw prompt string — no chat template), falling back to **chat completion** on chat-mode endpoints.

**User-confirmed decisions:**
- Editor is **contenteditable** (plain-text enforced) with generated text visually highlighted as spans + "undo last generation".
- Documents get a new **sidebar section** (Worlds/Characters pattern).
- v1 prompt is **pure document text before the cursor** — no system prompt, lorebook, macros, or memory field.
- **Continue from cursor**: text after the cursor is preserved; tokens stream in at the cursor.

## Design decisions

| Question | Decision |
|---|---|
| Backend home | **`features/documents/` slice** (AGENTS.md "Standard Slice Shape" / "Three ways to add a feature" #3, mirroring `features/summarization/`): the instruction constant, chat-fallback message shape, transport branch, and delta filtering live in the slice; `routes/documents.py` keeps only HTTP concerns (404s, AbortToken, SSE, locks). Byte-symmetric with the summarize route → `ConversationSummarizer` split. |
| Span persistence | `generated_spans` JSON offsets column alongside plain `content`. Content stays pure (directly usable as prompt). Offsets are UTF-16/JS-domain, **opaque to the backend** (only shape-validated) — dodges Python-vs-JS string-length mismatch. **Never** bounds-check spans against `len(content)` server-side: Python counts code points, JS counts UTF-16 units, so a valid JS offset can exceed Python's length on emoji-bearing docs. |
| Update contract | `content` and `generated_spans` must travel together: a `DocumentUpdate` carrying spans without content applies offsets to stale text. Pydantic model validator rejects spans-without-content (422). Title-only updates unaffected. |
| Who persists generated text | **Client only** — the generate route is a stateless LLM proxy (it still 404s an unknown `did`; see API). Client serializes `{content, spans}` from the editor DOM in one pass (can't drift) and saves at stream end/abort, **plus a 5s interval flush while streaming** so a tab crash loses ≤5s of tokens. |
| Preset domain | New `"documents"` domain (root table; reusing "chats" would silently bundle docs with chats). |
| Chat fallback shape | System instruction ("continue the text, output only the continuation") + user message with the doc prefix. Assistant-continuation is unreliable on OpenAI-compat servers. Suppress thinking via `reasoning_cfg(False)`; the slice's `stream()` yields content deltas only (drops `reasoning`), exactly like `ConversationSummarizer.stream`. |
| Raw text mode reasoning | No ThinkSplitter — raw continuation has no chat template, so no think channel. Stream provider bytes verbatim. |
| Mid-stream editing | **Lock the editor** (`contenteditable="false"` + `.generating` class) during generation. Insertion anchor is a dedicated span, so position stays stable. |
| Edits inside generated spans | Do nothing special — tint is advisory; serializer records whatever spans remain after native editing. |
| Undo-gen validity | "Undo generation" removes exactly `lastGenSpanEl`, but the user may type *inside* it afterward (edits-inside-spans are tolerated) — removing it then would eat their words. First `input` event after finalize clears `lastGenSpanEl` and disables the Undo button. |
| Abort/lock namespace | Reuse `_active_aborts`/stream locks via `_sse_stream(..., cid=f"doc:{did}")` — gets per-key lock, "already running" error, stop integration and disconnect watcher for free (`api/deps.py:129-222`). |
| Token counter | Client-side `chars/4` (mirrors `CHARS_PER_TOKEN`, `backend/core/utils.py:15`). |
| Shortcuts | Ctrl/Cmd+Enter generates; Esc stops while streaming. Both scoped: only when document mode is active and no modal is open (`$("modal-root")?.innerHTML` idiom). |
| Chat handler isolation | Chat's document-level key nav (`handleChatKeyNav`, `chat_messages.js:238`) stays live in document mode and is only blocked by input focus/modal/no-active-conv — with focus on a button (e.g. right after clicking Generate) ←/→ would silently switch branches of the hidden chat. Add an `S.documentMode` early-return to `isChatNavBlocked`. |

## Backend

### 1. Database (new domain, standard pattern)
- **`backend/database/schema.py`** — append to `CREATE_TABLES_SQL`:
  ```sql
  CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL DEFAULT 'Untitled',
      content TEXT NOT NULL DEFAULT '',
      generated_spans TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
  );
  ```
- **`backend/database/migrations/0039_documents.py`** (new; latest is 0038) — `conn.execute(table_create_sql("documents"))`, mirroring 0038. Migrations are glob-discovered; no registry edit.
- **`backend/database/models.py`** — `DocumentListRow` (id/title/created_at/updated_at) + `DocumentRow(DocumentListRow)` adding `content: str`, `generated_spans: list` (decoded shape — only `get_document` decodes). Note in a comment: this is deliberately the *inverse* of `ConversationListRow(ConversationRow)` — that one adds join fields to the full row, this one is a **column projection** (list view must not drag full `content` for every doc), so the full row extends the projection.
- **`backend/database/queries/documents.py`** (new; CRUD bodies follow `queries/worlds.py`, naming follows the query layer's `get_X` convention — no `list_*` exists anywhere): `get_documents` (list projection `SELECT id, title, created_at, updated_at`, `ORDER BY updated_at DESC`), `get_document` (json-decodes spans), `create_document`, `update_document` (`allowed=["title","content","generated_spans"]`, `json_fields={"generated_spans"}` via `_build_set_clause`, bumps `updated_at`), `delete_document`. Re-export via `backend/database/__init__.py`.
- **`backend/database/preset_schema.py`** — `DOMAIN_ROOTS["documents"] = "documents"`.
- **`tests/integration/test_preset_schema_coverage.py:131`** — append `"documents"` to the frozen `ALL_DOMAINS` literal (alphabetical: after `"configs"`).
- **`frontend/presets.js`** — add `{id:"documents", label:"Documents"}` to the `DOMAINS` array so selective export offers it.

### 2. Inference: `LLMClient.complete_raw` (`backend/inference/client.py`, next to `_complete_text`)
```python
async def complete_raw(self, prompt: str, model: str, **params) -> AsyncIterator[dict]:
```
- Text-transport only, raw prompt: `body = text_completion.build_completion_params(params)` (allowlist remap: max_tokens→n_predict, repetition_penalty→repeat_penalty, adds `cache_prompt:true`), `body["prompt"] = prompt`, `body["stream"] = True`.
- Stream through the existing single HTTP seam `_stream_completion(f"{self._server_root()}/completion", body)` (`client.py:395-414`) — abort already flows through `_iter_sse_payloads`.
- Yield `{"type":"content","delta":...}` per chunk; on the `stop` chunk, `synthesize_usage(data)` then `{"type":"done","message":{"content":...},"usage":...}` — preserves the `complete()` event contract. No ThinkSplitter.
- `cache_prompt:true` gives KV reuse across successive continuations for free.

### 3. Feature slice: `backend/features/documents/` (Standard Slice Shape)
```
features/documents/
├── __init__.py        # facade: re-export DocumentContinuer
└── continuation.py    # prompt policy + transport policy, no HTTP
```
- **`continuation.py`** — module constant `DOC_CHAT_INSTRUCTION` (single place to iterate on fallback quality), plus (mirroring `ConversationSummarizer`):
  ```python
  class DocumentContinuer:
      def __init__(self, client: LLMClient, settings: Mapping[str, Any]): ...
      def build_chat_messages(self, prompt: str) -> list[ChatMessage]:
          # [system=DOC_CHAT_INSTRUCTION, user=prompt]
      async def stream(self, prompt: str, model: str) -> AsyncGenerator[str, None]:
          # transport branch on self.client.completion_mode (the client's own
          # field — single source of truth, not a second settings read):
          #   "text" -> client.complete_raw(prompt, model, **params)
          #   else   -> client.complete(self.build_chat_messages(prompt), model,
          #                             **params, **reasoning_cfg(False))
          # yield chunk["delta"] only for chunk["type"] == "content"
          # (drops reasoning deltas — same filter as ConversationSummarizer.stream)
      ```
- Params: `extract_hyperparams(settings, defaults={"max_tokens": 512})` (guards against unset → `n_predict=-1` runaway).
- Layering: slice imports `inference` + `core` only — strictly downward, no route/pipeline imports.

### 4. API (`backend/api/`)
- **`schemas.py`**: `DocumentSpan{start,end}` (`ge=0` only — no length coupling, see design table), `DocumentCreate{title?}`, `DocumentUpdate{title?, content?, generated_spans?: list[DocumentSpan]}` with a `model_validator` rejecting `generated_spans` set while `content` unset, `DocumentGenerateRequest{prompt}`.
- **`routes/documents.py`** (new; append to `ROUTERS` in `routes/__init__.py`) — HTTP concerns only; prompt/transport policy lives in the slice:
  - `GET/POST /api/documents`, `GET/PUT/DELETE /api/documents/{did}` — standard CRUD.
  - `POST /api/documents/{did}/generate` — SSE (`token`/`done`/`error`), modeled on the summarize route (`routes/conversations.py:154-214`):
    - 404 unknown `did` first (`await get_document(did)`) — the proxy doesn't need the row, but every other `{did}` route 404s, and this avoids minting locks/abort entries for garbage ids.
    - `settings = await get_settings()`; `LLMClient(settings["endpoint_url"], api_key=..., abort_token=AbortToken(), completion_mode=settings.get("completion_mode","chat"))`; `continuer = DocumentContinuer(client, settings)`.
    - `_gen()` wraps `continuer.stream(data.prompt, model)` → `{"event":"token","data":delta}`…`{"event":"done"}`, error → in-band `error` event (summarize-route shape).
    - Return `_CleanupStreamingResponse(_sse_stream(_gen(), request, abort_token=..., cid=f"doc:{did}"), media_type="text/event-stream")`.
  - `POST /api/documents/{did}/stop` — `_active_aborts.get(f"doc:{did}")` → `.abort()` (mirror `conversations.py:359-366`).
  - Keep `LLMClient` a module-level bound import so tests can patch `backend.api.routes.documents.LLMClient` (the route constructs the client and hands it to the slice, so this stays the only patch seam).

## Frontend

### New files
- **`frontend/document_editor.js`** — the **pure editor model**, the invariant-heavy core in one reviewable unit: `serializeEditor(pageEl, stopNode?)`, `renderEditor(pageEl, content, spans, anchorOffset?)`, `computeCaretOffset(pageEl)`, plain-text guard installers. No `S`, no fetch, no rendering of anything but `#doc-page` children — separable and unit-testable if frontend tests ever land.
- **`frontend/document.js`** — everything stateful: list/CRUD, mode toggle, autosave, generation, shortcuts. Imports the editor model. (Two files mirrors the chat_*.js split-by-concern without over-fragmenting a feature this size.)
- **`frontend/css/document.css`** — layout + editor + list styles incl. its own ≤900px block (precedent: `css/chat.css` has one); `@import` appended in `style.css`.

### Modified files
- **`index.html`** — (1) mode-switch button in `.sidebar-header-top` (line 18): `<button id="mode-switch-btn" onclick="toggleDocumentMode()">📄</button>` (flex row, `margin-left:auto`; icon/title swaps per mode). (2) "Documents" `.sidebar-section` after Characters (~line 50) with `+ New Document` button and `#documents-list`, exact Worlds/Characters pattern. (3) `#document-main` as a sibling of `#main`: `#doc-header` (mobile ☰, title, rename ✎) + `#doc-editor-scroll > #doc-page[contenteditable]` + `#doc-footer` (token count, save state, gen status, Undo generation / Generate / Stop buttons).
- **`state.js`** — add `documents: []`, `activeDocId`, `documentMode`, `docStreaming`, `docAbortController`, `docDirty`.
- **`app.js`** — import from `./document.js`, add handlers to `Object.assign(window, {...})` (`toggleDocumentMode, createDocument, openDocument, deleteDocument, renameActiveDocument, docGenerate, docStop, docUndoLastGen`), call `initDocumentMode()` + `loadDocuments()` in `initAll()`.
- **`chat_messages.js`** — one line in `isChatNavBlocked`: `if (S.documentMode) return true;` (see design table: hidden-chat branch switching).

### Mode toggle (class idiom — no router exists)
```css
#document-main { display: none; }
#app.document-mode #main, #app.document-mode #tools-panel,
#app.document-mode #inspector, #app.document-mode #direction-notes-panel { display: none !important; }
#app.document-mode #document-main { display: flex; flex: 1; flex-direction: column; min-width: 0; }
```
Persist mode + active doc in `localStorage` (theme precedent, `settings.js`); restore on boot. Guard toggle on `S.docStreaming`. Mobile: off-canvas sidebar/overlay work unchanged (keyed off `#app.mobile-sidebar-open`); only show `#doc-mobile-sidebar-toggle` under 900px; `100dvh` sizing is inherited from `#app`.

### Editor (contenteditable, plain text — all in `document_editor.js`)
- **Content model (load-bearing invariant):** direct children of `#doc-page` are only text nodes and non-nested `<span class="gen-text">`; newlines are literal `"\n"` (`white-space: pre-wrap`). Page styled Word-like (max-width ~780px, centered, padded, shadowed) inside scrollable `#doc-editor-scroll`.
- **Plain-text enforcement:** `paste` → preventDefault + `execCommand("insertText", plainText)` (preserves native undo stack); `beforeinput` → rewrite `insertParagraph`/`insertLineBreak` to `"\n"`, block `format*`/`insertFromDrop`. Serializer is defensive (tolerates `<br>`/`<div>` as `"\n"`) so browser quirks degrade to normalization on next save, never data loss.
- **`serializeEditor(stopNode?)`** — single source of truth: walks childNodes → `{content, spans}`; prompt extraction reuses it with a stop node.
- **`renderEditor(content, spans, anchorOffset?)`** — rebuilds DOM from content+spans; only called on doc open and generation start (never while typing → no caret jumps/IME issues).

### Generation flow (`docGenerate`, in `document.js`)
1. Guards (`activeDocId`, `!S.docStreaming`) → `flushSave()`.
2. `computeCaretOffset()` — walk childNodes accumulating serialized length to the selection anchor; selection outside editor → end of doc.
3. Split in the **string domain**: serialize, split any span straddling the caret, `prompt = content.slice(0, caretOffset)`.
4. `renderEditor(...)` with an empty streaming anchor `span.gen-text.gen-active` at the caret; keep a ref to its text node.
5. Lock editor, swap Generate→Stop, show status; `fetch POST /generate` with `S.docAbortController.signal`; consume with a **small dedicated SSE reader** (~35 lines — do *not* reuse the chat-coupled `processSSEStream`). The reader must handle three wire facts of `_sse_stream`, each silently corrupting if missed:
   - **Unescape `\n`**: the server escapes newlines in `data` (`deps.py:208`); chat unescapes with `data.replace(/\\n/g, "\n")` (`chat_stream.js:399`). Newlines are load-bearing in the editor content model — forgetting this flattens every multi-paragraph continuation.
   - **Ignore `: keepalive` comment frames** (emitted every 15s during silent stretches).
   - **Surface the in-band `event: error`** — including the lock's "Another generation is already running" — as a toast + finalize.
6. Per token: append to the anchor text node; scroll into view only if near bottom. A **5s interval flush** (started at generation start, cleared at finalize) serializes and PUTs so a tab crash loses ≤5s; CRUD routes take no stream lock, so the PUT never contends with the SSE.
7. Finalize (done/error/abort): stop interval; empty anchor → remove (+ toast); else keep as module-local `lastGenSpanEl`, enable Undo. Unlock, caret after anchor, **immediate save**.
8. `docStop()` = `abortController.abort()` + fire-and-forget `POST /stop`. `docUndoLastGen()` removes exactly `lastGenSpanEl`, saves. First `input` after finalize clears `lastGenSpanEl` + disables Undo (protects post-generation edits typed inside the span). Ctrl/Cmd+Enter generates; Esc stops — both only when `S.documentMode` and no modal open (`$("modal-root")?.innerHTML` idiom), so they can't collide with modal.js/mobile.js Esc handlers.

### Autosave
`input` → dirty + debounce 1500ms → `flushSave()` (serialize → `PUT {content, generated_spans}` → update `S.documents` entry + resort + "Saved" indicator). Content and spans always travel together (backend validator enforces it). Immediate flush on blur, doc switch, mode toggle, before generate, stream end, undo-gen, and `beforeunload` (keepalive fetch). Token counter updates on input (`chars/4`).

### Documents list
`renderDocuments()` mirrors `renderCharacters()` (`library.js:113-138`): title + relative date, `.active` highlight, rename (modal.js) + delete (confirm modal). Click → `openDocument(id)`, which also enters document mode if needed. Deleting the active doc clears to an empty state; blocked while streaming.

## Out of scope for v1
Per-generation sampler overrides / max-tokens UI; memory/author's-note field; lorebook, macros, personas, any prompt injection; branching/versions; token probabilities; FIM/infill; reasoning display; ghost autocomplete in the editor; file export; `generated_chars` stats integration; multi-tab conflict handling (last-write-wins, like the rest of the app).

## Testing
- **`tests/integration/_llm_mock.py` + `conftest.py`**: add `FakeLLMClient.complete_raw` (new "raw" queue + `enqueue_raw`; capture prompt/params) and patch `backend.api.routes.documents.LLMClient` in the `llm_mock` fixture (same import-binding trap as conversations). **Chat-mode note:** the doc chat fallback calls `complete()` with no tools and `tool_choice=None`, so `_pass_from_tool_choice` routes it to the **writer** queue — chat-mode doc tests use `enqueue_writer`; only text-mode tests use `enqueue_raw`.
- **`tests/integration/test_documents.py`** (new): CRUD lifecycle + span JSON roundtrip + 404s (including 404 on `/generate` for an unknown id); 422 for spans-without-content on PUT; generate in chat mode (assert messages are exactly `[system, user=prompt]`, SSE frames `token`…`done`); generate in text mode (flip endpoint `completion_mode` to `"text"`, assert `complete_raw` got the verbatim prompt); stop with/without active token; optional concurrent-generate rejection.
- **`tests/unit/test_complete_raw.py`** (new): patch `client._stream_completion` (documented test seam) — assert event contract, no think-tag splitting (literal `<think>` arrives as content), usage synthesis, body shape (`prompt`, `stream`, `n_predict` remap, `cache_prompt:true`).
- **`tests/unit/test_document_continuation.py`** (new, cheap): `DocumentContinuer` against a stub client — chat path builds exactly `[system, user]` and spreads `reasoning_cfg(False)`; text path calls `complete_raw` with the verbatim prompt; reasoning deltas are dropped, content deltas pass through.
- Preset coverage test passes once `DOMAIN_ROOTS` + `ALL_DOMAINS` edits land.

## Implementation order + verification gates
1. **DB layer** (schema, models, queries, `__init__`, migration 0039, preset_schema, coverage literal) → `python -c "import backend.main"`, pyright zero, `./scripts/tests.sh integration`.
2. **`complete_raw`** + unit test → pyright, `./scripts/tests.sh unit`.
3. **Feature slice + API** (`features/documents/`, unit test, schemas, routes/documents.py, ROUTERS, mock, integration tests) → `./scripts/tests.sh all`; curl smoke of CRUD + generate against a llama.cpp (text) and a chat endpoint.
4. **Frontend shell** (index.html, document.css, style.css import, state.js) → app loads, chat mode visually unchanged, class toggle works at desktop + 900px.
5. **Editor model + document.js core** (document_editor.js render/serialize/caret/plain-text guards; document.js CRUD/list, mode toggle + localStorage, autosave, `isChatNavBlocked` guard, app.js wiring) → create/edit/reload roundtrips content + highlights; rich paste lands plain; arrow keys in document mode no longer reach chat branch nav.
6. **Generation** (docGenerate/SSE reader/lock/finalize, streaming interval flush, stop, undo + invalidation, shortcuts, status, counter, mobile toggle, presets.js entry) → manual run (`./run_unix.sh`): mid-doc generation preserves suffix; multi-paragraph continuation keeps its newlines; stop persists partial span; undo removes exactly it and is disabled after typing; concurrent generate surfaces the in-band error toast; mobile sidebar overlays editor.
7. **Docs + format**: update AGENTS.md (directory structure incl. `features/documents/`, DB table, API endpoints), `./scripts/format_backend.sh`, `./scripts/format_frontend.sh`, `./scripts/lint.sh`, final `./scripts/tests.sh all`.

## Known risks (accepted for v1)
- contenteditable span-boundary quirks vary by browser — cosmetic tint drift only; serializer normalizes on save.
- Programmatic re-render at generation start truncates native Ctrl+Z history; "Undo generation" covers the case that matters.
- Chat-fallback continuation quality is model-dependent (possible preamble despite instruction) — single slice constant, cheap to iterate; text mode is the recommended path.
- Models that emit EOS immediately produce an empty span → handled (remove + toast).
- Tab crash mid-generation loses at most the last ~5s of tokens (interval flush); a crash between flushes is last-write-wins like the rest of the app.
