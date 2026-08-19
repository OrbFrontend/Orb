# AGENTS.md — Orb Codebase Guide

> Keep this current when architecture changes — it's the single source of truth.

## Project Overview

Orb is an **agentic AI roleplay/writing frontend**: Python/FastAPI backend, vanilla JS frontend. Orchestrates a multi-pass LLM pipeline (Director → Writer → Editor). Characters are PNG cards (V3 spec, with V2/V1 fallback). Conversations are branching message trees with lorebooks, mood/interactive fragments, and personas.

**Stack:** Python 3.11+, FastAPI, aiosqlite, SQLite, vanilla JS (no framework), uvicorn

## Architecture

Pipeline passes: **Director** (optional, pre-writer) → **Writer** (streams output) → **Editor** (optional, post-writer auditor/rewriter).

- **Cross-pass KV caching:** All passes share one byte-identical prefix (same system prompt, history, tool schemas). Read [docs/architecture/kv-cache.md](docs/architecture/kv-cache.md) before touching prompt assembly, pass ordering, or tool schemas. Group chats are the one place a prefix may legitimately vary *within* a beat: under `group_context_mode = 'swap'` the active card sits before history, so each speaker gets its own frozen base and the Director keeps a neutral one.
- **Dynamic Worlds:** the Agent may propose durable World changes from a finished turn, to **every enabled World that opted in** (`dynamic_enabled`) — one forced call, split into one pending changeset per World. It only ever writes an *overlay* — `lorebook_entries.entry_layer` splits user-`authored` rows from Agent-`dynamic` ones, and no code path lets the Agent touch an authored row. Every consumer sees the resolved view through `inference/lorebook.select_effective_entries`. Read [docs/architecture/dynamic-worlds.md](docs/architecture/dynamic-worlds.md) before touching lorebook projection, the changeset lifecycle, or `worlds.content_revision`.
- **Editor patching:** `editor_apply_patch` anchors on a numbered finding id, not a `search` string — `analysis/targets.py` resolves the audit into addressable offsets. Every replacement is healed before it lands (`analysis/healing.py`): any run of words the model copied from the draft *outside* its target span is trimmed off either end, so a mis-aimed patch can't print the same text twice.
- **Secondary workflows:** Pluggable hooks (pre/post pipeline, on-demand). Full reference: [docs/architecture/secondary-workflow.md](docs/architecture/secondary-workflow.md).
- **SSE wire contract:** [docs/architecture/sse-stream.md](docs/architecture/sse-stream.md).

## Layer Stack

Dependencies run **strictly downward**. Never import up or sideways into a peer slice.

Dependency order (top to bottom — each layer may only import layers below it):

1. `api/`
2. `pipeline/`, `features/`
3. `workflows/`
4. `inference/`, `analysis/`
5. `database/`
6. `core/`

`database/` may also import `core/`. Feature slices follow the ordinary downward
rule: pure logic may reuse lower-layer inference/analysis utilities, while an
integration module may persist through `database/`; slices never import peers.

| Layer | Purpose |
|-------|---------|
| `core/` | Dependency-free kernel: `domain_types`, `llm_types`, `macros`, `locks`, `text_segmentation`, `utils` |
| `database/` | aiosqlite foundation: schema, migrations, queries, models (TypedDicts) |
| `inference/` | LLM transport + prompt/tool assembly (`client`, `cached_call`, `prompt_builder`, `tool_registry`) |
| `analysis/` | Pure prose-quality detection: `audit.py` + detectors, `targets.py` (findings → id-addressable draft offsets), `patching.py`, `healing.py` (trims patch text that restates the draft around the span); shared by editor + workflows |
| `workflows/` | Plugin registry + shipped workflows (TTS, image generation, format_consistency) |
| `pipeline/` | Director→Writer→Editor turn engine (`entrypoints`, `orchestrator`, `context`, `config`, `persistence`, `passes/`) |
| `features/` | Self-contained slices: `cards`, `lorebook`, `summarization`, `presets`, `documents` |
| `api/` | HTTP layer: FastAPI app factory, routes, Pydantic schemas |

**The one-way rule:** lower layers never import up. When a lower layer needs higher-layer *behavior*, use dependency inversion — the lower layer declares a hook, the higher layer registers an implementation. Example: `database/queries/messages.py` owns `register_workflow_attachment_persister`; `workflows/attachment_cache.py` fills it in.

