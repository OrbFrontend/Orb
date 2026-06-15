# Backend Re-Architecture: Layered Core + Vertical Feature Slices

## Context

`backend/` has **17 substantive `.py` files** (18 counting an empty `__init__.py`) sitting
flat at the top level, mixing five unrelated concerns in one directory.
`orchestrator.py` (1903 LOC) lives next to `card_downloader.py`, `summarizer.py`,
`tavern_cards.py`, and `presets.py` — feature modules that have nothing to do with
pipeline orchestration. Meanwhile `main.py` (2598 LOC, **93 routes**, **108 top-level
`def`s**) is the single file every new HTTP-facing feature must edit.

The irony is that the codebase **already contains the architecture it needs** — three
subpackages model it perfectly:

- `database/` — a contracts layer (`models.py`), strict one-way "never import up" rule, facade `__init__.py`.
- `passes/` — per-pass folders with a `pass` (raw loop) / `stage` (wiring) split and facade re-exports.
- `workflows/` — the gold standard: a **plugin registry** where a new feature is a new folder plus *one* `register_workflow(...)` + `subscribe(...)` block in `workflows/__init__.py`, fronted by a stable author surface (`toolkit.py`) and read-only boundary contracts (`contracts.py`).

The top level never got this treatment. This plan extends the existing precedent to
the **whole backend** so it satisfies the three stated requirements:

1. **Symmetry** — every layer package and every feature slice has the same internal shape and a facade `__init__.py`.
2. **Separation of concerns** — named layers (shared kernel → inference / analysis → workflows → pipeline / features → api) plus isolated feature slices, with a one-way downward dependency rule.
3. **Sensible shared data classes** — one contracts module per layer, all depending only *downward* toward a shared kernel; no layer reaches "up" for a shape.

**Goal:** a new feature creates its own folder (and at most drops one router file / one registration line) instead of editing `orchestrator.py` or `main.py`.

**Decisions locked in:**
- Full layered reorg (all 17 modules relocated) **and** split `main.py`'s 93 routes into an `api/routes/` router package.
- **Extract a new `analysis/` layer** (the prose-quality auditor + its detectors). This is forced by a real dependency: `workflows/toolkit.py` imports `passes.editor.audit`, so moving `passes/` wholesale into `pipeline/` would create a `pipeline ↔ workflows` package cycle (`orchestrator → workflows → pipeline.passes.audit`). The auditor is pure (depends only on `database.models`) and is genuinely shared between the editor pass and workflow tools, so it belongs in a layer *below both*. This mirrors the existing precedent where `PhraseGroup` was moved *down* into `database/models.py` to kill the last upward import.

---

## Target Architecture

**Named pattern:** *Layered architecture with a Shared Kernel + Vertical Feature Slices*, with a *plugin registry* for extensions (the `workflows/` pattern, generalized) and *ports-and-adapters* at the inference edge (`endpoint_profiles`, the TTS `engine/` adapters).

Dependency direction is strictly **downward**:

```
api  →  {pipeline, features}  →  workflows  →  {inference, analysis}  →  core
                                                                ↘        ↘
                                                              database  →  core
```

- `core/` is the dependency-free leaf (everything may import it, it imports nothing).
- `database/` is a foundation that **may read `core`** (e.g. `queries/workflow_attachments.py` imports `utils.scrub_log`) and is read by every layer above, but **never imports up** (rule already in force).
- `analysis/` depends only on `database.models` (+ stdlib); it does **not** depend on `core` or `inference`. It sits below `workflows` and `pipeline`, parallel to `inference`.
- `inference/` depends only on `core`.

> **`workflows/` is not a free-floating sibling — it sits *below* `pipeline/` and *above* `inference`/`analysis`.** `orchestrator.py` imports `workflows` (`from .workflows import …`), and `workflows` imports only `inference`, `analysis`, `core`, and `database` — all downward. Its place in the one-way order is fixed: `pipeline → workflows → {inference, analysis} → {core, database}`. (Before the `analysis/` extraction, `workflows/toolkit.py → passes.editor.audit` was the one back-edge that broke this; the extraction is what makes the claim true.)

