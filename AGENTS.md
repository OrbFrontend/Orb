# AGENTS.md — Orb Codebase Guide

> Keep this current when architecture changes — it's the single source of truth.

## Project Overview

Orb is an **agentic AI roleplay/writing frontend**: Python/FastAPI backend, vanilla JS frontend. Orchestrates a multi-pass LLM pipeline (Director → Writer → Editor). Characters are PNG cards (V3 spec, with V2/V1 fallback). Conversations are branching message trees with lorebooks, mood/interactive fragments, and personas.

**Stack:** Python 3.11+, FastAPI, aiosqlite, SQLite, vanilla JS (no framework), uvicorn. Dulwich powers Git installation of community extensions and is imported lazily — a deployment without it boots, reconciles, and installs `.orbext` archives normally.

## Architecture

Pipeline passes: **Director** (optional, pre-writer) → **Writer** (streams output) → **Editor** (optional, post-writer auditor/rewriter).

- **Cross-pass KV caching:** All passes share one byte-identical prefix (same system prompt, history, tool schemas). Read [docs/architecture/kv-cache.md](docs/architecture/kv-cache.md) before touching prompt assembly, pass ordering, or tool schemas.
- **Secondary workflows:** Pluggable hooks (pre/post pipeline, on-demand). Full reference: [docs/architecture/secondary-workflow.md](docs/architecture/secondary-workflow.md).
- **Registry snapshots:** Workflow and contributed fragment-type lookups resolve against an immutable `RegistrySnapshot` (built-in base + community overlay). A turn captures **one** in `_load_pipeline_context` and threads it everywhere; never re-read the global registry mid-turn.
- **Community extensions:** Untrusted declarative packages, a separate trust tier from built-in workflows. `.orbext` archives and HTTPS Git URLs are compiled to immutable records, stored content-addressed under `data/extensions/objects/<digest>/`, and published as the registry's community overlay. Compiled flows run through a bounded interpreter with a staged effect transaction; compiled views render through a host-owned component renderer that only ever writes `textContent`. **The unit of permission is a `(capability, parameter)` grant, and every fact about one — consent copy, data class, emphasis, admissible parameter values, gated resource, prerequisites — lives in `CAPABILITY_SPECS` (`features/extensions/contracts/capabilities.py`); the consent table, loud/data-reading sets, resource map, prerequisite map, `UI_SLOTS`, and the `Permission` model's parameters are all derived from it. Add a grant there, not in six places.** Design + phasing: [docs/architecture/community-extensions.md](docs/architecture/community-extensions.md).
- **Extension egress:** *All* outbound traffic a package causes — a flow's `http.request` and the Git installer's fetch — goes through `features/extensions/network.py`. It derives the canonical origin from the URL, checks it against the live grant set, validates every resolved address, and pins one for the connection while `Host` and TLS SNI keep the real hostname. Redirects are followed by Orb, revalidated per hop, and drop package headers when they cross origins. Secrets are substituted inside that module and nowhere else, and responses are scanned for them before becoming flow values. Never add a second HTTP path for package-influenced URLs.
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
| `core/` | Small, closed-by-default kernel of canonical host invariants, value contracts, and process primitives |
| `database/` | aiosqlite foundation: schema, migrations, queries, models (TypedDicts) |
| `inference/` | LLM transport + prompt/tool assembly (`client`, `cached_call`, `prompt_builder`, `tool_registry`) |
| `analysis/` | Pure prose-quality detection: `audit.py` + detectors; shared by editor + workflows |
| `workflows/` | Plugin registry + shipped workflows (TTS, image generation, format_consistency) |
| `pipeline/` | Director→Writer→Editor turn engine (`entrypoints`, `orchestrator`, `context`, `config`, `persistence`, `passes/`) |
| `features/` | Self-contained slices: `cards`, `lorebook`, `summarization`, `presets`, `documents`, `extensions` |
| `api/` | HTTP layer: FastAPI app factory, routes, Pydantic schemas |

### Core admission rule

`core/` is **not** a home for code that is merely shared, pure, or convenient.
It is closed by default. A new core symbol or module is admissible only when all
of these are true:

1. It is a canonical host invariant/value contract or a process-coordination
   primitive, not a feature workflow.