**Feature slice shape:**
```
features/<name>/
├── __init__.py     # facade re-export
├── contracts.py    # (optional) local TypedDicts — import only core/ + database/models
├── <logic>.py      # pure logic
└── <integration>.py# wiring: reads context, calls logic, persists via database/
```

## Key Files

| File | Role |
|------|------|
| `backend/main.py` | Thin entry: `build_app()` + uvicorn guard |
| `backend/api/__init__.py` | `build_app()`: lifespan, middleware, auto-include routers |
| `backend/api/routes/__init__.py` | `ROUTERS` list — add a file here to register a router |
| `backend/pipeline/entrypoints.py` | Public `handle_*` functions plus the group beat driver — top of the turn lifecycle. The driver owns what a group runs **once per beat** rather than once per speaker: the Director and the pre-writer direction-note step. A beat that arrives with no pinned speaker under `Choose` is a **rest**, not an error — the user's message stands and nobody answers it, the same empty plan the Director may choose under `Auto`. It is decided before any prompt is built, so a rest never pays for a Director call |
| `backend/pipeline/cast.py` | Who speaks: speaking-plan validation and round-robin policy |
| `backend/database/queries/group_members.py` | The durable roster **and** `resolve_cast` — active/tombstoned resolution plus the public/private card projection every prefix builder reads. In the query layer on purpose: `workflows/toolkit.build_offturn_prefix` needs the same answer the turn used and may not import `pipeline/`, and two resolvers would mean two prefixes. `_public_profile` keeps the `extensions.orb` walk but delegates the `Appearance:`/`Role:` join to `character_cards.render_public_profile`, beside the writer that stores it |
| `backend/features/cards/public_profile.py` | The one public-profile drafter, card- and scene-scoped. Owns the tool schema (kept out of `tool_registry.TOOLS` — a profile has no turn phase), the no-secrets **and durable-facts** floor both prompts quote verbatim, the forced-call drain, and the deterministic output contract (one line per field, no braces, ≤30 words). Durable-facts is a cache rule, not a style one: the profile is rendered into the shared cached body, so attire, gear and injuries stay out of it and the transcript carries them instead. A scene draft is **one call per member, never batched**: it carries that member's card and the other members' *names only* |
| `backend/features/cards/sheet_update.py` | The sibling drafter: proposes a rewrite of one member's scene-local sheet from a finished beat, and **applies nothing**. Same posture as `public_profile.py` — forced call, hardcoded summarization hyperparameters, deterministic contract, one call per member and never batched. The contract refuses an empty sheet, a brace, an essay, and a proposal identical to the sheet it was given |
| `backend/pipeline/sheet_update.py` | The turn stage that drives it, in the `world_change` slot and gated on `run_beat_final` so it runs **once per beat**, on the members that actually spoke. Errors are swallowed at both levels: one member's failed call never drops another's proposal, and the stage never costs the user their reply |
| `backend/database/queries/member_sheets.py` | The proposal lifecycle: stage / apply / reject. `base_sheet` is to a proposal what `worlds.content_revision` is to a changeset — the apply re-resolves the member's sheet under `BEGIN IMMEDIATE` and marks the proposal `stale` rather than clobbering a hand edit. Conflicts are decided inside the transaction and raised outside it, since `immediate_tx` rolls back on any exception |
| `backend/inference/group_context.py` | The group character-context projection — the **only** owner of which card fields each mode puts in the shared cached body vs. the speaker's trailing message, plus per-member `{{char}}` scoping. `build_prefix`, `build_writer_content` and the context-size estimator all read it; no pass decides visibility itself |
| `backend/pipeline/orchestrator.py` | `_run_pipeline()`: director→writer→editor coordination |
| `backend/pipeline/state.py` | `TurnState`, `ModelLane`, `_PipelineConfig`, `LorebookTurn` |
| `backend/pipeline/failures.py` | `describe_failure(exc)` → the `error` event's payload; the only place a failure is classified (status class, never provider vocabulary) |
| `backend/pipeline/world_proposal.py` | The Dynamic Worlds turn stage + `reevaluate_changeset`; re-reads every target World immediately before proposing so a proposal always names the latest `content_revision` |
| `backend/inference/lorebook.py` | Lorebook activation **and** the authored/dynamic projection. Every selection and rendering entry point applies `select_effective_entries` itself, so no caller can forget to project |
| `backend/features/lorebook/proposals.py` | Pure: the Agent-facing World catalog + strict validation of a `propose_world_changes` call against the live World(s), and `split_by_world` (one call → one changeset per World). The model never executes CRUD |
| `backend/features/lorebook/changesets.py` | Changeset lifecycle: accept / reject / undo / reset, each through one `BEGIN IMMEDIATE` + revision check |
| `backend/inference/tool_registry.py` | All tool schemas + `TOOLS`/`PRE_WRITER_TOOLS`/`POST_WRITER_TOOLS` |
| `backend/inference/errors.py` | `LLMCallError(httpx.HTTPStatusError)` + `provider_sentence`/`redact` — keeps the provider's own words instead of `raise_for_status()`'s canned line. **Must** stay an `HTTPStatusError` or `RetryPolicy` silently stops retrying |
| `backend/core/text_segmentation.py` | Canonical non-workflow backend sentence/quote policy; sentences never contain line breaks |
| `backend/database/models.py` | TypedDict row contracts (the model layer) |
| `backend/database/schema.py` | `CREATE TABLES` — source of truth for columns |
| `backend/database/preset_schema.py` | Preset policy: `DOMAIN_ROOTS`, `SECRET_COLUMNS`, etc. |
| `frontend/state.js` | Global `S` object — every key declared here; pub/sub bus |
| `frontend/chat.js` | Barrel re-exporting `chat_core/stream/messages/inspector/workflow/conversations/error` |
| `frontend/chat_error.js` | The failed-turn card: `S.turnError` → persistent card with Retry / Details / Copy, painted from `renderMessages()` |
| `frontend/notify.js` | The toast stack — one element and one timer per entry; errors are sticky. `utils.js` re-exports `toast` from here |
| `frontend/sse.js` | THE SSE parser (`sseEvents`, `streamPost`) — only one in the app |
| `frontend/text_segmentation.js` | Canonical non-workflow frontend sentence policy; line breaks are standalone stream units |
| `frontend/workflow_api.js` | Plugin facade ABI v3 — the only import for `frontend/workflows/**` |
| `frontend/group_cast.js` / `group_setup.js` | Pure group identity rendering — cast rail, speaking plan, empty scene, `TURN_MODES` wording, and `recommendContextMode` (the only definition of which context mode a chosen cast should use, keyed on cast size and card weight) (L1) / creation, cast manager, group settings, conversion (L5) |

