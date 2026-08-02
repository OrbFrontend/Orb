# AGENTS.md — Orb Codebase Guide

> Keep this current when architecture changes — it's the single source of truth.

## Project Overview

Orb is an **agentic AI roleplay/writing frontend**: Python/FastAPI backend, vanilla JS frontend. Orchestrates a multi-pass LLM pipeline (Director → Writer → Editor). Characters are PNG cards (V3 spec, with V2/V1 fallback). Conversations are branching message trees with lorebooks, mood/interactive fragments, and personas.

**Stack:** Python 3.11+, FastAPI, aiosqlite, SQLite, vanilla JS (no framework), uvicorn

## Architecture

Pipeline passes: **Director** (optional, pre-writer) → **Writer** (streams output) → **Editor** (optional, post-writer auditor/rewriter).

- **Cross-pass KV caching:** All passes share one byte-identical prefix (same system prompt, history, tool schemas). Read [docs/architecture/kv-cache.md](docs/architecture/kv-cache.md) before touching prompt assembly, pass ordering, or tool schemas.
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

`database/` may also import `core/`. `features/lorebook/` imports only `core/`.

| Layer | Purpose |
|-------|---------|
| `core/` | Dependency-free kernel: `domain_types`, `llm_types`, `macros`, `locks`, `utils` |
| `database/` | aiosqlite foundation: schema, migrations, queries, models (TypedDicts) |
| `inference/` | LLM transport + prompt/tool assembly (`client`, `cached_call`, `prompt_builder`, `tool_registry`) |
| `analysis/` | Pure prose-quality detection: `audit.py` + detectors; shared by editor + workflows |
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
| `backend/pipeline/entrypoints.py` | 5 public `handle_*` functions — top of the turn lifecycle |
| `backend/pipeline/orchestrator.py` | `_run_pipeline()`: director→writer→editor coordination |
| `backend/pipeline/state.py` | `TurnState`, `ModelLane`, `_PipelineConfig`, `LorebookTurn` |
| `backend/inference/tool_registry.py` | All tool schemas + `TOOLS`/`PRE_WRITER_TOOLS`/`POST_WRITER_TOOLS` |
| `backend/database/models.py` | TypedDict row contracts (the model layer) |
| `backend/database/schema.py` | `CREATE TABLES` — source of truth for columns |
| `backend/database/preset_schema.py` | Preset policy: `DOMAIN_ROOTS`, `SECRET_COLUMNS`, etc. |
| `frontend/state.js` | Global `S` object — every key declared here; pub/sub bus |
| `frontend/chat.js` | Barrel re-exporting `chat_core/stream/messages/inspector/workflow/conversations` |
| `frontend/sse.js` | THE SSE parser (`sseEvents`, `streamPost`) — only one in the app |
| `frontend/workflow_api.js` | Plugin facade ABI v2 — the only import for `frontend/workflows/**` |

## Database Schema (summary)

| Table | Purpose |
|-------|---------|
| `settings` | Global singleton (id=1): endpoint refs, enabled_tools (JSON), feature flags, workflow_config |
| `endpoints` | LLM API endpoints; `completion_mode` = `chat`\|`text` |
| `model_configs` | Per-endpoint model params (temp, top_p, max_tokens, system_prompt, …) |
| `conversations` | Chat sessions; `active_leaf_id` selects branch leaf; `macro_seed` pins {{random}} on checkpoint/compress copies |
| `messages` | Message tree (`parent_id`); `role`, `content`, `progressive_fields`, `workflow_state` |
| `character_cards` | V3-spec characters (`ccv3` chunk preferred, `chara` V2 fallback); `avatar_b64`, `world_id`, `persona_lock_id`, `extensions` (card extensions JSON; card-embedded fragments at `orb.fragments`, V3-only card fields parked at `orb.v3`, merged ephemerally in `_load_pipeline_context`) |
| `character_expressions` | Per-character go-emotions expression images |
| `user_personas` | User profiles injected into system prompt |
| `director_state` | Per-conversation Director memory (moods, keywords, progressive_fields, macro_choices) |
| `interactive_fragments` | Dynamic Director parameters; `field_type` = string/array/progressive/feedback/direction_note |
| `mood_fragments` | Named mood presets with prompt/negative_prompt |
| `phrase_bank` | Banned phrase variants for editor audit |
| `conversation_logs` | Per-turn Director audit trail |
| `direction_notes` | Persistent notes across a branch (Director or user-authored) |
| `worlds` / `lorebook_entries` | Lorebook containers + keyword-triggered context entries |
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

Vanilla ES modules, no build step. State in `state.js` (global `S`, all keys declared). Streaming via `sse.js`. All chat generation routes through `runStreamRequest()` in `chat_stream.js`. Plugin modules in `frontend/workflows/**` import only `workflow_api.js`. Plugin buttons use `registerAction(wid, name, fn)` + `data-wf-action="wid:name"` — never `window.*` or inline `on*`.

Guardrails enforced by `scripts/check_frontend_layers.py` (run via `scripts/lint.sh`): layer import direction, ABI snapshot, plugin-import rule, ratchets for inline handlers and underscore cross-module imports.

## API Endpoints (quick reference)

- **Settings/endpoints/models:** CRUD under `/api/settings`, `/api/endpoints`, `/api/models`
- **Conversations:** CRUD + `/summarize`, `/compress`, `/stop`, `/context-size`
- **Messages:** `/send` (SSE), `/continue`, `/edit`, `/fork-edit`, `/regenerate`, `/super_regenerate`, `/magic_rewrite`, `/switch-branch`, DELETE
- **Characters:** CRUD + `/import` (PNG), `/import-url`, `/browse`, `/export`, `/expressions`
- **Fragments/Moods:** `/api/fragments`, `/api/interactive-fragments`
- **Worlds/Lorebook:** CRUD under `/api/worlds/{id}/entries` + `/import` + `/export` (standalone `character_book` JSON — V2 shape plus the additive V3 `use_regex`/`selective`/`secondary_keys` keys)
- **Phrase bank, Personas, Presets, Documents:** standard CRUD
- **Workflows:** `/api/workflows`, trigger/regenerate/reroll/rehydrate/activate/delete on attachments
- **Image generation:** backend-agnostic readiness/styles/connection/model discovery via the conversation-less workflow QUERY route (`POST /api/workflows/image_gen/query`, `action` = status\|styles\|test\|models\|node_types); every action routes through `engine/router.get_adapter(config)` on `config["source"]` (`external_comfy` | `cloud`). `status` also returns `sources` (registered adapters) and `providers` (the cloud preset table projected — never a configured key). `node_types` is **ComfyUI-only** and dispatches to `ExternalComfyAdapter` explicitly rather than by active source, because imported graphs are global and the importer stays usable under cloud. Generation uses the conversation-scoped workflow trigger
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

## Context Management

Full active message path sent every turn — no automatic truncation. Manual compress: `POST /summarize` → review → `POST /compress` → new conversation with summary + last N messages.

## Golden Rules for Codebase health
1. Symmetry
2. Separation of Concerns
3. Robustness of Data Contracts