2. It depends only on the explicitly approved standard-library surface and
   sibling `core/` modules. It performs no database, filesystem, network,
   environment/config, HTTP, framework, or registry access.
3. It must have one identity across owners that cannot legally import one
   another, or it must sit below `database/` to enforce a write invariant at the
   single persistence path. "Two callers" alone is not sufficient.
4. It operates only on values supplied by its caller. Fetching, persistence,
   projection, consent, and orchestration remain with their owning layer.
5. Its vocabulary and policy belong to Orb itself. Extensions and workflows
   conform to the host rule; their requirements never define a core contract.

Prefer a feature-local module whenever the rule has a single feature owner. If
a lower layer needs higher-layer behavior, use dependency inversion instead of
moving that behavior into `core/`. The closed module/import inventory in
`tests/unit/test_import_layering.py` makes additions an explicit architecture
decision.

The current domain-specific admissions are deliberately narrow:

- `personas.py` owns only effective-persona precedence over already-loaded
  mappings. Persona CRUD, loading, and projection stay outside `core/`.
- `tags.py` owns only the canonical normalization of an already-supplied
  character-tag list. Card CRUD, import, filtering, and extension operations
  stay outside `core/`.

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
| `backend/pipeline/fragment_types.py` | Per-turn fragment descriptor resolution, prior preparation, reduction, carry-forward, and Writer rendering |
| `backend/inference/tool_registry.py` | All tool schemas + `TOOLS`/`PRE_WRITER_TOOLS`/`POST_WRITER_TOOLS` |
| `backend/workflows/fragment_types.py` | Built-in/contributed fragment-type runtime contracts and shared reducer budget |
| `backend/database/models.py` | TypedDict row contracts (the model layer) |
| `backend/database/schema.py` | `CREATE TABLES` — source of truth for columns |
| `backend/database/preset_schema.py` | Preset policy: `DOMAIN_ROOTS`, `SECRET_COLUMNS`, etc. |
| `frontend/state.js` | Global `S` object — every key declared here; pub/sub bus |
| `frontend/chat.js` | Barrel re-exporting `chat_core/stream/messages/inspector/workflow/conversations` |
| `frontend/sse.js` | THE SSE parser (`sseEvents`, `streamPost`) — only one in the app |
| `frontend/workflow_api.js` | Plugin facade ABI v2 — the only import for `frontend/workflows/**` |
| `frontend/extension_manager.js` | Orb-owned community-extension manager: install (file or Git), consent, permissions, write-only secrets, update, rollback, purge. DOM creation + `textContent` only — package strings never become markup, handlers, or attributes |
| `frontend/extension_renderer.js` | THE renderer for community component trees, including fragment config/value views. Tokenized styling, media by reference, node-built Markdown; no package string reaches markup, a class, a URL, or a handler |
| `frontend/extension_commands.js` | Host command model (built-in band + community band), slot placement, workspace/view lifecycle, the fixed effect→refetch map, and the renderer-driven library sweep |

## Database Schema (summary)