```
backend/
├── main.py                 # THIN: `from .api import build_app; app = build_app()` + uvicorn __main__ guard
├── api/                    # HTTP layer — the route half of old main.py
│   ├── __init__.py         # build_app(): FastAPI, lifespan, no_cache middleware, auto-include routers, static mount (LAST)
│   ├── deps.py             # ALL cross-route shared state/helpers (see Phase 6)
│   ├── schemas.py          # Pydantic request/response models (SettingsUpdate, etc.)
│   └── routes/
│       ├── __init__.py     # ROUTERS list (or pkgutil auto-discovery) consumed by build_app()
│       ├── settings.py  endpoints.py  conversations.py  messages.py
│       ├── characters.py  fragments.py  worlds.py  phrase_bank.py
│       ├── personas.py  presets.py  workflows.py  stats.py  misc.py
├── core/                   # SHARED KERNEL — dependency-free leaves; imports nothing upward
│   ├── __init__.py         # re-exports the kernel surface
│   ├── llm_types.py        # wire contracts (ChatMessage, ToolCall, ContentPart, …)
│   ├── macros.py           # dependency-free leaf (string/message transforms)
│   ├── locks.py            # process-level asyncio locks (incl. the _workflow_state_locks registry)
│   └── utils.py            # estimate_tokens, scrub_log, build_multimodal_content, extract_hyperparams (imports core.llm_types only)
├── database/               # UNCHANGED — already exemplary (may read core; never imports up)
├── analysis/               # NEW shared layer — pure prose-quality detection; deps: database.models + stdlib only
│   ├── __init__.py         # facade: run_audit, format_report, AuditReport, AUDIT_TYPES + public result types
│   ├── audit.py            # was passes/editor/audit.py (run_audit, format_report, AuditReport)
│   └── detectors/
│       ├── __init__.py
│       ├── slop_detector.py        text_segmentation.py     contrastive_negation.py
│       ├── opening_monotony.py     phrase_repetition.py
│       ├── structural_repetition.py  template_repetition.py
├── inference/              # LLM transport + prompt assembly (ports & adapters); depends only on core
│   ├── __init__.py         # facade re-export
│   ├── client.py           # was llm_client.py (imports endpoint_profiles)
│   ├── endpoint_profiles.py# provider adapter — a LEAF here (imported BY client.py)
│   ├── cached_call.py      # CachedBase + cached_complete (imports kv_tracker)
│   ├── kv_tracker.py
│   ├── prompt_builder.py   # imports core (macros, llm_types) + inference.tool_registry
│   └── tool_registry.py
├── pipeline/               # the Director→Writer→Editor engine
│   ├── __init__.py         # re-exports handle_turn, handle_regenerate, …
│   ├── orchestrator.py
│   ├── state.py            # was pipeline_state.py (ModelLane, TurnState, _PipelineConfig)
│   └── passes/             # moved from backend/passes/ MINUS the analysis modules
│       ├── __init__.py
│       ├── writer.py
│       ├── director/       # director.py, prompt_rewrite.py, __init__.py
│       └── editor/         # editor.py, feedback.py, length_guard.py, __init__.py
│                           #   (audit.py + detectors now live in analysis/)
├── features/               # VERTICAL SLICES — each feature its own self-contained folder
│   ├── cards/
│   │   ├── __init__.py     # facade
│   │   ├── parsing.py      # was tavern_cards.py
│   │   └── downloader.py   # was card_downloader.py (imports .parsing)
│   ├── summarization/
│   │   ├── __init__.py
│   │   └── summarizer.py   # ConversationSummarizer
│   └── presets/
│       ├── __init__.py
│       └── engine.py       # was presets.py
└── workflows/              # plugin-registry gold standard; structure UNCHANGED, but 3 internal files
                            #   (toolkit.py, _forced_call.py, registry.py) get import-path edits in Phases 1/2/3
```

> Note: a stale `backend/tts/` directory exists but contains **only `__pycache__`** (no source — the real TTS lives in `workflows/tts/`). Delete it as cleanup; it is correctly absent from the tree above.

### Why each top-level module lands where it does

| Module(s) today | Lands in | Rationale |
|---|---|---|
| `llm_types.py`, `macros.py`, `locks.py`, `utils.py` | `core/` | Dependency-free / near-leaf; high fan-out. (`utils` imports `llm_types`; both intra-`core`.) `macros` is imported by 6 modules, `llm_types` by 9. |
| `passes/editor/audit.py` + the 7 detectors (`slop_detector`, `text_segmentation`, `contrastive_negation`, `opening_monotony`, `phrase_repetition`, `structural_repetition`, `template_repetition`) | `analysis/` | Pure prose-quality detection, deps only on `database.models`. **Shared by the editor pass AND `workflows/toolkit`** — extracting it is what keeps the one-way rule (was the lone `workflows → passes` back-edge). |
| `llm_client.py`, `endpoint_profiles.py`, `cached_call.py`, `kv_tracker.py`, `prompt_builder.py`, `tool_registry.py` | `inference/` | Cohesive LLM transport + prompt/tool assembly. (NB: **`llm_client` imports `endpoint_profiles`**, not the reverse — `endpoint_profiles` is the leaf; `cached_call`→`kv_tracker`; `prompt_builder`→`tool_registry` + `core`. Cluster is internally near-flat.) `llm_client` has 10 importers repo-wide. |
| `orchestrator.py`, `pipeline_state.py`, `passes/` (minus analysis) | `pipeline/` | The turn engine + its per-turn contracts + its passes belong together. |
| `tavern_cards.py`, `card_downloader.py` | `features/cards/` | Domain-isolated; only `main.py` imports them; `card_downloader → tavern_cards` is internal to the slice. |
| `summarizer.py` | `features/summarization/` | A user feature (not a pipeline pass); depends only downward on `inference` + `core`. |
| `presets.py` | `features/presets/` | DB-facing maintenance feature; depends only on `database/`. |
| route half of `main.py` | `api/routes/*` | Removes the 93-route hotspot. |
| `main.py` (remainder) | `main.py` (thin) + `api/__init__.py` + `api/deps.py` | Entry point keeps only `build_app()` wiring. |

### The "Standard Slice Shape" (the symmetry contract)

Codify one shape every `features/<name>/` folder follows — mirroring `workflows/tts/`:

