# Inference boundary refactor implementation plan

Status: ready for implementation. This document update changes no production
code.

## 1. Outcome

Create a lower shared `backend/prompting/` package for deterministic,
provider-independent model-facing construction, then move product policy out of
`backend/inference/`.

The resulting ownership is:

- `inference/`: model execution, provider adaptation, retries and errors,
  structured-output normalization, cache-call mechanics, cache diagnostics, and
  local model runtimes;
- `prompting/`: shared prompt-context projections, message construction, tool
  schemas, and deterministic ordered tool composition;
- `pipeline/passes/`: instruction prose and result contracts owned by a
  pipeline pass;
- `features/`: feature enablement and feature-specific adapters;
- `analysis/`: text interpretation and measurement.

A module belongs in `prompting/` only when it:

1. deterministically transforms caller-supplied data;
2. constructs model-facing content or a canonical projection required by that
   content;
3. is shared by multiple upper-layer consumers or by the stable workflow
   toolkit; and
4. performs no I/O, model call, persistence, or feature/pass enablement decision.

### Enforced dependency graph

```text
api        -> pipeline, features, workflows, prompting, inference, analysis, database, core
pipeline   -> features, workflows, prompting, inference, analysis, database, core
features   -> prompting, inference, analysis, database, core
workflows  -> prompting, inference, analysis, database, core
prompting  -> core
inference  -> core
analysis   -> database, core
database   -> core
core       -> (nothing)
```

`features/` and `workflows/` are sibling composition inputs and do not import
each other. Feature slices remain isolated from peer feature slices.

## 2. Planned source moves

| Current surface | Implemented destination |
|---|---|
| autocomplete `local_ml.build_prompt` and the autocomplete-specific `complete` whitespace adapter | `features/autocomplete/` |
| `format_message_with_attachments`, `group_speaker_label`, and `build_prefix` | `prompting/base.py` |
| all of `inference/group_context.py` | `prompting/group_context.py` |
| `resolve_mood_fragment_randoms`, `compute_style_injection_block`, and `build_style_injection` | `prompting/scene_direction.py` |
| lorebook effective projection, catalog, selection, rendering, scan-depth contracts, and block builders | `prompting/lorebook.py` |
| `agentic_lorebook_active` | `features/lorebook/` |
| Director and Agentic Lorebook instruction builders | `pipeline/passes/director/prompts.py` |
| direction-note instruction and block rendering | `pipeline/passes/director/direction_note_prompts.py` |
| Editor and feedback instruction builders | `pipeline/passes/editor/prompts.py` |
| Dynamic Worlds instruction builder | `pipeline/passes/world_change_prompt.py` |
| shared tool-call instruction formatting and reasoning guidance | `pipeline/passes/_prompting.py` |
| concrete built-in schemas, choices, and dynamic schema builders | `prompting/tool_schemas.py` |
| ordered tool lookup, filtering, registration, and removal | `prompting/tool_catalog.py` |
| `strictify_schema` | `inference/schema.py` |
| Director-loop tool membership | `pipeline/tools.py` as `DIRECTOR_LOOP_TOOL_NAMES` |

`features/lorebook/__init__.py` remains the feature facade. Feature callers use
that facade; lower consumers such as `workflows/toolkit.py` import
`prompting.lorebook` directly.

The context-size route imports shared group and Scene Direction projections from
`prompting/`. It does not depend on private pass modules or expand the
`pipeline` facade for read-only formatting helpers.

## 3. Preserved contracts

Every relocation preserves:

- HTTP request and response bodies;
- SSE event names, ordering, and payload shapes;
- database schema and stored JSON shapes;
- prompt text, whitespace, delimiters, message order, and parameter order;
- macro resolution timing, seeds, and stored random choices;
- tool names, descriptions, schemas, property order, choice objects, and final
  array order;
- the names and signatures exported by `backend.workflows.toolkit`.

Repository consumers migrate atomically with each move. Old
`backend.inference` re-exports are removed in the same phase so the final
architecture contains no `inference -> prompting` dependency.

### Ordered tool contract

The built-in order becomes an explicit tuple:

```python
BUILTIN_TOOL_ORDER = (
    "direct_scene",
    "editor_apply_patch",
    "editor_rewrite",
    "give_feedback",
    "record_direction_note",
    "select_lorebook",
    "propose_world_changes",
)
```

`BUILTIN_TOOL_NAMES` is derived from that tuple. Enabled built-ins are emitted
in tuple order. Workflow tools append in workflow registration order.
Re-registering an existing workflow tool preserves its position.

`prompting.tool_catalog` owns mutable catalog state. Production consumers use
catalog operations for lookup, registration, and removal; workflow replacement
does not mutate catalog dictionaries or standalone sets directly.
`workflows.toolkit` retains `TOOLS` and `STANDALONE_TOOLS` as read-only live
views for compatibility. Test fixtures restore catalog state through
catalog-owned operations instead of calling `clear()`, `update()`, `pop()`,
or `discard()` on those views.

Tool-order verification uses the raw request body captured at the HTTP transport
boundary for both OpenAI-compatible and Anthropic requests. Cached-base
verification also compares insertion-order-preserving serialization:

