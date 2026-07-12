# Frontend Architecture Audit + Foundation Refactor — Orb

## Context

User asked for a thorough architectural audit of `frontend/` (~16.6k lines vanilla JS, 44 ES modules, no build step, served at `/static/`). Metrics: **symmetry** and **separation of concerns**. Constraint (user-confirmed): **stay vanilla** — no framework, no build step, no vendored libs. Scope (user-revised): deliver the written audit report **and incorporate the safely-implementable stages of the remediation roadmap into this refactor**; the high-risk stages stay as a documented roadmap.

All findings verified with file:line refs across 3 exploration sweeps + first-hand spot-checks + a design pass. Double-check pass done: backend SSE emission verified (`event: X\ndata: Y\n\n` + `: keepalive\n\n` from the **shared** `_sse_stream` in backend/api/deps.py:195-209 — chat streams get keepalives too; the chat parser survives them only by accident), the existing docs/architecture/sse-stream.md documents the exact frame contract the new parser must honor (and names `processSSEStream`, so it needs updating), node v22 available for a test harness, mkdocs.yml has an explicit nav (report needs a nav entry).

## Part A — the audit report (deliverable 1)

Write `docs/architecture/frontend-audit-2026-07.md` + add nav entry in `mkdocs.yml` under Architecture. Content (fully specified below): executive verdict; findings §SoC and §symmetry; churn evidence; exemplars; target architecture; roadmap with what-was-done-now marked; micro-lib triggers. Include a mermaid module-dependency diagram (current cycles vs target DAG). Reference sse-stream.md and secondary-workflow.md rather than duplicating them.