```
features/<name>/
├── __init__.py     # facade: re-export the slice's public callables
├── contracts.py    # (optional) slice-local dataclasses/TypedDicts; import only from core/ + database/models
├── <logic>.py      # pure logic, testable in isolation
└── <integration>.py# wiring that reads context, calls logic, persists via database/
```

A slice may import **downward** (`core`, `inference`, `analysis`, `database`) but never
from another slice, `pipeline/`, `workflows/`, or `api/`. This is the same one-way rule
`database/` and `passes/` already enforce.

### How a new feature stops editing existing files

1. **Route surface** — `api/build_app()` auto-includes every `APIRouter` exported from `api/routes/` (iterate a `ROUTERS` list, or `pkgutil`-walk the package). Adding an HTTP feature = drop `api/routes/<feature>.py` exporting `router`. No edit to `main.py`.
2. **Secondary/pipeline-adjacent feature** — use the existing `workflows/` plugin path (new folder + one `register_workflow`/`subscribe` block). Already satisfies the requirement; documented as the *default* extension mechanism.
3. **Self-contained domain feature** — new `features/<name>/` slice + one router file. The only shared files it touches are additive (`database/schema.py` + a numbered migration when it needs persistence) — unavoidable and already the documented "Adding a Feature Flag" flow.

---

## Implementation Plan

This is a large mechanical refactor. Do it in **ordered phases**, each independently
testable (`./scripts/tests.sh all` green after every phase).

### The bottom-up retarget rule (read first — this is the heart of the method)

We move layers **bottom-up** (`core` → `inference`/`analysis` → `workflows` edits → `pipeline` → `features` → `api`). Two invariants make every phase end green:

1. **When a module moves, retarget *all* its importers in the same phase** — even importers whose own move happens in a later phase. A facade re-export changes only the *name* you import from (`from .llm_client` → `from .inference`), not the dot depth, because the new layer package sits at the `backend` top level exactly where the flat module used to. So this retarget is a pure search-and-replace that **keeps the dot count unchanged**.
   - *Why this matters:* if Phase 2 moves `llm_client.py` → `inference/client.py` but defers `passes/writer.py`'s `from ..llm_client import …` to Phase 4 (when `passes/` itself moves), then after Phase 2 `passes/writer.py` imports a module that no longer exists and **`python -c "import backend.main"` fails**. Retarget eagerly.
2. **When an importer itself later moves deeper, only its dot depth changes** — the *targets* are already in their final packages (lower layers moved first), so each import reaches its final form in one edit at move-time, never needing another pass.

Net effect: a module that both imports a lower layer *and* moves later (e.g. `orchestrator.py`, every `passes/*` file) is touched in two phases — once to **retarget the name** (when the lower layer moves) and once to **bump the dots** (when it moves). Both edits are mechanical; the verification gate (`import backend.main` + pyright + tests) catches any miss.

Each phase below lists: **(M)** modules moved + facade, **(R)** the exhaustive set of importers to retarget *this* phase, **(O)** the moved modules' own (now-finalized) downward imports, **(T)** tests / patch-targets / docstrings to update, **(V)** verification.

### Phase 0 — Safety net
- Branch off current `orchestrator-housekeeping`.
- Confirm baseline green: `./scripts/tests.sh all`; pyright clean (`pyrightconfig.json`, **standard** mode, **zero errors, no file-level suppressions** — must stay zero, and **no `# pyright: ignore` may be reintroduced**, per AGENTS.md §"Type checking").
- Record the import inventory as a checklist (the **(R)** lists below are the snapshot taken 2026-06-15; re-grep if the branch has drifted: `grep -rnE 'from \.+(<module>)\b' backend/`).
- Note: there are **no frontend changes** in this plan; the SSE/HTTP contract is unchanged.

### Phase 1 — `core/` (shared kernel)
- **(M)** Move `llm_types.py`, `macros.py`, `locks.py`, `utils.py` → `core/`. Add `core/__init__.py` re-exporting `ChatMessage`, `ContentPart`, `AssistantToolMessage`, `WireMessage`, `Macros`, the lock objects + the `_workflow_state_locks` registry, `estimate_tokens`, `scrub_log`, `build_multimodal_content`, `extract_hyperparams`.
- **(O)** `utils.py`'s `from .llm_types import ContentPart` stays `from .llm_types import …` (1 dot — sibling within `core/`). The other three import nothing.
- **(R) retarget (same dot count, `.<mod>` → `.core`):**
  - `macros` (6): `main`, `orchestrator`, `passes/director/director`, `prompt_builder`, `summarizer`, `workflows/toolkit`.
  - `llm_types` (9): `orchestrator`, `passes/director/director`, `passes/editor/editor`, `passes/editor/feedback`, `passes/writer`, `pipeline_state`, `prompt_builder`, `summarizer`, `utils` *(now intra-core)*.
  - `locks` (3): `main`, `orchestrator`, `workflows/toolkit`.
  - `utils` (7): `main`, `orchestrator`, `passes/director/director`, `passes/editor/editor`, `passes/editor/feedback`, `passes/writer`, **`database/queries/workflow_attachments.py`** *(`...utils` → `...core`; this is the one real `database → core` edge — do not miss it)*.
