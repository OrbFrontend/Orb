# Prompting boundary

`backend/prompting/` owns deterministic, provider-independent model-facing
construction. A module belongs there only when it:

1. deterministically transforms caller-supplied data;
2. constructs model-facing content or a canonical projection required by it;
3. is shared by multiple upper-layer consumers or the stable workflow toolkit;
4. performs no I/O, model call, persistence, or feature/pass enablement decision.

Instruction prose and result contracts used by one pipeline pass stay with that
pass. Feature enablement stays in its feature slice. Provider adaptation,
retries, structured-output normalization, cache mechanics, and local runtimes
stay in `inference/`.

## Allowed dependency graph

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

`features/` and `workflows/` are siblings and do not import one another.
Feature slices do not import peer slices. The exact matrix is enforced by
`scripts/check_backend_layers.py`; every Python-bearing top-level backend
package must be classified there.

## Byte and order contracts

Prompt text, whitespace, delimiters, message order, parameter order, macro
resolution timing, seeds, and stored random choices are public behavior for
this refactor and must remain byte-for-byte stable.

Built-in tools use this order:

```python
(
    "direct_scene",
    "editor_apply_patch",
    "editor_rewrite",
    "give_feedback",
    "record_direction_note",
    "select_lorebook",
    "propose_world_changes",
)
```

Enabled built-ins preserve that order. Workflow tools append in registration
order, and re-registering a workflow tool preserves its position. Schema
property order and insertion-order-preserving JSON serialization are part of
the transport contract.