| Table | Purpose |
|-------|---------|
| `settings` | Global singleton (id=1): endpoint refs, enabled_tools (JSON), feature flags, workflow_config |
| `endpoints` | LLM API endpoints; `completion_mode` = `chat`\|`text` |
| `model_configs` | Per-endpoint model params (temp, top_p, max_tokens, system_prompt, …) |
| `conversations` | Chat sessions; `active_leaf_id` selects branch leaf; `macro_seed` pins {{random}} on checkpoint/compress copies |
| `messages` | Message tree (`parent_id`); `role`, `content`, `progressive_fields`, `workflow_state` |
| `character_cards` | V3-spec characters (`ccv3` chunk preferred, `chara` V2 fallback); `avatar_b64`, `world_id`, `persona_lock_id`, `extensions` (card extensions JSON; card-embedded fragments at `orb.fragments`, V3-only card fields parked at `orb.v3`, merged ephemerally in `_load_pipeline_context`). **`tags` is normalized on every update** by the canonical host policy in `core/tags.py` (trim, clip, drop empties, case-insensitive dedupe, per-tag and per-card caps) — `update_character_card` is the single write path, and extension `card.tags.set` conforms to it. Import is deliberately *not* normalized, and existing rows are never backfilled |
| `character_expressions` | Per-character go-emotions expression images |
| `user_personas` | User profiles injected into system prompt |
| `director_state` | Per-conversation Director memory (moods, keywords, progressive_fields, macro_choices) |
| `interactive_fragments` | Dynamic Director parameters; `field_type` = string/array/progressive/feedback/direction_note, or `<extension-id>:<type-id>` for an extension-contributed type; `type_config` (JSON) holds that type's per-instance config |
| `mood_fragments` | Named mood presets with prompt/negative_prompt |
| `phrase_bank` | Banned phrase variants for editor audit |
| `conversation_logs` | Per-turn Director audit trail, including persisted sanitized fragment diagnostics |
| `direction_notes` | Persistent notes across a branch (Director or user-authored) |
| `worlds` / `lorebook_entries` | Lorebook containers + keyword-triggered context entries |
| `documents` | Free-form writing mode documents |
| `user_attachments` | User-uploaded images on messages |
| `workflow_attachments` | LRU-3 byte-budget artifact cache for secondary workflows |
| `extension_packages` / `extension_revisions` / `extension_secrets` | Installed community extensions: source + grants + load status, per-digest manifests, write-only secrets. **`LOCAL_ONLY_TABLES`** — stripped from shareable presets, kept in full local snapshots |

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

`features/presets/engine.py` exports/imports/snapshots the DB as `.db` files. Schema-driven (introspects `PRAGMA`): tables classified as `singleton` / `stable` / `surrogate`; FK graph auto-derives insert order. Policy lives in `database/preset_schema.py` — update it when adding a new entity root, secret column, or local-only table. `LOCAL_ONLY_TABLES` is for live user data that must not travel between machines (never `EXCLUDED_TABLES`, which is bookkeeping): stripped from shareable presets, retained by full local snapshots. Drift is caught by `tests/integration/test_preset_schema_coverage.py`.

## Frontend Architecture

Vanilla ES modules, no build step. State in `state.js` (global `S`, all keys declared). Streaming via `sse.js`. All chat generation routes through `runStreamRequest()` in `chat_stream.js`. Plugin modules in `frontend/workflows/**` import only `workflow_api.js`. `workflow_loader.js` dynamically imports a manifest entry only when `extension_policy.js` says it is a `trusted_module`; community entries are declarative data and never reach `import()`. Plugin buttons use `registerAction(wid, name, fn)` + `data-wf-action="wid:name"` — never `window.*` or inline `on*`.

**Menus and extension slots are a host command model.** The composer burger and the mobile action menu render from `extension_commands.js`: `app.js` supplies the built-in entries (with optional `visible`/function-`label`), and enabled extension placements form a second band ordered by extension id. Other slots are empty `[data-ext-slot="<slot>"]` containers the host puts in its own markup (`index.html`, the per-message template in `chat_core.js`, the library card in `library_browser.js`); `renderPanelSlots(root)` fills them. Extensions never insert DOM nodes and never name a selector.

Guardrails enforced by `scripts/check_frontend_layers.py` (run via `scripts/lint.sh`): layer import direction, ABI snapshot, plugin-import rule, ratchets for inline handlers and underscore cross-module imports.

## API Endpoints (quick reference)