- **Keep `Macros.resolve_prompt_messages` wired as the `CachedBase.resolve` hook** (AGENTS.md Gotcha 10). The actual binding `resolve=macros.resolve_prompt_messages` is in **`orchestrator.py` (two call sites: ~L130, ~L141)**, *not* `cached_call.py` (which only documents/applies the hook). Only the `from .macros import Macros` import path changes (`→ from .core import Macros`); the lowercase `macros` in the wiring is a local `Macros` instance, untouched. *(AGENTS.md Gotcha 10's phrasing "wired in cached_call.py" describes the apply-site, not the bind-site — keep the bind in orchestrator.)*
- **(T)** Tests/patches: `from backend.locks import …` (test_locks, test_character_state_lock, test_config_route, test_toolkit_surface) → `backend.core.locks`; conftest's `backend.locks._workflow_state_locks` reference → `backend.core.locks._workflow_state_locks` (patch the **canonical module**, not the facade — same class of trap as Gotcha 7's `DB_PATH`). `test_toolkit_surface.py` asserts `toolkit.X is locks.X` (lock-singleton identity) — update its `backend.locks` import and confirm the facade re-export preserves object identity.
- **(V)** `python -c "import backend.main"`; pyright 0; `./scripts/tests.sh all`.

### Phase 2 — `inference/`
- **(M)** Move `llm_client.py`→`inference/client.py`, plus `endpoint_profiles.py`, `cached_call.py`, `kv_tracker.py`, `prompt_builder.py`, `tool_registry.py` → `inference/`. Add `inference/__init__.py` re-exporting `LLMClient`, `AbortToken`, `reasoning_cfg`, `parse_tool_calls`, `CachedBase`, `cached_complete`, `_KVCacheTracker`, `build_prefix`, `build_editor_prompt`, `build_feedback_prompt`, `compute_lorebook_injection_block`, `build_lorebook_catalog`, `build_style_injection`, `compute_style_injection_block`, `format_message_with_attachments`, `TOOLS`, `STANDALONE_TOOLS`, `enabled_schemas`, `build_direct_scene_tool`, `build_feedback_tool`, `GIVE_FEEDBACK_CHOICE`, etc. *(Re-export the full set the importers below actually pull.)*
- **(O)** Moved modules' own imports finalize: `prompt_builder` `from .macros`/`from .llm_types` → `from ..core import …` (2 dots), `from .tool_registry` stays `.tool_registry` (sibling). `client.py` `from . import endpoint_profiles` stays (sibling). `cached_call` `from .kv_tracker` stays (sibling).
- **(R) retarget (`.<mod>`/`..<mod>`/`...<mod>` → same-depth `…inference`):**
  - `llm_client` (10): `main`, `orchestrator`, `pipeline_state`, `summarizer`, `passes/writer`, `passes/director/director`, `passes/editor/editor`, `passes/editor/feedback`, `workflows/toolkit`, `workflows/_forced_call`.
  - `cached_call` (6): `orchestrator`, `pipeline_state`, `passes/writer`, `passes/director/director`, `passes/editor/editor`, `passes/editor/feedback`.
  - `kv_tracker` (5): `cached_call` *(intra — stays sibling, skip)*, `orchestrator`, `passes/writer`, `passes/director/director`, `passes/editor/editor`.
  - `prompt_builder` (8): `main`, `orchestrator`, `summarizer`, `passes/director/director`, **`passes/director/__init__`**, `passes/editor/editor`, `passes/editor/feedback`, `workflows/toolkit`.
  - `tool_registry` (9): `main`, `orchestrator`, `prompt_builder` *(intra — stays sibling, skip)*, `passes/director/director`, `passes/editor/editor`, `passes/editor/feedback`, `workflows/toolkit`, `workflows/_forced_call`, `workflows/registry`.
  - `endpoint_profiles` (1): `client.py` *(intra — stays sibling, skip)*.
  - **The three workflow internals confirmed** (`toolkit`, `_forced_call`, `registry`) plus **all four `passes/` files and both `pipeline_state`/`summarizer`** — the original draft's "just the 3 workflow files" was wrong; `passes/*` import inference heavily and would break the tree if deferred.
- **Preserve KV-cache invariants** — import-path-only; do not touch prefix/tool-assembly logic ([docs/architecture/kv-cache.md](docs/architecture/kv-cache.md)).
- **(T)** Tests: `from backend.{llm_client,cached_call,kv_tracker,tool_registry,endpoint_profiles,prompt_builder} import …` → `backend.inference…` (≈40 sites across unit + integration). **String patch-targets need care** — `LLMClient` is patched in *multiple namespaces*: the definition `monkeypatch.setattr("backend.llm_client.LLMClient", …)` → `"backend.inference.client.LLMClient"`, but `"backend.orchestrator.LLMClient"` (the name re-imported into orchestrator's namespace) stays `backend.orchestrator.*` **until Phase 4** moves orchestrator. Update each namespace path when *that* module moves (see conftest's `_llm_mock` comment about `backend.main`/`backend.orchestrator` retaining their own `LLMClient` refs).
- **(V)** as Phase 1.