```python
json.dumps(tools, separators=(",", ":"), ensure_ascii=False)
```

## 4. Implementation sequence

Each phase is one reviewable commit and must satisfy its acceptance checks
before the next phase starts.

### Phase 0 — make the target architecture executable

1. Create `docs/architecture/prompting.md` with the ownership test, allowed
   dependency graph, tool-order contract, and prompt-byte contract.
2. Update `AGENTS.md` with the same layer order and link responsibilities to
   the architecture note.
3. Replace numeric ranks in `scripts/check_backend_layers.py` with one explicit
   allowed-edge matrix containing `prompting`.
4. Make the checker fail when a Python-bearing top-level `backend/` package is
   absent from the matrix.
5. Preserve the feature peer-slice rule in the shared checker.
6. Replace the duplicate policy in `tests/unit/test_import_layering.py` with
   calls and focused negative cases against the shared checker.
7. Keep `tests/unit/test_backend_layers.py` as the repository-wide execution
   test for that checker.

Acceptance:

- the current backend passes the shared AST checker;
- fixtures prove rejection of `inference -> prompting`,
  `prompting -> inference`, `features -> workflows`,
  `workflows -> features`, peer feature-slice imports, and unclassified
  Python packages;
- the script and both test modules enforce the same matrix.

### Phase 1 — pin behavior before relocation

Add or strengthen exact-output tests for:

- solo prefixes and all group-context modes;
- user attachments and workflow attachment annotations;
- constant, depth, keyword, Agentic, replacement, and suppression lore paths;
- combined and per-fragment Director prompts;
- lorebook selection, feedback, direction-note, world-change, and Editor
  prompts;
- Scene Direction rendering, progressive fields, disabled Director state, and
  stored-random mutation;
- autocomplete prompts with blank summaries, named group messages, truncation,
  whitespace-ending drafts, and non-whitespace-ending drafts;
- the complete built-in tools blob;
- dynamic schema overrides, workflow registration order, workflow replacement,
  orphan-tool removal, standalone tools, and forced `offer_tools` calls.

Capture the actual request body for representative OpenAI-compatible and
Anthropic tool calls. Assert byte equality, including object-key and array
order. Keep the runtime KV tracker assertions as a second witness rather than
the byte oracle.

Acceptance:

- every moved behavior listed in Section 2 has characterization coverage at its
  current location;
- the tool tests fail on either schema-key reordering or tool-array reordering;
- existing expected prompt and tool fixtures remain unchanged.

### Phase 2 — extract autocomplete as one feature

1. Add `backend/features/autocomplete/__init__.py` and its implementation
   module.
2. Move `local_ml.build_prompt` into the feature.
3. Move `local_ml.complete` into the feature, preserving its trailing
   `rstrip`/conditional `lstrip` behavior.
4. Call `inference.local_ml.acomplete("autocomplete", ...)` as the lower
   runtime primitive.
5. Update `api/routes/messages.py`, package imports, and autocomplete tests.
6. Remove the `local_ml.py` executable self-check block after autocomplete and
   POV assertions are covered by unit tests.

Acceptance:

- the API returns the same completion for whitespace and non-whitespace tails;
- `inference/local_ml.py` contains only generic load, completion, scoring, and
  classification runtime contracts.

### Phase 3 — add shared prompt projections

1. Add `backend/prompting/__init__.py`, `base.py`,
   `group_context.py`, and `scene_direction.py`.
2. Move prefix/message construction to `prompting/base.py`.
3. Move the complete group-context projection to
   `prompting/group_context.py`.
4. Move Scene Direction projection and stored-random resolution to
   `prompting/scene_direction.py`.
5. Update pipeline context construction, Writer/Director composition,
   summarization, context-size reporting, workflow toolkit imports, tests, and
   package facades.
6. Remove `roster_names` from the public package facade; keep it private to
   `prompting.group_context`.
7. Update `docs/architecture/group-chats.md`, `kv-cache.md`, and
   `secondary-workflow.md`.
8. Delete `inference/group_context.py` after all imports migrate.

Acceptance:

- prefix, attachment, group, Scene Direction, and context-size fixtures are
  unchanged;
- `prompting/` imports only `core/`;
- `backend.workflows.toolkit` retains its existing public names and
  signatures.

### Phase 4 — move lorebook projection as one unit

1. Add `prompting/lorebook.py`.
2. Move the effective-layer projection, keyword and Director selection,
   catalog construction, rendering, scan-depth constants, and every lorebook
   block builder into it.
3. Move `agentic_lorebook_active` into `features/lorebook/`.
4. Update the lorebook feature facade, proposals, pipeline context and passes,
   API effective views, workflow toolkit, tests, and package exports.
5. Update database docstrings and
   `docs/architecture/dynamic-worlds.md` to the new canonical module.
6. Delete `inference/lorebook.py` after all imports migrate.

Acceptance:

- lore selection and rendering have one implementation in
  `prompting.lorebook`;
- constant, depth, keyword, Agentic, replacement, suppression, and off-turn
  outputs match Phase 1;
