# AGENTS.md — Orb Codebase Guide

Keep this file focused on durable project conventions. Put feature-specific behavior in the relevant code, tests, or architecture documentation.

## Project

Orb is an agentic roleplay and writing application with a Python/FastAPI backend and a vanilla JavaScript frontend. The backend uses Python 3.11+, aiosqlite, SQLite, and uvicorn. Conversation generation runs through optional Director, Writer, and Editor passes.

## Backend layout

The backend is split into layers. Dependencies point downward only:

1. `api/` — HTTP routes and schemas
2. `pipeline/` — conversation turn orchestration
3. `features/` — self-contained application features
4. `workflows/` — pluggable secondary workflows
5. `inference/` and `analysis/` — model access, prompt assembly, and pure text analysis
6. `database/` — schema, migrations, queries, and row models
7. `core/` — dependency-free shared utilities and types

Lower layers must not import higher layers or peer feature slices. Use dependency inversion when a lower layer needs higher-layer behavior. Keep pure logic separate from integration code and persistence.

Feature slices should expose a small facade, keep local contracts near their logic, and persist through the database layer rather than reaching into unrelated features.

Before changing prompt assembly, pass ordering, tool schemas, or streaming behavior, read the relevant document in `docs/architecture/`.

## Backend conventions

- Keep database row shapes in `database/models.py` and use them at query boundaries.
- Type SQLite flags as `int` (`0` or `1`), not `bool`.
- Decode JSON columns at the boundary where they are read; keep free-form JSON untyped unless a contract is needed.
- Keep Pyright at zero errors. Prefer widening a consumer to `Mapping` or `Sequence` over adding an ignore.
- When changing the schema, update the schema definition, models, API schemas where applicable, seeds, and migrations together.
- Add routes under `api/routes/` and register their router in `api/routes/__init__.py`.
- Preserve public API and SSE contracts unless the change explicitly includes a contract update.

## Frontend conventions

- Use vanilla ES modules and keep shared state in `state.js`.
- Keep streaming behavior in the stream modules and route chat generation through the shared stream helper.
- Workflow modules may import their workflow API and local modules, but should not reach into application internals.
- Use registered actions and `data-*` attributes for UI events. Do not add globals or inline event handlers.
- Keep frontend layer checks passing.

## Validation

Use the repository scripts from the project root:

```sh
./scripts/format_backend.sh
./scripts/format_frontend.sh
./scripts/lint.sh
./scripts/tests.sh all
```

Run the narrowest relevant checks while iterating, then review the final diff. Prefer `rg` and `rg --files` for code search.

## Change checklist

1. Find the nearest architecture note, contract, or test before changing behavior.
2. Keep imports within the layer rules and keep responsibilities separated.
3. Update related contracts, schema files, migrations, and tests together.
4. Format, lint, test, and inspect the diff before handing off.