## Database Schema (summary)

| Table | Purpose |
|-------|---------|
| `settings` | Global singleton (id=1): endpoint refs, enabled_tools (JSON), feature flags, workflow_config |
| `endpoints` | LLM API endpoints; `completion_mode` = `chat`\|`text` |
| `model_configs` | Per-endpoint model params (temp, top_p, max_tokens, system_prompt, …) |
| `conversations` | Chat sessions; `kind=solo|group`, group turn policy, `group_context_mode` (`private`\|`shared`\|`swap`), `group_sheet_updates` (per-scene opt-in to the post-beat sheet pass, off by default), `active_leaf_id` branch leaf; `macro_seed` pins {{random}} on copies |
| `group_members` | Durable ordered group roster; immutable speaker keys, local names, mute/tombstone state, and two scene-local overrides — `public_profile_override` (what the rest of the cast sees) and `card_sheet_override` (what the member reads about itself). Both resolve on `is not None`, so a blanking is not an absence; both default `NULL` to card text, and neither ever writes the card |
| `messages` | Message tree (`parent_id`); group replies carry `speaker_member_id` and request-scoped `beat_id` |
| `character_cards` | V3-spec characters (`ccv3` chunk preferred, `chara` V2 fallback); `avatar_b64`, `world_id`, `persona_lock_id`, `extensions` (card extensions JSON; card-embedded fragments at `orb.fragments`, V3-only card fields parked at `orb.v3`, merged ephemerally in `_load_pipeline_context`) |
| `character_expressions` | Per-character go-emotions expression images |
| `user_personas` | User profiles injected into system prompt |
| `director_state` | Per-conversation Director memory (moods, keywords, progressive_fields, macro_choices) |
| `interactive_fragments` | Dynamic Director parameters; `field_type` = string/array/progressive/feedback/direction_note |
| `mood_fragments` | Named mood presets with prompt/negative_prompt |
| `phrase_bank` | Banned phrase variants for editor audit |
| `conversation_logs` | Per-turn Director audit trail |
| `direction_notes` | Persistent notes across a branch (Director or user-authored) |
| `worlds` / `lorebook_entries` | Lorebook containers + keyword-triggered context entries. `worlds.content_revision` stamps *lore-content* mutations only (never an `enabled` toggle); `lorebook_entries.entry_layer` splits authored rows from the Agent-managed overlay |
| `world_changesets` | Dynamic Worlds proposals + applied history, including the `manual` record a hand delete of an entry leaves; `source_*` message/conversation ids are nullable cross-domain pointers, with denormalised labels so history survives their deletion |
| `member_sheet_proposals` | Staged rewrites of `group_members.card_sheet_override`, one per member per beat; `pending`\|`applied`\|`rejected`\|`stale`. Written by the post-beat pass, decided by the user in Manage cast |
| `documents` | Free-form writing mode documents |
| `user_attachments` | User-uploaded images on messages |
| `workflow_attachments` | LRU-3 byte-budget artifact cache for secondary workflows |