### Phase 3 — `analysis/` (extract the shared auditor)
*Independent of `inference`; depends only on `database.models`, so it may be sequenced any time after Phase 0 — placed here so it lands before the `pipeline/` move that consumes it.*
- **(M)** Move out of `passes/editor/`: `audit.py` → `analysis/audit.py`; the 7 detectors → `analysis/detectors/` (`slop_detector`, `text_segmentation`, `contrastive_negation`, `opening_monotony`, `phrase_repetition`, `structural_repetition`, `template_repetition`). Add `analysis/__init__.py` (facade: `run_audit`, `format_report`, `AuditReport`, `AUDIT_TYPES`, `DetectionResult`, `MonotonyResult`, `FlaggedOpener`, `TemplateResult`, `FlaggedTemplate`, `StructuralResult`, `PhraseResult`, `split_narration_sentences`) and `analysis/detectors/__init__.py`.
- **(O)** Internal rewrites:
  - `audit.py` (now pkg `backend.analysis`): detector imports `from .slop_detector …` → `from .detectors.slop_detector …` (still 1 dot, now via subpackage); `from ...database.models import PhraseGroup` (3 dots) → `from ..database.models import PhraseGroup` (**2 dots** — audit moved *up* from depth-3 `passes/editor/` to depth-2 `analysis/`).
  - Detectors (now pkg `backend.analysis.detectors`, still depth 3): `from .text_segmentation …` stays 1 dot (sibling); `slop_detector`'s `from ...database.models import PhraseGroup` stays **3 dots** (depth unchanged: `backend.passes.editor` → `backend.analysis.detectors`).
- **(R) retarget the two non-test consumers** (the only ones — `orchestrator`, `writer`, `director`, `feedback` do **not** import the auditor/detectors; confirmed):
  - `passes/editor/editor.py` *(still in `passes/` this phase)*: `from .audit import AuditReport, format_report, run_audit` (1 dot) → `from ...analysis import …` (**3 dots**); `from .slop_detector import DetectionResult`, `from .opening_monotony import …`, `from .template_repetition import …`, `from .text_segmentation import split_narration_sentences` → `from ...analysis import …` (facade) (1 dot → 3 dots — these were intra-package siblings, now cross-package). `from .feedback import …` and `from .length_guard import …` stay (those modules remain in `passes/editor/`).
  - `workflows/toolkit.py`: `from ..passes.editor.audit import format_report, run_audit` (2 dots) → `from ..analysis import format_report, run_audit` (**2 dots** — same count; this single edit is what dissolves the would-be `pipeline ↔ workflows` cycle).
- **(T)** Tests: `from backend.passes.editor.audit import …` → `backend.analysis` (test_slop_detector, test_audit_toggles, test_editor_abort, test_super_regen_audit_context, test_opening_monotony); `from backend.passes.editor.{slop_detector,contrastive_negation,opening_monotony,template_repetition,structural_repetition,phrase_repetition,text_segmentation} import …` → `backend.analysis.detectors.<mod>` (incl. **private** symbols like `contrastive_negation._split_sentences`, `slop_detector._split_sentences` — these bypass the facade, point at the submodule). Note `_run_contextual_audit` lives in `passes/editor/editor.py` (stays — moves in Phase 4), not in `analysis/`.
- **(V)** as Phase 1.

