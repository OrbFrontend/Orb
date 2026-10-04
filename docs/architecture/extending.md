# Where to Extend

This page maps each kind of change to the code that owns it and the contract a
fork needs to preserve. The dependency rules are in
[Backend layers and prompting](prompting.md#allowed-dependency-graph); the
`scripts/check_backend_layers.py` and `scripts/check_frontend_layers.py` checks
enforce them.

| Change | Start here | Contract to read |
|---|---|---|
| Optional processing, media, or message actions | `backend/workflows/<id>/` and `frontend/workflows/<id>/` | [Secondary workflows](secondary-workflow.md) |
| Core turn behavior or a new pass | `backend/pipeline/` | [Prompting boundary](prompting.md), [KV cache reuse](kv-cache.md), and [SSE stream](sse-stream.md) |
| Provider transport or request adaptation | `backend/inference/` | [Endpoint routing](endpoints.md) |
| Persisted fields or defaults | `backend/database/` | [Schema change checklist](database.md#changing-the-schema) |
| HTTP endpoints | `backend/api/routes/` and `backend/api/schemas.py` | Register the router in `backend/api/routes/__init__.py` |
| Failure responses and warnings | `backend/api/errors.py`, `backend/pipeline/failures.py`, and `frontend/errors.js` | [Shared failure handling](sse-stream.md#shared-failure-handling) |
| Core UI | `frontend/`, with shared state in `state.js` | Module layers in `scripts/check_frontend_layers.py` and action registration in `actions.js` |

## Workflow or core

An optional feature that fits the workflow hooks belongs in a workflow. Backend
plug-ins use named exports from `backend.workflows.toolkit`; frontend plug-ins
use `/static/workflow_api.js`. When a plug-in needs a capability the facade
lacks, add it to the facade and its contract checks rather than importing past
it. [Frontend integration](secondary-workflow.md#frontend-integration) describes
the additive-only rule for the frontend facade.

## Asynchronous UI work

Asynchronous UI work captures its resource id before awaiting. Core chat work
keeps its data in `conversationState(cid)` and registers long operations through
`operations.js`; workflow renders use the job helpers in the facade.
[Concurrent ownership and deletion](sse-stream.md#concurrent-ownership-and-deletion)
explains ownership, Stop, and reconciliation. Orb runs in one uvicorn process,
and its in-memory locks are not shared across workers.

## Tests

Backend integration tests use temporary databases and a shared fake LLM in
`tests/integration/conftest.py`. Frontend tests live in `tests/frontend/` and run
with `node --test tests/frontend/*.test.mjs`. Add regression coverage for changed
contracts and behavior, and reuse the existing fixtures rather than calling live
providers or downloaded models.