**Important:** SQLite has no boolean — flag columns are `int` (0/1). Always update `schema.py` + `models.py` + `api/schemas.py` (SettingsUpdate) in lockstep when adding columns.

**Migrations run for upgrades only.** Fresh installs get the full schema + seeds from `schema.py`/`bootstrap.py`/`seeds.py`, then the migration chain is *stamped* as applied without running (see `lifespan` in `api/__init__.py` + `stamp_all`). So any schema/data change in a new migration must also land in `schema.py`/`seeds.py`, or fresh installs won't have it. `tests/integration/test_fresh_install_stamping.py` fails if the two diverge.

## Single-Model vs Dual-Model

Controlled by `settings.agent_same_as_writer` (default `true`).

| | Single-model | Dual-model |
|-|--------------|------------|
| Director/Editor endpoint | Writer's endpoint | `settings.agent_endpoint_id` |
| Agent system prompt | Writer's system prompt | `settings.agent_shared_system_prompt` |
| Writer tool schemas | Sent (for byte-parity) | Dropped |
| KV cache | One shared prefix | Two: writer server / agent server |

## Data Contracts (TypedDicts)

`database/models.py` holds all row contracts. Rules:
- TypedDicts label plain `dict(row)` objects — zero runtime cost; use `cast(SomeRow, ...)` at query boundaries.
- Flag columns typed `int`, not `bool`.
- JSON columns typed as decoded shape only on queries that actually decode them.
- `total=False` for conditionally-present keys; use `total=True` base + subclass for required-base + optional-extension.
- Free-form per-workflow JSON slots (`get_workflow_state`, etc.) stay bare `dict` — don't invent contracts for them.
- **Pyright must stay at zero errors.** Widen consumers to `Mapping[str, Any]` / `Sequence[Mapping[str, Any]]` rather than `dict`/`list[dict]`. No `# pyright: ignore` suppressions.

## Preset Engine

`features/presets/engine.py` exports/imports/snapshots the DB as `.db` files. Schema-driven (introspects `PRAGMA`): tables classified as `singleton` / `stable` / `surrogate`; FK graph auto-derives insert order. Policy lives in `database/preset_schema.py` — update it when adding a new entity root or secret column. Drift is caught by `tests/integration/test_preset_schema_coverage.py`.

## Frontend Architecture

Vanilla ES modules, no build step. State in `state.js` (global `S`, all keys declared). Streaming via `sse.js`. All chat generation routes through `runStreamRequest()` in `chat_stream.js`. Plugin modules in `frontend/workflows/**` import only `workflow_api.js` and their own local modules. Workflows own any backend/frontend lexical parsing they need instead of importing application segmentation; shared fixtures pin cross-runtime behavior. Plugin buttons use `registerAction(wid, name, fn)` + `data-wf-action="wid:name"` — never `window.*` or inline `on*`.

Guardrails enforced by `scripts/check_frontend_layers.py` (run via `scripts/lint.sh`): layer import direction, ABI snapshot, plugin-import rule, ratchets for inline handlers and underscore cross-module imports.

## API Endpoints (quick reference)