### Phase 4 — `pipeline/`
- **(M)** Move `orchestrator.py`, `pipeline_state.py`→`pipeline/state.py`, and `backend/passes/`→`backend/pipeline/passes/` (now without the analysis modules). Add `pipeline/__init__.py` re-exporting `handle_turn`, `handle_regenerate`, `handle_fork_edit`, `handle_super_regenerate`, `handle_magic_rewrite`, `resolve_persona_id`, `agent_enabled`, and the `TurnState`/`ModelLane`/`_PipelineConfig` contracts. *(Private symbols `_run_pipeline`, `_consume_pipeline`, `_iterate_pre_pipeline_hooks` are **not** re-exported — tests that use them import from `backend.pipeline.orchestrator` directly.)*
- **Dot-depth bumps (the moving modules climb one level deeper, so every edge that climbs out to `backend` gains one dot):**
  - `passes/writer.py` (pkg `backend.pipeline.passes`): `..inference`→`...inference`, `..core`→`...core`, `..llm_types`/`..utils` (already `..core` after P1) →`...core`. **Exception — same-package edges keep their dot count:** `..pipeline_state`→`..state` (both now under `pipeline/`); `.editor.length_guard` unchanged (sibling).
  - `passes/director/director.py` **and** `passes/director/__init__.py` (pkg `backend.pipeline.passes.director`): `...inference`→`....inference` (**4 dots**), `...core`→`....core` (**4 dots**). **Exception:** `...pipeline_state`→`...state` (3 dots, same count); `.prompt_rewrite` unchanged (sibling).
  - `passes/editor/editor.py` **and** `passes/editor/feedback.py` (pkg `backend.pipeline.passes.editor`): `...inference`→`....inference`, `...core`→`....core`, **`...database.models`→`....database.models`** (TYPE_CHECKING edges climb too — don't forget them), **`...analysis`→`....analysis`** (editor.py's Phase-3 retarget now gets its dot bump: 3→4). **Exception:** `...pipeline_state`→`...state` (3→3). The only remaining *sibling* imports are `.feedback` and `.length_guard` (both stayed in `passes/editor/`) — unchanged. *(editor.py's `.audit`/`.slop_detector`/`.opening_monotony`/… are no longer siblings — they became `...analysis` in Phase 3 and bump to `....analysis` here, per the `...analysis` entry above.)*
  - `passes/editor/__init__.py`, `passes/director/__init__.py`: bump any climbing edges (e.g. `passes/director/__init__.py`'s `prompt_builder` edge, now `...inference` after the P2 retarget, →`....inference`).
- **Rewrite `orchestrator.py`'s own ~19 backend-level imports** (pkg `backend.pipeline`): `.workflows`→`..workflows`; `.workflows.attachment_cache`→`..workflows.attachment_cache`; `.cached_call`/`.kv_tracker`/`.llm_client`/`.prompt_builder`/`.tool_registry` (already `.inference` after P2) →`..inference`; `.macros`/`.llm_types`/`.locks`/`.utils` (already `.core` after P1) →`..core`; `.pipeline_state`→`.state`; **`from . import database as db`→`from .. import database as db`** and **`.database.models`→`..database.models`** (the two `database` edges the draft forgot); `.passes.*`→`.passes.*` (unchanged — passes moved alongside).
- **Rewrite `pipeline/state.py`'s own imports** (was `pipeline_state.py`; the draft listed only its `length_guard` edge): `.cached_call`/`.llm_client` (already `.inference` after P2) →`..inference`; `.llm_types` (already `.core` after P1) →`..core`; **`.passes.editor.length_guard` stays `.passes.editor.length_guard`** (1 dot — `state` and `passes` are now siblings under `pipeline/`; `length_guard` imports only stdlib, so no cycle — keep this runtime edge intact).
- **(R) retarget external importers of the moved modules** (orchestrator/state/passes all sit at `backend` no longer — their *importers outside `pipeline/`* update the name):
  - `orchestrator` (1 non-test importer): `main` — `from .orchestrator import …` → `from .pipeline import …`.
  - `pipeline_state`: only `orchestrator` + the three passes (all intra-`pipeline/`, handled above). No external importers.
  - `passes/*`: only `orchestrator` (intra) imports them; no external retarget.
- **(T)** Tests (largest surface): `from backend.orchestrator import …` → `backend.pipeline.orchestrator` (public can use `backend.pipeline`); `from backend.passes.* import …` → `backend.pipeline.passes.*`; `from backend.pipeline_state import …` → `backend.pipeline.state`. **String patch-targets (fail at runtime, invisible to the import smoke test):** `"backend.passes.writer.writer_pass"` → `"backend.pipeline.passes.writer.writer_pass"` (≈25 sites in test_pipeline_hooks etc.); `"backend.passes.editor.editor._run_contextual_audit"` / `".editor_pass"`; `"backend.passes.director.director.director_pass"`; `"backend.orchestrator.LLMClient"` → `"backend.pipeline.orchestrator.LLMClient"` (the deferred namespace from Phase 2).
- **(V)** as Phase 1, **plus** `grep -rn "backend\.\(orchestrator\|passes\|pipeline_state\)" tests/` returns nothing (catches string targets).

### Phase 5 — `features/`
- **(M)** `features/cards/`: `tavern_cards.py`→`parsing.py`, `card_downloader.py`→`downloader.py` (`downloader.py`'s `from . import tavern_cards` → `from . import parsing`); `__init__.py` re-exports `parse`, `card_to_dict`, `read_orb_id`, `from_json_obj`, `browse`, `randomize`, `download_card`. `features/summarization/summarizer.py` (`ConversationSummarizer`; its imports are already `..core`/`..inference` after P1/P2 — bump to `...core`/`...inference` for the new depth, or import via facade). `features/presets/engine.py` (`__init__.py` re-exports the functions `main.py` calls **and `schema_safety_problems`**, which `database/bootstrap.py` needs).
- **(O) presets `database` edges DO change dot count** (the draft wrongly said "unchanged"): `engine.py` is now pkg `backend.features.presets`, so `from .database import preset_schema as ps` (1 dot) → `from ...database import preset_schema as ps` (**3 dots**); likewise `.database.migrations`, `.database.schema`, and the lazy `.database.connection` → `...database.*`. The *dependency* is unchanged; the *import lines* are not.
- **(R)** Update importers of the moved features:
  - `summarizer` (1): `main` — `from .summarizer import ConversationSummarizer` → `from .features.summarization import …`.
  - `tavern_cards`, `card_downloader`, `presets` (via `main.py:22` `from . import card_downloader, presets, prompt_builder, tavern_cards`) → split into `from .features.cards import …`, `from .features.presets import …` (and `prompt_builder` already moved to inference in P2).
  - **`database/bootstrap.py`'s lazy `from .. import presets`** (the documented circular-import exception) → `from ..features import presets`. Its call `presets.schema_safety_problems(conn)` requires that symbol in the slice facade (see (M)).
- **(T)** Tests: `from backend.presets import ALL_DOMAINS` (test_presets) → `backend.features.presets`. (No direct test imports of `summarizer`/`tavern_cards`/`card_downloader` — exercised via the API.)
- **(V)** as Phase 1.

### Phase 6 — `api/` (split the 93 routes)
- **(M)** Create `api/__init__.py` with `build_app()`: instantiate `FastAPI(title="Orb", lifespan=lifespan)`, attach the lifespan (`init_db`, `run_pending(DB_PATH)`; `finalize_registry` stays import-time at the bottom of `workflows/__init__.py`), **register the `no_cache_middleware` `@app.middleware("http")`**, auto-include routers from `api/routes/`, then mount static `frontend/` **last** (`app.mount("/static", StaticFiles(...))` is literally the final line of today's `main.py:2598` — preserve that ordering so routes match before the static catch-all).
- Move Pydantic models → `api/schemas.py`. **`api/deps.py` must own *all* cross-route shared surface** that today lives at `main.py` module scope, or the split silently forks state:
  - `_active_aborts` — the **module-level mutable abort registry** (`main.py:240`) read/written by 5 routes. One module, imported, never duplicated.
  - `_workflow_root_lock`/`_workflow_root_locks` and `_conversation_stream_lock` — the `@asynccontextmanager` lock helpers **and their backing dict registries** (**16 call sites**: `_workflow_root_lock` ×9 + `_conversation_stream_lock` ×7).
  - `_CleanupStreamingResponse` (`main.py:1633`, used by ~7 streaming routes; AGENTS.md Gotcha 2).
  - `Depends` providers `require_world` / `require_lorebook_entry`; validators `_normalise_lorebook_entry` / `_validate_phrase_group`.
  - In total ~15 non-route helpers among `main.py`'s 108 top-level `def`s — relocating these is the real work of Phase 6, not the route-decorator moves.
- Split routes into `api/routes/<domain>.py`, each exposing `router = APIRouter()`. **This file taxonomy is the plan's own, not a 1:1 map of AGENTS.md sections** — AGENTS.md has **no "stats" section** (`/api/stats` lives under **Other**) and **no standalone "endpoints" section** (endpoint/model routes sit under **Settings & Config**), and it *does* have an **Inspector** section (the `/director`, `/logs`, `/director-log` routes) to fold into `conversations.py`. Proposed files: `settings`, `endpoints` (+ models), `conversations` (incl. Inspector + Secondary-Workflows-adjacent stream routes as appropriate), `messages`, `characters`, `fragments` (Fragments & Moods), `worlds` (Worlds & Lorebooks), `phrase_bank`, `personas`, `presets`, `workflows`, `stats`, `misc` (`/`, `/api/themes`, `/api/reset`).
- Reduce `main.py` to: `from .api import build_app; app = build_app()` plus the uvicorn `__main__` guard. *(`backend.main:app` stays valid — the Dockerfiles' `uvicorn backend.main:app` and conftest's `from backend.main import app` keep working.)*
- **Route paths, methods, request/response shapes, and SSE event names are byte-for-byte unchanged** — verify against the AGENTS.md "API Endpoints" list.
- **(T)** conftest's `backend.main._workflow_root_locks` → `backend.api.deps._workflow_root_locks`; revisit the `_llm_mock` comment about `backend.main` retaining an `LLMClient` ref — if routes (not `main`) now import `LLMClient`, the patch namespace moves to the relevant `api/routes/*` module (or keep the canonical `backend.inference.client.LLMClient` patch, which all namespaces alias).
- **(V)** as Phase 1, **plus** integration tests (FastAPI `TestClient` against a real DB) exercise the actual route surface, confirming the split preserved every endpoint; **endpoint diff** against AGENTS.md.

### Phase 7 — Docs + conventions
- Rewrite the **Directory Structure** tree in [AGENTS.md](AGENTS.md) and update every `path/file.py` reference (Data Contracts, Configuration Chain, "Where it lives in code", Gotchas all name old paths).
- **Sweep stale module paths in docstrings/comments** (cosmetic but required for "meticulous"): `grep -rn "backend\.\(macros\|llm_types\|locks\|utils\|llm_client\|cached_call\|kv_tracker\|prompt_builder\|tool_registry\|endpoint_profiles\|orchestrator\|pipeline_state\|presets\|tavern_cards\|card_downloader\|passes\)" backend/ --include='*.py'` — e.g. `core/locks.py`'s own header mentions `backend.orchestrator`; `database/queries/{settings,conversations,messages,character_cards}.py` reference `backend.locks.*`/`backend.orchestrator` in caller-contract comments; `pipeline/passes/director/director.py` references `backend.tool_registry.*`/`backend.orchestrator._PipelineResult`; `workflows/toolkit.py`'s header lists `backend.llm_client`/`backend.prompt_builder`.
- Add an **Architecture Layers** section: the layers, the one-way downward rule, the **bottom-up retarget rule**, the "Standard Slice Shape", and the three ways to add a feature (router drop / workflow plugin / feature slice) — workflows as the default for pipeline-adjacent features.
- Update **Common Development Workflows** accordingly. Also update AGENTS.md Gotcha 10 to say the resolve hook is **bound in `pipeline/orchestrator.py`** and applied in `inference/cached_call.py`.

### Optional follow-up (not in this PR)
- A `scripts/check_layering.py` import-linter (or `import-linter` contract) asserting the one-way rule in CI — and in particular forbidding any `workflows → pipeline` / `* → api` edge — mirroring `database/`'s existing `schema_coverage_problems()` backstop. Flag as a fast-follow.

---

## Key Reuse / Precedent to Mirror (don't reinvent)

- **Facade `__init__.py`** — copy `database/__init__.py` and `workflows/__init__.py` re-export style for each new layer package.
- **Stable author surface** — `workflows/toolkit.py` proves the facade insulates consumers from internal moves; rely on it in Phases 1–3 (its `macros`/`locks`/`llm_client`/`prompt_builder`/`tool_registry`/`audit` edges all retarget without any *new-workflow-author* code changing).
- **Moving a shared shape *down* to kill an upward import** — `PhraseGroup` was moved from `slop_detector.py` into `database/models.py` for exactly this reason. The `analysis/` extraction is the same move applied to `audit.py` + detectors.
- **One-way "never import up" rule** — already documented and enforced for `database/`; this plan generalizes it to all layers.
- **Dependency-inversion seam (if ever needed)** — `database/queries/messages.py`'s `register_workflow_attachment_persister` (impl registered by `workflows/attachment_cache.py` at import) is the sanctioned way to let a lower layer call up without an upward import. Not needed here (the `analysis/` extraction removes the only such case) — but the pattern exists if a future edge appears.
- **Lazy-import exception handling** — `database/bootstrap.py`'s `from .. import presets` (→ `from ..features import presets`) shows the sanctioned way to break a cycle; keep it explicit, don't add new hidden ones.

## Risks / Watch-outs

- **Import-path blast radius.** `main.py` alone imports **12 sibling modules** (`llm_client`, `locks`, `macros`, `orchestrator`, `summarizer`, `tool_registry`, `utils`, `workflows` ×2, `card_downloader`, `presets`, `prompt_builder`, `tavern_cards`) + dozens of DB symbols. Move one layer per phase, retarget eagerly (the bottom-up rule), run tests between phases.
- **Test suite + string patch-targets are a second blast radius the facades do NOT cover.** ~120 test imports of old paths (`backend.passes.*` ×56, `backend.llm_client` ×15, `backend.orchestrator` ×11, …), plus `mock.patch`/`monkeypatch.setattr` **string** targets that fail at *runtime*, not at `import backend.main`. Private symbols (`_run_pipeline`, `_run_contextual_audit`) and deep submodule paths bypass the top-level facade. `LLMClient` is patched in multiple namespaces — each updates when *its* module moves. Treat per-phase **(T)** items as first-class work, gated by a `grep tests/` check, not afterthoughts.
- **`backend/data/` (SQLite `app.db` + `snapshots/`) is intentionally untouched** and omitted from the tree. `DB_PATH` in `database/connection.py` hardcodes `../data/app.db` *relative to the database package*, which doesn't move — so the reorg doesn't disturb it (Gotcha 7 stays valid).
- **Pyright must stay at zero errors**; **no `# pyright: ignore` may be reintroduced** (AGENTS.md §"Type checking"). Run pyright after each phase.
- **KV-cache invariants** (Phases 2/4) — moves are import-path-only; do not alter prefix/tool-schema assembly.
- **`DB_PATH` test-patching seam** (Gotcha 7) in `database/connection.py` is untouched — but the *same class of trap* now applies to the relocated `_workflow_state_locks` (→ `core/locks`) and `_workflow_root_locks` (→ `api/deps`): patch the **canonical module**, not a facade re-export.
- **Circular imports** — `pipeline/state.py ↔ pipeline/passes/`, `inference ↔ core`, `analysis ↔ database` are all downward-only. **A passing `python -c "import backend.main"` does *not* prove the DAG is clean** (the old `workflows → passes.audit` back-edge would not crash, because `audit` doesn't transitively import `orchestrator`/`workflows`). The `analysis/` extraction is what actually removes it; the optional import-linter is what would *enforce* it.

## Verification

Run after **each phase**, and fully at the end:

1. **Import smoke:** `python -c "import backend.main"` (catches broken relative imports and runtime cycles immediately).
2. **Stale-path grep (catches string patch-targets the smoke test can't):** `grep -rn "backend\.\(<modules moved so far>\)" tests/` returns nothing pointing at an old path; same grep over `backend/ --include='*.py'` for docstrings in Phase 7.
3. **Type check:** pyright over `backend/` — **zero errors**, no new suppressions.
4. **Tests:** `./scripts/tests.sh all` — unit + integration (FastAPI `TestClient` against a real DB). Integration tests exercise the real route surface, so they confirm the `api/` split preserved every endpoint. (Under a sandbox, run integration with escalated execution per AGENTS.md §"Codex Sandbox Caveat".)
5. **Lint/format:** `./scripts/format_backend.sh` (Ruff, 128-char) + `./scripts/lint.sh`.
6. **Manual end-to-end (final):** start the backend (`./run_unix.sh`), open the frontend, send a turn (Director→Writer→Editor SSE stream renders), import a PNG card, run a summarize/compress, and trigger the TTS workflow — confirming the cards / summarization / presets / workflows slices, the **`analysis/` auditor** (run an editor pass *and* a standalone workflow audit), and the SSE contract all still work.
7. **Endpoint diff:** confirm the live route table matches the AGENTS.md "API Endpoints" list (no path/method drift from the split).