### Verdict
- Primitives good, system-level symmetry poor; exemplars in-repo prove the skill exists (audio trio, document_editor.js, library_fragments.js, esc trio).
- **Frozen plugin ABI discovery**: workflow plugins import absolute `/static/*.js` URLs (state.js, chat.js, api.js, utils.js, modal.js, audio_player.js, workflow_segmentation.js, workflow_text_effects.js). ES-module identity is per-URL → enforce layering logically (import-direction rule + lint), physical moves optional/last behind root shims.
- AGENTS.md frontend section describes a dead frontend (chat.js monolith, "api.js holds all fetch", style.css main stylesheet); docs/architecture/sse-stream.md says `_sse_stream` lives in main.py (it's api/deps.py).

### Findings — separation of concerns
1. **Window-global bridge**: ~55 inline `on*` in index.html + ~140 in JS templates resolve via app.js `Object.assign(window, {~180 fns})` (app.js:239-443); app.js churned 107 commits/4mo. Parallel convention: self-attached `window.*` (settings.js:824-937, settings_models.js:283/786, chat_workflow.js ~7 sites, modal.js `window._confirmCb`, tts `window.ttsCreate`).
2. **S = 120-key flat bag, 9+ domains, no ownership** (state.js); ~600 direct writes (chat_stream 168, settings 128); mutation+render manually paired. `S.allCharacters` never declared (born library.js:96); `(S.allCharacters || S.characters || [])` ~10×. state.js also hosts the 8 workflow registrars.
3. **Chat cluster cyclic** (5 cycles: core↔workflow; core→workflow→inspector→core; stream↔messages; stream↔composer; library↔conversations). Underscore-"private" exports imported cross-module (_renderWorkflowArtifacts, _mergeWorkflowRejections, _avatarBust). Stream rewrites core-owned DOM (finalizeStreamingDiv chat_stream.js:123, patchParentUserMessage :198); swipe-nav markup duplicated (chat_stream.js:152-157 vs chat_core.js:365-370).
4. **Misplaced concerns**: utils.js imports S + domain logic (effectivePersonaId, convActivity, resolvePlaceholders); modal.js contains canvas crop editor (:119-346); chat_core.js contains home stats (:224-295) + context counter; settings.js (962 lines) is a 6-concern orchestrator.
5. **Helper boundary violations**: tabLock.js mutates chat DOM — `send-btn.disabled` written from two modules (chat_stream setStreaming/:687/:755 and tabLock.js:208), with doc mode running a third parallel copy of the pattern on its own buttons; tabLock also carries an unrelated cross-tab workflow-mutation bus. mobile.js MutationObservers on other features' DOM + injects into lorebook drawer + ~10 hard-coded ID couplings. Five independent Escape listeners, no overlay manager.
6. **Plugin boundary leaks**: tts imports renderMessages/setWorkflowPhase/refreshConversationMessages from /static/chat.js, calls renderMessages() directly (config_panel.js:122), reads S.messages/S.activeConvId. No narrow facade.

### Findings — symmetry
1. **SSE triplicated**: chat_stream.js:351 (line-split), document.js:520 (frame-split + keepalives + probs-no-unescape), chat_conversations.js:436-462 (copy of chat idiom). Three bespoke transports (chat_stream.js:59, chat_conversations.js:375, document.js:585).
2. **Stream lifecycle 3×**: runStreamRequest (chat_stream.js:684) + copies in sendMessage (:751-800) and saveForkEdit (chat_messages.js:445-468).
3. **Document mode parallels chat infra**: flags, stop-button swap, stop endpoint, autoscroll (document.js:59/:480/:649 vs chat_stream.js:68/:166, chat_messages.js:247). Persistence legitimately differs.
4. **Three save strategies** (reload-after-save / optimistic patch / location.reload()) and **two state homes** (S / module closures) with no rule.
5. **CRUD shape**: canonical (library_fragments.js) vs deviants (lorebooks bespoke drawer + dirty machine + lb* + no validate.js; presets sub-modal + reload + naming; library_browser fetch-in-opener; direction_notes fetch+render merged; phrase bank modal-reopen-per-save).
6. **Copy-paste**: delete-confirm ~7×; chips widget (lorebooks.js:478-580 vs library.js:423-473); char validation gauntlet 3× (library.js:359/:547/:645); download-anchor 2× (library.js:221-228, presets.js:152-159); validateEditMessage ≡ validateChatInput (validate.js:603-612 vs :227-236); `enabled===true||enabled===1` ~14×; 3 local-ML 503-degrade sites; "Close other tabs" markup ~10 sites/4 modules; console.log leftovers (library.js:494/498/613/646).
7. **Naming**: toggle*/set* mixed; createCharacter/saveCharEdit/saveImportedChar; lb* prefix; dead validate.js named exports + constants.

### Churn (4mo)
chat.js 142, app.js 107, style.css 105, settings.js 100, library.js 77, state.js 68, index.html 58.

### Target architecture (6 logical layers, one-way imports; lint-enforced first, directories optional last)
L0 core (pure leaves incl. SSE parser) → L1 state (S declared+owned, subscribe/notify bus, selectors) → L2 services (api, sse, crosstab/tab_presence, local_ml) → L3 ui (modal, overlays, panels, actions, chips) → L4 platform (workflow registry, segmentation/effects/interaction, default_widget, audio trio) → L5 features (chat/document/library/settings/home; peers never import each other; cross-feature via notify(topic); documented exception: navigation entrypoints from chat barrel) → L6 shell (app.js boot-only, workflow_loader, workflow_api facade). Plugins import only the ABI files.
Key mechanisms (full detail in report): data-action dispatcher replacing the window bridge (hybrid migration, ratcheted); flat S + 20-line pub/sub (NOT nesting — ~600 sites + plugin ABI); chat DAG via 4 surgical moves; workflow_api.js facade (ABI v1 files stay deprecated-but-stable); NO crudPanel framework — convention + extracting true duplicates; overlays.js Escape stack; capability.js multi-tab gate; tabLock split (presence vs crosstab bus, zero DOM).

### Micro-lib triggers (stay-vanilla escape hatches)
(1) a 3rd/4th bespoke DOM-diff hack → vendored lit-html for the chat spine; (2) monthly mutate-without-notify bugs post-DAG → vendored preact signals; (3) plugin ABI v2 forced anyway → the rational moment for templates (renderers return HTML strings today).

## Part B — stages implemented in THIS refactor (deliverable 2)

Incorporate the low-risk foundation stages (0, 1, 2, 6, 7-lite). Defer 3 (tabLock/overlay/mobile decoupling), 4 (chat DAG), 5 (event wiring), 8 (physical moves) — they touch the render spine/multi-tab paths or need many small PRs; roadmap order 3→4→5(→8) documented in the report.

### Stage 0 — guardrails
- `scripts/check_frontend_layers.py` (~80 lines): `{file: layer}` manifest over current FLAT files + allowed-edge table, regex-parses `import ... from "./x.js"`. Two ratchets: max inline `on*=` count, max underscore cross-module imports (may only decrease). Known current cycles enter a documented allowlist that shrinks as stages 3-4 land (lint fails on NEW violations only).
- Wire into `scripts/lint.sh`.
- `tests/frontend/*.test.mjs` on `node --test` (node v22 confirmed; zero deps, no jsdom): first targets = sse parser (frames, keepalives, chunk-boundary splits, escaped-token vs probs-JSON fixtures), validate.js rules, charactersView selector.
- Manual smoke checklist appended to AGENTS.md (boot clean console; send/abort/regen/branch-nav; edit-fork; doc generate+probs+abort; every panel/modal + Escape; tts round-trip; two-tab gating; settings persist).

### Stage 1 — one SSE path (flagship symmetry fix)
- New `frontend/sse.js`: `sseEvents(body, {signal})` async generator — frame-based (`\n\n`), skips `:` comments, yields `{event, data}` (raw string; **parser never unescapes**); `unescapeSSE()` helper (per-event, consumer-side — preserves document probs-JSON rule); `streamPost(path, body, signal)` (the streaming sibling of api._req).
- chat_stream.js: delete processSSEStream internals + streamPost → use sse.js; `runStreamRequest(path, body, {cutoffMsgId, optimisticUserMsg, beforeRender, afterDone})` becomes the ONLY chat lifecycle — sendMessage (:751-800) and saveForkEdit (chat_messages.js:445-468) converge onto it. handleSSEEvent stays in chat_stream (sse.js knows zero event names).
- chat_conversations.js summarize → ~15 lines over sse.js. document.js: delete readDocSSE + inline fetch (:585) → sse.js primitives; keep its thin lifecycle (persistence legitimately differs).
- Update docs/architecture/sse-stream.md: parser now sse.js (and fix its stale "main.py" → api/deps.py while touching it).

### Stage 2 — state ownership (flagship SoC fix)
- state.js: declare EVERY key incl. `allCharacters`, grouped under domain banners with `// owner: <module>` annotations; add `subscribe(topic, fn)`/`notify(topic, detail)` (~20 lines, sync fan-out, per-handler try/catch; enumerated topics documented: messages, conversations, characters, personas, settings, documents, attachments, tabs, workflow-phase); add `charactersView()` selector; replace the ~10 `(S.allCharacters || S.characters || [])` sites.
- Move the 8 registrars → new `frontend/workflow_registry.js`; state.js re-exports them (plugin ABI unchanged).
- Rule (documented, enforced by review): bus is for CROSS-module mutate→render pairs; same-module pairs stay manual. No call-site conversions this stage beyond the selector — the bus is infrastructure stages 3-4 consume.

### Stage 6 — workflow plugin facade
- New `frontend/workflow_api.js` (root, becomes ABI): re-exports registrars + api/convUrl/esc/escAttr/toast/showModal/closeModal/audio surface/messageSegments/startTextEffect/effectiveWorkflowEnabled/subscribe; adds `requestRepaint()` (rAF-debounced renderMessages), `setPhase/clearPhase`, `refreshMessages()`, `getActiveConvId()`, `getMessages()` (documented read-only).
- Migrate in-repo consumers: workflows/tts/{index,widget,karaoke,config_panel}.js + workflows/format_consistency/index.js (config_panel.js:122 direct renderMessages() → requestRepaint()).
- docs/architecture/secondary-workflow.md: document workflow_api.js as THE plugin surface; direct /static/chat.js etc. imports = deprecated-but-stable ABI v1.

### Stage 7-lite — mechanical dedupes (deletion pass)
- validate.js: `export const validateEditMessage = validateChatInput;` (delete the copy + its stale docstring); delete dead underscore constants.
- modal.js: add `confirmDelete(label, message, onOk)` thin wrapper; replace the ~7 copy-pasted confirm bodies. (Lives in modal.js — no new file.)
- utils.js: add `downloadBlob(name, blob|url)`; replace library.js:221-228 + presets.js:152-159.
- library.js: single `_validateCharForm()` gauntlet replacing the 3 copies (:359/:547/:645) + unify the near-dup payload builders; delete console.log leftovers.
- New `frontend/chips.js`: one chip-input widget replacing lorebooks.js:478-580 + library.js:423-473 (they already share CSS classes).

### AGENTS.md (targeted, not the full rewrite)
Fix stale claims (chat.js barrel + 6 chat_* modules, api.js scope, css/ split, new modules list); document the new invariants actually introduced: sse.js is the only SSE parser/transport; every S key declared with an owner; bus topics; workflow_registry.js; workflow_api.js as plugin surface; guardrail scripts; smoke checklist. The full layer table stays in the audit report as target, referenced from AGENTS.md.

## Part C — deferred roadmap (documented in report, NOT executed)
Stage 3 (M): tabLock → tab_presence + crosstab (channel name kept "orb-tab-lock"), capability.js gate, single send-btn owner, overlays.js + 5 Escape migrations, mobile observers → bus, local_ml.js 503-helper. Stage 4 (L, highest risk): the 4 chat-DAG moves + swipe/avatarBust cleanup + peer-import removal via topics. Stage 5 (M, many small PRs): actions.js data-action dispatcher, app.js bridge dissolves module-by-module. Stage 8 (L, mechanical, optional): physical dirs + root ABI shims + full AGENTS.md layer rewrite.

## Verification
1. `node --test tests/frontend/` — sse parser fixtures (incl. chunk-split frames, keepalive, probs-JSON no-unescape), validators, selector.
2. `bash scripts/lint.sh` (now incl. layer check + ratchets) and `npx biome check frontend/`.
3. Backend suite untouched but run `./scripts/tests.sh all` (stream routes have integration coverage; nothing backend changes).
4. Boot the app (`./run_unix.sh`), load UI, check clean console, exercise what's drivable without a live LLM endpoint (panels, modals, CRUD, tts config panel render). Streaming paths (send/abort, doc generate, summarize, fork-edit) need the user's local endpoint — walk the AGENTS.md smoke checklist; I'll drive it if an endpoint is configured, otherwise hand the checklist to the user as the final gate.
5. Grep-verify no orphan references (processSSEStream, readDocSSE, window._confirmCb unchanged this pass, deleted validator).

## Files
- Create: `docs/architecture/frontend-audit-2026-07.md`, `frontend/sse.js`, `frontend/workflow_registry.js`, `frontend/workflow_api.js`, `frontend/chips.js`, `scripts/check_frontend_layers.py`, `tests/frontend/*.test.mjs`
- Edit: `mkdocs.yml` (nav), `frontend/{state,chat_stream,chat_messages,chat_conversations,document,validate,modal,utils,library,lorebooks}.js`, `frontend/workflows/tts/*.js`, `frontend/workflows/format_consistency/index.js`, `scripts/lint.sh`, `AGENTS.md`, `docs/architecture/{sse-stream,secondary-workflow}.md`