- **Settings/endpoints/models:** CRUD under `/api/settings`, `/api/endpoints`, `/api/models`
- **Conversations:** CRUD + `/members`, `/members/scene-profile/generate`, `/sheet-proposals` (+ `/{pid}/apply|reject`; apply 409s on a moved sheet, no force-apply), `/convert-to-group`, `/activate`, `/summarize`, `/compress`, `/stop`, `/context-size`
- **Messages:** `/send` (SSE), `/speak`, `/continue`, `/edit`, `/fork-edit`, `/regenerate`, `/super_regenerate`, `/magic_rewrite`, `/switch-branch`, DELETE
- **Characters:** CRUD + `/import` (PNG), `/import-url`, `/browse`, `/export`, `/expressions`, `/public-profile`
- **Fragments/Moods:** `/api/fragments`, `/api/interactive-fragments`
- **Worlds/Lorebook:** CRUD under `/api/worlds/{id}/entries` (`?view=all|authored|effective`) + `/import` + `/export` (standalone `character_book` JSON — V2 shape plus the additive V3 `use_regex`/`selective`/`secondary_keys` keys; `?view=authored` is the default and `effective` is opt-in, as it is for `/api/characters/{id}/export?world_view=`). `POST /api/worlds/deactivate-linked` is the client's boot sweep: a World a character card links to is on loan to whoever is in play, so a fresh page (nobody in play) turns every linked World off before the sidebar paints; floating Worlds are global lore and survive a reload untouched. It stamps neither `updated_at` nor `content_revision`
- **Dynamic Worlds:** `PUT /api/worlds/{id}/dynamic`, `POST /api/worlds/{id}/reset`, and `/api/worlds/{id}/changesets` + `/{cid}/apply|reject|re-evaluate|undo`. Apply takes a per-World lock and a `BEGIN IMMEDIATE` transaction and refuses on a `content_revision` mismatch (`409`) — there is no force-apply and no automatic rebase
- **Phrase bank, Personas, Presets, Documents:** standard CRUD
- **Workflows:** `/api/workflows`, trigger/regenerate/reroll/rehydrate/activate/delete on attachments. `reroll` and `rehydrate` share one `reroll_gen` hook and differ by one declared bit, `RerollGenCtx.replay`: rehydrate reproduces the stored render target, reroll re-renders the same subject on today's configuration. Only regenerate recomposes prompts
- **Image generation:** backend-agnostic readiness/styles/connection/model discovery via the conversation-less workflow QUERY route (`POST /api/workflows/image_gen/query`, `action` = status\|styles\|test\|models\|node_types). Generation uses the conversation-scoped workflow trigger
    - *Routing:* every action routes through `engine/router.get_adapter(config, style)`, which resolves `config.style_source(config, style)` — the **style's** `connection` (`comfy` → `external_comfy`, a cloud provider id → `cloud`, `""` → the stored global `source`/`cloud.provider` for a style predating connection linking). Never on `config["source"]`, which is derived from the *default* style and so is wrong for any replay naming another
    - *Ownership:* a style owns the whole render target (`checkpoint`/`workflow`, `model`, `width`/`height`, `quality`, `reference_sources`); a connection owns only `{api_key, base_url}`
    - *References:* `reference_sources` is **positional** — entry *i* says where the *i*-th slot the target declares draws from, `""` being off. A source may also name a **cast ordinal**: `character` is subject 0 in every slot (two rows on it send the same likeness twice, which is what a two-`LoadImage` solo graph needs), while the *n*-th slot reaching `cast` draws subject *n+1*, counted over the slots that reach it — the one source keyed by ordinal rather than by name in `references.resolve_references`'s per-source cache. `workflows/image_gen/subjects.py` is the **sole owner of subject order** (primary = the anchor's speaker, then the members who spoke in that `beat_id` *up to the anchor*, in roster order; first-person truncates to one) — the history is already cut at the anchor by `_history_through`, so the first reply of a beat has a shorter cast than the last, and that is the invariant, not a bug. Names leave `resolve` distinct (`_disambiguated`): `display_name` has no unique index, and every binding downstream is by name. Both halves of a render read that one list: the slots and the composer. The **analyzer runs above the slots**, not inside `compose_scene` — `composer.addressable_subjects` drops the tail members it left out of frame, so a likeness is never uploaded for someone the prompt does not name (subject 0 is never filtered; a solo slot must always resolve). A `cast` reference records a plain `character:<card id>` origin, so replay never learns a cast exists. How many slots a *cloud* target declares is **derived, never tabulated** — `providers.reference_capacity` reads the reference *encoding*, because that is the only thing that genuinely constrains it: a list encoding carries as many as `MAX_REFERENCE_SLOTS`, a scalar one carries exactly one. There is no per-provider count and no per-model allowlist; both were hand-measured tables over catalogues that grow without us, so both were permanently unfinished, and an unfinished allowlist withholds a capability the user configured and paid for with nothing on screen to say so. Whether *this* model reads what it was sent is the model's to answer, at render time, by refusing — see the refusal ladder below. Every target also publishes that ceiling as `RenderTarget.reference_capacity` (a cloud provider's max, or the image inputs a graph declares) alongside the slots the style actually switched on, and the gap between the two is the only thing that can say *why* somebody in frame got no likeness: below capacity is the style's doing and the user can turn another row on, at capacity is the backend's and they cannot. `hooks._uncovered_note` is that disclosure — it names who was described rather than pictured, and fires only on a **mixed** render, since a render that pictured nobody is `_unfilled_note`'s to describe. A style keeps both backends' answers across a relink, so entries past what the current target declares are stored but inert: read through `config.style_reference_sources` (backend) or `policy.effectiveReferenceSources` (frontend), never the raw list, or a disclosure asks to approve an upload no adapter makes. A ComfyUI graph declares *which* of its inputs load an image (structural, found at import); the style alone says where each draws from, so `engine/graph.enabled_references` is the one place the two meet — and `validate_graph_structure(filled=…)` must be handed that result, not the declared list, or an image widget Orb will *not* overwrite stops being checked for a filename the server actually has. A rehydrate re-keys the *recorded* sources back onto the graph rather than reading today's style, which has been editable since the render
    - *Refusal ladder:* `engine/degrade.py` + the loop in `engine/render.py` are the generic replacement for the capability tables. **Asking is cheaper than tabulating** — every provider measured refuses a bad request *before* rendering and bills nothing for it — so a render sends what the style asked for and degrades on the refusal. Classified on the **shape** of the failure, never a provider's vocabulary: `ImageGenerationError.kind == "request"` plus a message that mentions images. A named limit (*"accepts up to 3 input images"*) is taken at its word, otherwise the references are dropped entirely; bounded at `MAX_DEGRADATIONS` and every rung appends a note. Which references may be dropped is read off the slots' own `required` flag, so a ComfyUI graph — whose image inputs are required — raises untouched with no backend named anywhere in the logic. The prompt no longer depends on a reference having landed: `prompts._reference_instruction` describes **everyone** in full and lets the pictures sharpen a likeness rather than substitute for words, which is what makes guessing high harmless and optimism safe
    - *Action shapes:* `status` answers about the default style, and also returns `sources` (registered adapters) and `providers` (the cloud preset table projected — never a configured key). `node_types` is **ComfyUI-only** and dispatches to `ExternalComfyAdapter` explicitly rather than by any style's connection, because imported graphs are global and the importer stays usable under cloud