- the feature gate is reached through `features.lorebook`;
- lower consumers import `prompting.lorebook`, not the feature facade.

### Phase 5 — co-locate pass instruction contracts

1. Add `pipeline/passes/_prompting.py` for shared tool-call instruction
   formatting and reasoning guidance.
2. Add `pipeline/passes/director/prompts.py` for combined/per-fragment
   Director prompts and Agentic Lorebook selection prompts.
3. Add `pipeline/passes/director/direction_note_prompts.py` for direction-note
   instructions and block rendering.
4. Add `pipeline/passes/editor/prompts.py` for Editor and feedback
   instructions.
5. Add `pipeline/passes/world_change_prompt.py` for the Dynamic Worlds
   instruction.
6. Update each pass to import its local contract and update focused tests.
7. Remove moved names from `inference/__init__.py`.
8. Delete `inference/prompt_builder.py` after all repository consumers migrate.

Acceptance:

- all Phase 1 prompt strings remain unchanged;
- each instruction builder is imported only by its owning pass and tests;
- `inference/` contains no Orb-domain instruction prose.

### Phase 6 — separate tool contracts from transport

1. Add `inference/schema.py`, move `strictify_schema` into it, and update both
   provider adapters and tests.
2. Add `prompting/tool_schemas.py` and move all built-in schema/choice objects
   and dynamic schema builders into it.
3. Add `prompting/tool_catalog.py` with:
   - `BUILTIN_TOOL_ORDER` and derived `BUILTIN_TOOL_NAMES`;
   - catalog lookup and membership operations;
   - `enabled_schemas` with its existing signature;
   - workflow tool registration and removal operations;
   - read-only live compatibility views for `TOOLS` and
     `STANDALONE_TOOLS`.
4. Build the initial catalog from `BUILTIN_TOOL_ORDER`; built-in population
   uses no import-time registration side effects.
5. Update workflow replacement to remove orphaned tools through the catalog
   operation.
6. Update workflow test fixtures to restore registration state through catalog
   operations.
7. Add `pipeline/tools.py` with
   `DIRECTOR_LOOP_TOOL_NAMES = frozenset({"direct_scene"})`, replace
   `PRE_WRITER_TOOLS` consumers, and remove `POST_WRITER_TOOLS` and its
   partition assertion.
8. Update pipeline configuration, pass prompt lookups, settings filtering,
   forced-call helpers, document audit, workflow toolkit, tests, docstrings, and
   comments to the new owners and terminology.
9. Delete `inference/tool_registry.py` after all imports migrate.

Acceptance:

- built-in tool bytes equal the Phase 1 baseline at the cached-base and raw
  transport boundaries;
- dynamic override, registration, replacement, orphan-removal, standalone, and
  forced-offer paths preserve their Phase 1 order and bytes;
- production code mutates tool-catalog state only through catalog operations;
- toolkit compatibility views reject direct mutation;
- provider strictification output is unchanged.

### Phase 7 — finish facades and documentation

1. Restrict `inference/__init__.py` to model/runtime contracts.
2. Give `prompting/__init__.py` a small facade for genuinely shared contracts;
   specialized consumers import canonical submodules.
3. Remove all repository imports of:
   - `inference.prompt_builder`;
   - `inference.group_context`;
   - `inference.lorebook`;
   - `inference.tool_registry`;
   - moved names from the flat `inference` facade.
4. Update architecture documents, test descriptions, code comments, and
   docstrings that identify the previous owners.
5. Run the dependency checker and the full validation suite.

Acceptance:

- `inference/` has no dependency on `prompting/` or an upper layer;
- `prompting/` has no dependency outside `core/`;
- one AST checker is the source of truth for backend dependency edges;
- every moved source module has been deleted;
- no compatibility re-export recreates the old dependency boundary.

## 5. Validation

Run focused checks after each phase:

| Phase | Required focused checks |
|---|---|
| 0 | backend layer checker, `test_backend_layers.py`, `test_import_layering.py` |
| 1 | all characterization tests added or strengthened in the phase |
| 2 | autocomplete and local-ML unit/integration tests |
| 3 | group context, prefix, attachment, Scene Direction, context-size, off-turn prefix, and KV entry-point tests |
| 4 | lore matching, Agentic Lorebook, Dynamic Worlds, constant/depth lore, off-turn prefix, and API effective-view tests |
| 5 | interactive fragments, Director, lore selection, feedback, direction notes, Editor, world proposal, and KV invariant tests |
| 6 | tool catalog, provider structured calls, workflow registry, forced calls, offer-tools blob, KV invariant, and real-stack tests |
| 7 | all affected tests above plus import and layer checks |

For Phases 3–6, compare the Phase 1 prompt/tool fixtures before accepting the
commit. A relocation commit produces no fixture change.

Final validation from the repository root:

```sh
./scripts/format_backend.sh
./scripts/format_frontend.sh
./scripts/lint.sh
./scripts/tests.sh all
```

Review the final diff after formatting. The refactor is complete when all
acceptance checks pass and the public HTTP, SSE, persistence, frontend, and
workflow-toolkit contracts remain unchanged.