- **Settings/endpoints/models:** CRUD under `/api/settings`, `/api/endpoints`, `/api/models`
- **Conversations:** CRUD + `/summarize`, `/compress`, `/stop`, `/context-size`
- **Messages:** `/send` (SSE), `/continue`, `/edit`, `/fork-edit`, `/regenerate`, `/super_regenerate`, `/magic_rewrite`, `/switch-branch`, DELETE
- **Characters:** CRUD + `/import` (PNG), `/import-url`, `/browse`, `/export`, `/expressions`
- **Fragments/Moods:** `/api/fragments`, `/api/interactive-fragments`, `/api/interactive-fragment-types` (captured host/contributed descriptor catalog)
- **Worlds/Lorebook:** CRUD under `/api/worlds/{id}/entries` + `/import` + `/export` (standalone `character_book` JSON — V2 shape plus the additive V3 `use_regex`/`selective`/`secondary_keys` keys)
- **Phrase bank, Personas, Presets, Documents:** standard CRUD
- **Workflows:** `/api/workflows`, trigger/regenerate/reroll/rehydrate/activate/delete on attachments
- **Extensions:** `/api/extensions` (catalog + orphaned data), `/{id}` detail, two-phase `inspect-file` → `install` (local `.orbext`) and `inspect` → `install` (HTTPS Git URL), `inspect-update` / `inspect-update-git` → `update`, `inspect-rollback` → `rollback`, `/{id}/enabled`, `PUT /{id}/permissions`, `PUT /{id}/secrets` (write-only; reads return presence only), `DELETE /{id}` (uninstall preserves namespaced data), `POST /{id}/purge-data` (preview, then confirm with the preview's token). Content routes: `POST /{id}/actions/{action}`, `GET /{id}/views/{view}`, `GET /{id}/resources/{resource}` (`conversation.tree`, `library.cards`, `lorebook.entries`, `direction.notes`, `persona` — each behind its own grant, opaque cursors), `GET /{id}/assets/{path}` (exact compiled asset key, never a path join), `PUT /{id}/state` (host-generated write for a bound form). Every response uses the fixed effect envelope `{data, effects, runtime_generation}`
- **Image generation:** external-ComfyUI readiness/styles/connection/model discovery via the conversation-less workflow QUERY route (`POST /api/workflows/image_gen/query`, `action` = status\|styles\|test\|models\|node_types); generation uses the conversation-scoped workflow trigger
- **Inspector:** `/api/conversations/{cid}/director`, `/logs`, `/messages/{id}/director-log`
- **Direction notes:** CRUD under `/api/conversations/{cid}/direction-notes`
- **Storage:** `GET /api/storage?days=N` (what a cleanup would reclaim), `POST /api/storage/cleanup` (age-based artifact eviction + Director-log wipe — payload columns blanked in place, `LOG_KEEP_COLUMNS` whitelist survives — then VACUUM)
- **Other:** `GET /api/stats`, `GET /api/themes`, `POST /api/reset`

## Common Tasks

### Add an HTTP route
Drop `api/routes/<feature>.py` with `router = APIRouter()`, append to `ROUTERS` in `api/routes/__init__.py`. No edit to `main.py`.

### Add a model-callable tool
1. Define schema in `inference/tool_registry.py`
2. Register in `TOOLS` with `choice` + `schema`; add to `PRE_WRITER_TOOLS` or `POST_WRITER_TOOLS`
3. Handle the tool call in the relevant pass
4. Add to `settings.enabled_tools` and the frontend `TOOL_DEFS` panel

### Add a feature flag (non-tool toggle)
1. Add `INTEGER NOT NULL DEFAULT 0` column to `database/schema.py`, `seeds.py`, and a numbered migration
2. Add to `allowed` list in `database/queries/settings.py` and `SettingsUpdate` in `api/schemas.py`
3. Read from `settings` (not `enabled_tools`) in the pipeline

### Install a community extension (dev loop)
Zip the package directory (`zip -r pkg.orbext my-extension/` — one wrapping directory is stripped), then use the Extensions sidebar section, or `POST /api/extensions/inspect-file` followed by `POST /api/extensions/install` with the returned token and the exact permission `value` objects the inspection listed. `POST /api/extensions/inspect` takes `{url, ref, allow_local}` instead and does the same thing from a Git repository (`allow_local` is required for a repository on your own machine or LAN). The compiler derives the real requirement set from the flows/views; `requires.operations`, `requires.components`, and `permissions` must *cover* it or the package is rejected.

### Add a secondary workflow
See [docs/architecture/secondary-workflow.md](docs/architecture/secondary-workflow.md) — new folder + `register_workflow`/`subscribe` in `workflows/__init__.py`. A `POST_PIPELINE` binding must also pick its `stage=`: `TRANSFORM` if it rewrites the draft, `OBSERVE` if it only consumes the final text.

### Add a theme
Create `frontend/themes/your_theme.css` using CSS custom properties on `[data-theme="your_theme"]`. Auto-listed by `GET /api/themes`.

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