- **Local ML:** `/api/local-ml/status`, `/{feature}/download`, `/{feature}/enabled`, plus one route per inference shape (`/slop-score`, `/classify-emotion`); 503 when the extras, the GGUF, or the toggle is missing
- **Inspector:** `/api/conversations/{cid}/director`, `/logs`, `/messages/{id}/director-log`
- **Direction notes:** CRUD under `/api/conversations/{cid}/direction-notes`
- **Storage:** `GET /api/storage?days=N` (what a cleanup would reclaim), `POST /api/storage/cleanup` (age-based artifact eviction + Director-log wipe — payload columns blanked in place, `LOG_KEEP_COLUMNS` whitelist survives — then VACUUM)
- **Other:** `GET /api/stats`, `GET /api/themes`, `POST /api/reset`

## Common Tasks

### Add an HTTP route
Drop `api/routes/<feature>.py` with `router = APIRouter()`, append to `ROUTERS` in `api/routes/__init__.py`. No edit to `main.py`.

### Add a secondary workflow
See [docs/architecture/secondary-workflow.md](docs/architecture/secondary-workflow.md) — new folder + `register_workflow`/`subscribe` in `workflows/__init__.py`.

### Format and lint
```sh
./scripts/format_backend.sh  # Ruff, 128-char lines
./scripts/format_frontend.sh # Biome
./scripts/lint.sh            # Lint + static checks
./scripts/tests.sh all       # Full test suite
```

## Multi-user and tab management

We do NOT support multi users nor multiple open tabs at the same time. tabLock.js stops this from happening.

## Golden Rules for Codebase health
1. Symmetry
2. Separation of Concerns
3. Robustness of Data Contracts
