# AGENTS.md — Orb Codebase Guide

> Keep this current when architecture changes — it's the single source of truth.

## Project Overview

Orb is an **agentic AI roleplay/writing frontend**: Python/FastAPI backend, vanilla JS frontend. Orchestrates a multi-pass LLM pipeline (Director → Writer → Editor). Characters are PNG cards (V3 spec, with V2/V1 fallback). Conversations are branching message trees with lorebooks, mood/interactive fragments, and personas.

**Stack:** Python 3.11+, FastAPI, aiosqlite, SQLite, vanilla JS (no framework), uvicorn

## Architecture

Pipeline passes: **Director** (optional, pre-writer) → **Writer** (streams output) → **Editor** (optional, post-writer auditor/rewriter).

- **Cross-pass KV caching:** All passes share one byte-identical prefix (same system prompt, history, tool schemas). Read [docs/architecture/kv-cache.md](docs/architecture/kv-cache.md) before touching prompt assembly, pass ordering, or tool schemas. Group chats are the one place a prefix may legitimately vary *within* an exchange: under `group_context_mode = 'swap'` the active card sits before history, so each speaker gets its own frozen base and the Director keeps a neutral one.
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
| `inference/` | LLM transport + prompt/tool assembly (`client`, `cached_call`, `prompt_builder`, `tool_registry`); local models in `local_ml.py` (in-process GGUF classifiers) and `prose_rewriter/` (a supervised `llama-server` child — Orb's only managed subprocess; teardown lives in the app `lifespan`) |
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

## Database Schema (summary)

| Table | Purpose |
|-------|---------|
| `settings` | Global singleton (id=1): endpoint refs, enabled_tools (JSON), feature flags, workflow_config |
| `endpoints` | LLM API endpoints; `completion_mode` = `chat`\|`text` |
| `model_configs` | Per-endpoint model params (temp, top_p, max_tokens, system_prompt, …) |
| `conversations` | Chat sessions; `kind=solo|group`, group turn policy, `group_context_mode` (`private`\|`shared`\|`swap`), `group_sheet_updates` (per-scene opt-in to the post-exchange sheet pass, off by default), `active_leaf_id` branch leaf; `macro_seed` pins {{random}} on copies |
| `group_members` | Durable ordered group roster; immutable speaker keys, local names, mute/tombstone state, and two scene-local overrides — `public_profile_override` (what the rest of the cast sees) and `card_sheet_override` (what the member reads about itself). Both resolve on `is not None`, so a stored `""` blanks the field rather than falling back (Manage cast coerces an empty box to `NULL`, so only the API reaches that case today); both default `NULL` to card text, and neither ever writes the card |
| `messages` | Message tree (`parent_id`); group replies carry `speaker_member_id` and request-scoped `exchange_id` |
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
| `member_sheet_proposals` | Staged rewrites of `group_members.card_sheet_override`, **at most one `pending` per member**; `pending`\|`applied`\|`rejected`\|`stale`. Written by the post-exchange pass, decided by the user on Scene setup's Cast tab; retired with the member when the roster drops it |
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
- **Conversations:** CRUD + `/members`, `/members/scene-profile/generate`, `/sheet-proposals` (`?status=` defaults to the review set — `pending` + `stale`; `all` is the history view. + `/{pid}/apply|reject`; apply 409s on a moved sheet, a decided proposal, or a member that left the scene — no force-apply), `/convert-to-group`, `/activate`, `/summarize`, `/compress`, `/stop`, `/context-size`
- **Messages:** `/send` (SSE), `/speak`, `/continue`, `/edit`, `/fork-edit`, `/regenerate`, `/super_regenerate`, `/magic_rewrite`, `/switch-branch`, DELETE
- **Characters:** CRUD + `/import` (PNG), `/import-url`, `/browse`, `/export`, `/expressions`, `/public-profile`
- **Fragments/Moods:** `/api/fragments`, `/api/interactive-fragments`
- **Worlds/Lorebook:** CRUD under `/api/worlds/{id}/entries` (`?view=all|authored|effective`) + `/import` + `/export` (standalone `character_book` JSON — V2 shape plus the additive V3 `use_regex`/`selective`/`secondary_keys` keys; `?view=authored` is the default and `effective` is opt-in, as it is for `/api/characters/{id}/export?world_view=`). `POST /api/worlds/deactivate-linked` is the client's boot sweep: a World a character card links to is on loan to whoever is in play, so a fresh page (nobody in play) turns every linked World off before the sidebar paints; floating Worlds are global lore and survive a reload untouched. It stamps neither `updated_at` nor `content_revision`
- **Dynamic Worlds:** `PUT /api/worlds/{id}/dynamic`, `POST /api/worlds/{id}/reset`, and `/api/worlds/{id}/changesets` + `/{cid}/apply|reject|re-evaluate|undo`. Apply takes a per-World lock and a `BEGIN IMMEDIATE` transaction and refuses on a `content_revision` mismatch (`409`) — there is no force-apply and no automatic rebase
- **Phrase bank, Personas, Presets, Documents:** standard CRUD
- **Workflows:** `/api/workflows`, trigger/regenerate/reroll/rehydrate/activate/delete on attachments. `reroll` and `rehydrate` share one `reroll_gen` hook and differ by one declared bit, `RerollGenCtx.replay`: rehydrate reproduces the stored render target, reroll re-renders the same subject on today's configuration. Only regenerate recomposes prompts
- **Image generation:** backend-agnostic readiness/styles/connection/model discovery via the conversation-less workflow QUERY route (`POST /api/workflows/image_gen/query`, `action` = status\|styles\|test\|models\|node_types). Generation uses the conversation-scoped workflow trigger
- **Local ML:** `/api/local-ml/status`, `/{feature}/download` (optional `{"variant"}`), `/{feature}/enabled`, `/{feature}/config` (per-feature JSON blob — the prose rewriter's variant + GPU flag), `DELETE /{feature}/model?variant=`, `/prose_rewriter/runtime` (fetch the llama-server binary), plus one route per inference shape (`/slop-score`, `/classify-emotion`); 503 when the extras, the GGUF, or the toggle is missing. `deps_ok` is per feature — the prose rewriter's `runtime="llama_server"` needs only `huggingface_hub`, not `llama-cpp-python`
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
