# Workflow development guide

Navigation map for authoring a workflow. Reader is assumed to know the rest of Orb's backend (FastAPI + aiosqlite, three-pass pipeline in `backend/pipeline/orchestrator.py`) and frontend (vanilla JS modules mutating the global `S` object in `frontend/state.js`), and to be new to the workflow framework. Every section points at code; build the mental model from the cited source.

---

## 1. What a workflow is

A workflow is a Python record in the process-local registry -- one record per workflow id -- plus zero or more hook bindings into the turn pipeline and HTTP routes. Workflows can:

- Augment the in-turn pipeline (pre/post hooks).
- Emit out-of-turn HTTP responses (on-demand trigger).
- Produce per-message byte artifacts persisted in `workflow_attachments`. The artifact route surface is regenerate / reroll-gen (produce new bytes), rehydrate (re-synthesize in place), and activate / delete / access (lifecycle and access-tracking).
- Carry state across four DB-backed tiers (conversation, message, character, config) plus one in-memory per-turn scratch tier.
- Ship a frontend module that registers renderers (message buttons, attachment widgets, inspector/tools-panel cards -- a config panel is just a tools-panel renderer), click/text-effect/SSE handlers.

Built-in registered workflows: `tts` (`backend/workflows/tts/`, `frontend/workflows/tts/`) and `format_consistency` (`backend/workflows/format_consistency/`, `frontend/workflows/format_consistency/`). `tts` binds four of the five hook types (post-pipeline, on-demand, regenerate, reroll-gen -- not pre-pipeline) and uses the character and config state tiers; cross-reference it as the full worked example. `format_consistency` is the minimal example: a single post-pipeline hook (priority `-10`, so it runs before any artifact hook like TTS and they synthesize from the normalized text) that calls the deterministic RP-markup normalizer in `backend/analysis/format_consistency.py` via the toolkit, produces no artifacts, no tools, and no config -- its former `enabled` config flag is gone, replaced by the framework per-workflow on/off toggle (sec. 3.7), so the hook simply runs whenever the workflow is enabled. Its frontend module does nothing but register a one-line Tools-panel card body (a description); the on/off toggle itself is framework-rendered for every manifest workflow.

---

## 2. File map

### Backend (`backend/workflows/`)

| Path | Role |
|---|---|
| `__init__.py` | Package re-exports + workflow registration site (`register_workflow` + `subscribe` calls + final `finalize_registry()`). |
| `registry.py` | `Workflow` dataclass, `Subscription`, registration, lookup, storage wrappers. |
| `contracts.py` | `HookType` enum, Ctx dataclasses (`PreCtx`, `PostCtx`, `OnDemandCtx`, `RegenCtx`, `RerollGenCtx`), `ToolSpec`, `_readonly`. |
| `toolkit.py` | Stable import surface for workflow authors -- LLM client, prompt builders, audit, DB readers, state stores, locks, `forced_tool_call`, `insert_workflow_attachment`. Full list in sec. 6. |
| `_forced_call.py` | `forced_tool_call(...)` -- one-shot single-tool-call helper. |
| `attachment_cache.py` | Byte cache: total byte budget with LRU-3 eviction ordering, flat sibling groups, validation, public insert/rehydrate/access/active/delete. |
| `tts/` | Shipped TTS workflow (registration, hooks, synth, engine adapters). |

Adjacent backend pieces a workflow author touches:

| Path | Role |
|---|---|
| `backend/core/locks.py` | `workflow_state_lock`, `workflow_character_state_lock`, `workflow_config_lock`. |
| `backend/api/routes/workflows.py` | Workflow HTTP routes. (`_workflow_root_lock` lives in `backend/api/deps.py`.) |
| `backend/pipeline/workflow_bridge.py` | The pipeline↔workflows seam: pre-pipeline hook loop (`_iterate_pre_pipeline_hooks`) + post-pipeline hook loop (`_run_post_pipeline`, over `iter_subscriptions(HookType.POST_PIPELINE)`) + `_stage_workflow_attachment`. |
| `backend/pipeline/persistence.py` | `_persist_result` (writes the assistant row + staged attachments / message state) + `_consume_pipeline` (drains the SSE stream, persists, emits `done`). |
| `backend/database/queries/workflow_attachments.py` | Raw row INSERT (`insert_workflow_attachment_row`) -- no budget/eviction; the cache wraps this. |
| `backend/database/migrations/0020_workflows.py` | Schema for `workflow_attachments` + per-scope `workflow_state` columns (conversations / messages / character_cards) + `workflow_config` + `attachment_cache_budget_bytes` + `attachment_access_counter`. |
| `backend/database/schema.py` | Mirror of post-migration shape for fresh installs. |

### Frontend (`frontend/`)

| Path | Role |
|---|---|
| `state.js` | `S.workflow*` slots + exported `registerWorkflowPipeline` / `registerTextEffect` / `registerClickHandler`. |
| `workflow_loader.js` | Boot loader: `loadWorkflowModules` dynamic-imports each manifest entry's `index.js` in manifest order. (Manifest itself fetched by `loadWorkflowManifest` in `chat.js`.) |
| `chat.js` | SSE dispatch, workflow widget rendering, phase pill, reasoning rail, refetch helpers, `window.workflow*` handlers. |
| `default_widget.js` | Fallback MIME-routed renderer (image / audio / video; else a download link). |
| `workflow_segmentation.js` | `.seg` span wrapper + `messageSegments(msgId)` + `segDescriptor`. |
| `workflow_text_effects.js` | `startTextEffect` / `clearTextEffect` + paint. |
| `workflow_text_interaction.js` | Click routing, multi-claimant chooser DOM. |
| `audio_player.js` | `playAudio` + channel controls + `onChannel` + `channelState`. |
| `audio_schedule.js` | Pure scheduling math (normalize / build / locate / reschedule). |
| `audio_transport.js` | Transport bar mount: channel selector plus one control row bound to the selected channel. |
| `tabLock.js` | `broadcastWorkflowMutation` for cross-tab refresh. |
| `app.js` | Boot wiring: imports + calls at startup `loadWorkflowManifest` + `initWorkflowMutationListener` (from `chat.js`), `loadWorkflowModules` (from `workflow_loader.js`), `initWorkflowTextInteraction` (from `workflow_text_interaction.js`), `initAudioPlayer` (from `audio_transport.js`). `window.workflow*` inline handlers themselves live in `chat.js`. |
| `workflows/<id>/` | Per-workflow modules served from `/static/workflows/<id>/`. |
| `workflows/tts/` | Shipped TTS frontend (index, widget, karaoke, config_panel, extract, tts.css). |

---

## 3. Workflow declaration and registration

Declaration and registration are two distinct steps; `registry.py` hosts both the `Workflow` dataclass and the registration functions:

1. **Declare** the `Workflow` data record. Author calls `Workflow(id=..., display_name=..., ...)` inside the workflow's own subdir `__init__.py`. No registration happens yet.
2. **Register + bind hooks**. Author calls `register_workflow(w)` + one `subscribe(w.id, HookType.X, fn)` per hook + `finalize_registry()`. ALL three calls live in `backend/workflows/__init__.py`, NOT in the workflow's own subdir.

### 3.1 `Workflow` dataclass -- data shape only (`registry.py`)

Authors construct one of these and never touch `subscriptions` -- that field is framework-owned; `subscribe()` appends to it during registration.

```
@dataclass class Workflow:
  id: str                                   # required, process-local primary key
  display_name: str                         # required, surfaced in manifest
  tools: list[ToolSpec]                     # default-factory []
  config_defaults: dict                     # default-factory {}
  config_schema: Optional[dict]             # default None
  produces_artifacts: bool                  # default False
  subscriptions: list[Subscription]         # default-factory []; framework-owned
```

Live example: shipped TTS builds its `Workflow(...)` instance at `backend/workflows/tts/__init__.py`.

### 3.2 `ToolSpec` (`contracts.py`)

```
@dataclass class ToolSpec:
  name: str            # must equal schema["function"]["name"]
  schema: dict         # OpenAI-style tool schema
  choice: dict         # pre-built tool_choice payload
  standalone: bool     # default True; keeps tool out of pipeline union
```

### 3.3 `HookType` (`contracts.py`)

| Member | Value | Dispatch | Fires from |
|---|---|---|---|
| `PRE_PIPELINE` | `"pre_pipeline"` | Fan-out (every subscriber, priority-ascending) | During the turn, inside the pipeline |
| `POST_PIPELINE` | `"post_pipeline"` | Fan-out | During the turn, inside the pipeline |
| `ON_DEMAND` | `"on_demand"` | Single-dispatch by workflow id | `POST .../conversations/{cid}/workflows/{workflow_id}/trigger` |
| `REGENERATE` | `"regenerate"` | Single-dispatch by workflow id | `POST .../workflow-attachments/{aid}/regenerate` |
| `REROLL_GEN` | `"reroll_gen"` | Single-dispatch by workflow id | `POST .../workflow-attachments/{aid}/reroll-gen` and `.../{aid}/rehydrate` |

Single-dispatch hooks fire from their own HTTP routes, never from the turn pipeline. Note the name clash on `regenerate`: the message-level route `POST .../messages/{msg_id}/regenerate` reruns the three-pass pipeline via `handle_regenerate`, firing PRE_PIPELINE/POST_PIPELINE but no single-dispatch hook. The `REGENERATE` hook fires only from the attachment-level `POST .../workflow-attachments/{aid}/regenerate` route (`main.py`).

### 3.4 Registration sequence

The package `__init__.py` imports each workflow's instance plus its hook callables and runs the three registration calls against them. Hooks are aliased on import (`_tts_*`, `_fc_*`) because both shipped workflows define an identically-named `post_pipeline` hook -- without the alias the second import would shadow the first in the shared package namespace.

Live shape -- imports and registration calls in `backend/workflows/__init__.py` (two workflows; `format_consistency` binds only `POST_PIPELINE`, at a negative priority so it runs first):

```
from .format_consistency import format_consistency_workflow           # the Workflow(...) instance
from .format_consistency.hooks import post_pipeline as _fc_post_pipeline
from .tts import tts_workflow                                          # the Workflow(...) instance
from .tts.hooks import (
    on_demand as _tts_on_demand,
    post_pipeline as _tts_post_pipeline,
    regenerate as _tts_regenerate,
    reroll_gen as _tts_reroll_gen,
)

register_workflow(tts_workflow)                                        # step 1
subscribe(tts_workflow.id, HookType.POST_PIPELINE, _tts_post_pipeline)      # step 2 (one per hook)
subscribe(tts_workflow.id, HookType.ON_DEMAND,    _tts_on_demand)
subscribe(tts_workflow.id, HookType.REGENERATE,   _tts_regenerate)
subscribe(tts_workflow.id, HookType.REROLL_GEN,   _tts_reroll_gen)

register_workflow(format_consistency_workflow)                         # a second workflow
subscribe(format_consistency_workflow.id, HookType.POST_PIPELINE, _fc_post_pipeline, priority=-10)
finalize_registry()                                                    # step 3 (keep at file bottom)
```

- `register_workflow(w)` -- `registry.py`. Idempotent on `w.id`; re-registering the same id preserves the original insertion position, so manifest order stays stable across reloads (docstring `registry.py`). Raises `ToolNameCollision` if any declared tool name is a built-in, or if a newly-claimed name (one not already owned by a prior registration of this id) collides with another workflow's tool. Both checks run before any mutation, so a rejected call leaves the registry, `TOOLS`, and `STANDALONE_TOOLS` untouched. On re-registration the new `tools` list is diffed against the prior one: names new to this registration are registered, dropped names are removed from `TOOLS`/`STANDALONE_TOOLS`, and names in both have schema/choice/standalone overwritten (`registry.py`).
- `subscribe(workflow_id, hook_type, fn, *, priority=0)` -- `registry.py`. Appends a `Subscription` to `w.subscriptions`. Raises `LookupError` if id unknown, `ValueError` on duplicate hook for same id, `ValueError` on `REGENERATE`/`REROLL_GEN` without `produces_artifacts=True`.
- `finalize_registry()` -- `registry.py`. Every `produces_artifacts=True` workflow MUST also have `REGENERATE` and `REROLL_GEN` bindings; missing either raises `WorkflowMandateError` at import time.

### 3.5 Lookups (`registry.py`)

- `get_workflow(workflow_id) -> Workflow | None`.
- `get_subscription(workflow_id, hook_type) -> Subscription | None`. Collapses "unknown id" and "unbound hook" to None.
- `iter_subscriptions(hook_type) -> list[Subscription]`. Priority-ascending, registration-order tie-break (stable sort).
- `list_workflows() -> list[Workflow]`. Registration order.
- `workflow_has_hook(w, hook_type) -> bool`.

### 3.6 Manifest route

`GET /api/workflows` (`main.py`). Returns a list; each entry `{id, display_name, config_schema, config_defaults}`. Frontend fetches once at boot via `loadWorkflowManifest` (`chat.js`) into `S.workflowManifest`.

### 3.7 Enablement (per-workflow on/off)

Every registered workflow can be switched off without code changes. Two `settings` columns hold the state (`backend/database/schema.py`); the registry `Workflow` record carries no enabled flag (it is rebuilt from code at import and would lose the bit on restart), so the `settings` row is the single source of truth.

| Column | Type | Default | Meaning |
|---|---|---|---|
| `workflows_globally_enabled` | INTEGER | `1` | Master switch for the whole subsystem. |
| `workflow_enabled` | TEXT (JSON `{wid: bool}`) | `'{}'` | Per-workflow override; a missing key means enabled. |

`get_settings` decodes `workflow_enabled` to a dict. Effective state is the pure predicate `effective_workflow_enabled(workflow_id, settings)` (`backend/workflows/enablement.py`): `global_on AND local_on`, each defaulting to enabled when its value is absent. A non-dict `workflow_enabled` coerces to `{}` (degrade-to-enabled) rather than raising -- the predicate runs once per subscription per turn, so a decode regression must never crash the turn.

Control:

- Master: `PUT /api/settings` with `workflows_globally_enabled` (in `SettingsUpdate`).
- Per-workflow: `POST /api/workflows/{wid}/enabled` (sec. 8.1). `workflow_enabled` is deliberately NOT in `SettingsUpdate` -- a full-column settings write would clobber a concurrent tab's flip of a different workflow, whereas the route's `set_workflow_enabled` does a single atomic per-key `json_set` with no read-modify-write window (hence no lock).

Enforcement -- each site reads the per-turn / per-request `settings` snapshot it already holds:

- PRE_PIPELINE and POST_PIPELINE fan-out skip a disabled workflow's subscription, logging `"... hook suspended (disabled)"` (sec. 7.3, 7.4).
- `_resolve_pipeline_config` strips a disabled workflow's tool names from the per-turn tool blob via `disabled_workflow_tool_names(settings)` (`backend/pipeline/config.py`) -- a no-op today, since no disabled workflow declares tools.
- The routes that fire a workflow hook -- `/trigger` (ON_DEMAND), `/regenerate` (REGENERATE), `/reroll-gen` and `/rehydrate` (REROLL_GEN) -- return 404 when the owning workflow is disabled, checked before the route takes its lock and before the hook runs (sec. 8.1). The hookless consumption routes (`/activate`, `/delete`, `access`) and the manifest / config / enabled routes are never gated, so a disabled workflow's existing artifacts stay viewable and re-enabling restores full function.

Frontend mirror: `effectiveWorkflowEnabled(wid)` (`state.js`) applies the same truth table off `S.settings`; the four production registries are filtered by it at their read sites (sec. 11.3), and the Secondary tab renders a master switch plus one per-workflow checkbox (sec. 14.5). Both columns are in `PRESERVED_COLUMNS` (`backend/database/preset_schema.py`), so applying a configs preset never silently re-enables a locally-disabled workflow.

---

## 4. Hook context dataclasses (`contracts.py`)

All Ctx are `@dataclass(frozen=True)`. Mutable fields routed through `_readonly(...)` (recursive: `dict -> MappingProxyType`, `list/tuple -> tuple`, `set/frozenset -> frozenset`, `bytearray -> bytes`). `turn_scratch`, `client`, `kv_tracker` stay unwrapped.

### 4.1 PreCtx -- paired with PRE_PIPELINE

| Field | Type | Note |
|---|---|---|
| `conversation_id` | str | |
| `history` | tuple | Read-only-wrapped messages. |
| `last_user_message` | str | |
| `settings` | MappingProxyType | |
| `prefix` | tuple | Base prefix, before pre-pipeline `system_prompt` extras. |
| `enabled_tools_pre_merge` | MappingProxyType | Every value forced False when `agent_on` is false (keys kept). |
| `turn_scratch` | dict | Mutation channel; same identity across PRE + POST in the same turn. |
| `client` | Any | Per-turn LLMClient. |
| `kv_tracker` | Any | Per-turn cache aggregator. |
| `schema_overrides` | MappingProxyType | Dynamic-schema map; today `{"direct_scene": ...}`. |
| `character_id` | str \| None | |
| `character` | MappingProxyType \| None | Read-only character card view. |

### 4.2 PostCtx -- paired with POST_PIPELINE

Same shape with these substitutions:

| Field | Note |
|---|---|
| `draft` | str -- current draft, updated by prior hooks' `draft_replaced`. |
| `effective_msg` | str -- the current turn's user message. |
| `director_output` | MappingProxyType -- `{active_moods, raw, calls, latency, extra_fields, progressive_fields}`. |
| `enabled_tools` | MappingProxyType -- merged pipeline tool-enable map (replaces `enabled_tools_pre_merge`). |
| `prefix` (note differs) | Final pipeline prefix; pre-pipeline extras already appended. |
| `history` | tuple -- same read-only prior-message list PreCtx received; excludes this turn's user message and the in-flight assistant message (the current user message is `effective_msg`). |
| No `last_user_message` / `enabled_tools_pre_merge`. |

### 4.3 OnDemandCtx -- paired with ON_DEMAND

Fields: `conversation_id`, `history`, `last_user_message`, `settings`, `client`, `character_id`, `character`. No `turn_scratch`, `kv_tracker`, `prefix`, `enabled_tools`, `schema_overrides`.

### 4.4 RegenCtx -- paired with REGENERATE

Fields: `conversation_id`, `message_id`, `attachment_id`, `original_attachment`, `history` (strictly before anchor message), `last_user_message`, `settings`, `client`, `character_id`, `character`. No turn-scoped fields.

### 4.5 RerollGenCtx -- paired with REROLL_GEN

Fields: `conversation_id`, `message_id`, `attachment_id`, `original_attachment`, `settings`, `client`, `prior_consumption_metadata`. No history, no character. Shared backend for `/reroll-gen` and `/rehydrate`; the hook does not branch on route.

### 4.6 Hook callable signatures (`contracts.py`)

```
PreHook       = Callable[[PreCtx],                 AsyncIterator[dict]]
PostHook      = Callable[[PostCtx],                AsyncIterator[dict]]
OnDemandHook  = Callable[[OnDemandCtx, dict],      Awaitable[dict]]
RegenHook     = Callable[[RegenCtx, dict],         Awaitable[list[dict]]]
RerollGenHook = Callable[[RerollGenCtx, dict, str], Awaitable[bytes | tuple[bytes, dict | None]]]
```

PRE/POST hooks are async generators yielding dict events. The rest are awaited and return a single value (dict / list / bytes-or-tuple).

---

## 5. Locks

### 5.1 Shared in-process locks (`backend/core/locks.py`)

| Lock | Key | Scope |
|---|---|---|
| `workflow_state_lock(cid, wid)` | `(cid, wid)` | Per `(conversation, workflow)` |
| `workflow_character_state_lock(character_id, wid)` | `(character_id, wid)` | Per `(character_card, workflow)` |
| `workflow_config_lock()` | (none) | Process-global; serializes all `workflow_config` RMW across every workflow id |

Non-reentrant `asyncio.Lock`s. Nesting order at every site: `workflow_state_lock` outer, `workflow_character_state_lock` inner.

### 5.2 `_workflow_root_lock(root_id)` (`backend/api/deps.py`)

Distinct, int-keyed space (`dict[int, asyncio.Lock]`), keyed on the root attachment id. Held by the five attachment-mutating routes: `/regenerate`, `/reroll-gen`, `/rehydrate`, `/activate`, `/delete`. It serializes concurrent edits to one attachment's variant group (the root row plus its sibling variants), so two callers cannot interleave a read-modify-write on the same group. It is never nested with `workflow_state_lock` or `workflow_character_state_lock` at any call site and so sits outside their ordering rule.

### 5.3 Acquisition sites

| Lock | Held by |
|---|---|
| `workflow_state_lock` (outer) + `workflow_character_state_lock` (inner) | PRE-pipeline iterator (`workflow_bridge.py`), POST-pipeline iterator (`workflow_bridge.py`), `/trigger` route (`main.py`). Workflow code doing read-modify-write on workflow_state acquires the same locks via the `toolkit` re-export (`backend/workflows/toolkit.py`). |
| `workflow_config_lock` | `PUT /api/workflows/{workflow_id}/config` (`main.py`). Workflow code doing read-modify-write on workflow_config acquires it via the `toolkit` re-export. |

---

## 6. Toolkit (`backend/workflows/toolkit.py`)

The pinned author import surface. Importing from anywhere else inside `backend` is discouraged.

### 6.1 LLM + prompt + audit helpers (re-exports)

`LLMClient`, `parse_tool_calls`, `reasoning_cfg`, `Macros`, `format_report`, `run_audit`, `build_prefix`, `compute_lorebook_injection_block`, `compute_style_injection_block`, `format_message_with_attachments`, `STANDALONE_TOOLS`, `TOOLS`, `enabled_schemas`.

### 6.2 Read-only core DB helpers (re-exports)

`get_character_card`, `get_conversation`, `get_director_fragments`, `get_director_state`, `get_message_by_id`, `get_messages`, `get_mood_fragments`, `get_phrase_bank`, `get_user_personas`.

Mutating DB helpers (`add_message`, director-state writers, etc.) are intentionally NOT re-exported.

### 6.3 State stores (re-exports from `registry.py`)

```
get_workflow_state(cid, wid)              -> dict | None
set_workflow_state(cid, wid, payload)
get_workflow_message_state(mid, wid)      -> dict | None
set_workflow_message_state(mid, wid, payload)
get_workflow_character_state(char_id, wid) -> dict | None
set_workflow_character_state(char_id, wid, payload)
get_workflow_config(wid)                  -> dict (default-fallback)
set_workflow_config(wid, payload)
```

Passing `payload=None` to a `set_*` state writer deletes that slot. `set_workflow_config(wid, {})` clears the persisted slot, so the next `get_workflow_config(wid)` returns a fresh copy of the workflow's `config_defaults`. None of these acquire locks; callers doing a read-modify-write MUST hold the matching lock from sec. 5.

### 6.4 `forced_tool_call` (`_forced_call.py`)

```
async def forced_tool_call(
    *,
    client, prefix, tail_messages, tool_name, settings,
    pass_id=None, enabled_tools=None, schema_overrides=None,
    kv_tracker=None, reasoning_on=True, temperature=0.25, max_tokens=8192,
) -> AsyncIterator[dict]
```

One-shot single-tool forced LLM call. Never raises: a missing tool call, a parse failure, or any exception raised while consuming the stream all degrade to an empty-args result, `{"type": "result", "args": {}}`. Reasoning deltas yield `{"event": "reasoning", "data": {"pass": pass_id, "delta": ...}}` only when `pass_id` is set.

KV cache reuse: forward the pipeline's `prefix`, `enabled_tools` (or `enabled_tools_pre_merge`), `schema_overrides`, and `kv_tracker` from the ctx. This makes the prefix and message bytes match the turn's, so the forced call reuses the turn's KV cache.

Terminal yield: `{"type": "result", "args": <dict>}` -- the parsed tool arguments.

### 6.5 `overlay_enable_tools(base, contribution) -> dict[str, bool]` (`registry.py`)

Fresh `dict` copy of `base` with `contribution`'s True entries merged. Accepts `set` / `frozenset` (presence => True), `Mapping[str, bool]` (True entries kept, False dropped), or `None` (returns a fresh copy of `base` unchanged); an empty `set`/`Mapping` likewise yields an unchanged copy. Use to compute the merged enable map for `forced_tool_call`.

### 6.6 `insert_workflow_attachment` (re-export from cache)

The only attachment writer exposed to authors. See sec. 9.

### 6.7 Workflow locks (re-exports from `backend.core.locks`)

`workflow_state_lock(cid, wid)`, `workflow_character_state_lock(character_id, wid)`, and `workflow_config_lock()`. Hold the matching lock across a read-modify-write on the corresponding state tier (sec. 5, sec. 10). `workflow_character_state_lock` nests inside `workflow_state_lock` (conversation lock outer, character lock inner). There is no dedicated message-state lock: serialize a message-state RMW under `workflow_state_lock(cid, wid)` of the message's owning conversation.

---

## 7. In-turn integration (`backend/pipeline/workflow_bridge.py`)

### 7.1 Turn entry points

| Function | First built-in event | Last event |
|---|---|---|
| `handle_turn` | `user_message_created` | `done` |
| `handle_regenerate` | `director_start` or, when the director block is skipped, `director_done` | `done` |
| `handle_super_regenerate` | same as `handle_regenerate` | `done` |
| `handle_magic_rewrite` | same as `handle_regenerate` | `done` |

All four run PRE-pipeline hooks first. `handle_regenerate` / `handle_super_regenerate` / `handle_magic_rewrite` skip `user_message_created` -- they do not persist a new user row. `done` fires last from `_consume_pipeline` on any turn that completes without raising -- it sits after the pipeline's `try/finally`, so a pipeline exception propagates past it.

`handle_magic_rewrite` is super-regenerate with a user-supplied steering message (the typed direction) in place of the canned one; it runs the same full pipeline and persists a new sibling.

### 7.2 Per-turn shared identities

- `turn_scratch: dict = {}` allocated once per turn. Same object reference into every PreCtx and PostCtx (both the PRE and POST wrap sites pass `turn_scratch=turn_scratch`, no `_readonly`). Writes during PRE visible to POST.
- `schema_overrides: dict = {"direct_scene": build_direct_scene_tool(ctx["director_fragments"])}` -- built per turn, then threaded into pre-pipeline iter, `_run_pipeline`, every pass (`_director_pass`, `_writer_pass`, `editor_pass`), and exposed read-only on PreCtx/PostCtx for `forced_tool_call` reuse.
- `client = LLMClient(...)` built in `_load_pipeline_context`; attached to PreCtx.client / PostCtx.client (raw, not macros-wrapped).
- `kv_tracker` -- per-turn `_KVCacheTracker`; ref-shared across all passes and ctx fields.

### 7.3 PRE_PIPELINE iteration (`_iterate_pre_pipeline_hooks`)

For each subscription (priority-ascending; a disabled workflow's subscription is skipped first, logging `"... pre-pipeline hook suspended (disabled)"` -- sec. 3.7):

1. Acquire `workflow_state_lock(cid, wid)` then `workflow_character_state_lock(character_id or "", wid)`.
2. Build `PreCtx`.
3. `async for ev in sub.callable(pre_ctx)`. Dispatch on `ev.get("type")`:

| Event `type` | Effect |
|---|---|
| `"enable_tools"` | Merge `ev["tools"]` into `accumulators["merged_enabled_tools"]`: `set`/`frozenset` -> each name True; `dict` -> entries whose value is exactly `True`. Names not in `TOOLS`, dict values that are not `True`, and a `tools` payload that is not set/frozenset/dict each drop (the whole event, for a bad payload) with WARNING. |
| `"system_prompt"` | Append `ev["block"]` to `accumulators["extras"]` if it is a non-whitespace `str` (empty/whitespace-only dropped with WARNING). |
| neither | Forward `ev` to SSE stream verbatim. |

Reserved-name rule: any `ev["event"]` that is a string starting with `_` is dropped with WARNING.

Error containment: each subscription's body wrapped in `try / except Exception`. One bad hook is logged-and-skipped.

Post-loop application (entry points and analogues): `extras` non-empty triggers `_build_prefixes(ctx, history, extra_system_blocks=extras)` rebuild. `merged_enabled_tools` is fed to `_run_pipeline(enabled_tools=...)`.

### 7.4 POST_PIPELINE iteration (inside `_run_pipeline`)

For each subscription (a disabled workflow's subscription is skipped first, logging `"... post-pipeline hook suspended (disabled)"` -- sec. 3.7):

1. Acquire `workflow_state_lock(conversation_id or "", wid)` + `workflow_character_state_lock(character_id or "", wid)`.
2. Build `PostCtx`.
3. `async for ev in sub.callable(post_ctx)`. Dispatch on `ev.get("type")`:

| Event `type` | Effect |
|---|---|
| `"draft_replaced"` | One per hook. `ev["draft"]` must be a str differing from current `draft`, else WARNING + drop. On accept: `draft = ev["draft"]`, yield `{"event": "writer_rewrite", "data": {"refined_text": draft}}`. |
| `"attach_artifact"` | Gated on `get_workflow(wid)` resolving with `produces_artifacts` truthy (unknown workflow or unset flag -> WARNING + drop). Validated via `_stage_workflow_attachment`. Survivors appended to local `staged_attachments`. No SSE event at attach time. |
| `"set_message_state"` | `ev["state"]` must be a dict (else WARNING + drop). On accept: staged under the hook's `workflow_id` (last-wins), then written to the new assistant message's per-message state slot in `_persist_result` once the assistant row exists. No SSE event. |
| neither | Forward `ev` to SSE stream. Underscore-prefixed `ev["event"]` dropped. |

Error containment: each subscription wrapped in `try / except Exception`.

### 7.5 `_stage_workflow_attachment(att, workflow_id) -> dict | None`

Inline validator. Required attachment fields:

| Field | Check |
|---|---|
| `filename` | `isinstance(_, str)` |
| `mime` | `isinstance(_, str)` |
| Exactly one of `data` / `path` | XOR (`has_data != has_path`) |
| `data` (if present) | `isinstance(_, (bytes, bytearray))` |
| `path` (if present) | `isinstance(_, str)` |
| `annotation` (if present) | `None` or str |
| `source` | `== f"workflow:{workflow_id}"` |
| `workflow_id` | `== workflow_id` |

On fail: WARNING + return None. On success: shallow-copy + normalize whitespace-only annotation to None + coerce non-dict `consumption_metadata` to None + read `path` (drop key, set `data` = bytes) + reject empty bytes. Never raises.

### 7.6 Reserved event names

The orchestrator owns these `event:` names: built-ins it emits itself, and underscore-prefixed internals it drops before they reach the wire (in both the PRE and POST hook loops). A hook that yields one collides with the orchestrator's own use.

| Event | Notes |
|---|---|
| `user_message_created` | only `handle_turn` |
| `director_start` | |
| `director_done` | |
| `token` | |
| `reasoning` | built-ins; custom pipelines see sec. 13.2 |
| `writer_rewrite` | editor + post-pipeline draft_replaced |
| `editor_done` | |
| `workflow_attachments_rejected` | from `_consume_pipeline` |
| `done` | from `_consume_pipeline` |
| `error` | entry-point guard returns + `except` blocks |
| `_result`, `_editor_reasoning`, `_refined_result` | Internal; never reach SSE wire. |

Any other event name passes through.

`phase_status` is hook-emitted, not reserved: a workflow yields it as a passthrough event to drive the built-in phase pill, and `chat.js` handles it (sec. 13.1).

### 7.7 `_persist_result`

Runs unconditionally (subject to each step's own guard):

1. `db.update_director_state(...)` if `enable_agent` truthy.

Then, only when `resp_text.strip()`:

2. `db.add_message(..., attachments=staged, ...)` -- single transaction. It persists workflow attachments by calling through a registered persister seam (the database layer must not import "up" into `backend.workflows`; `attachment_cache` registers `insert_workflow_attachments` via `register_workflow_attachment_persister` at import time). Returns `(asst_id, rejected_workflow_atts)`.
3. For each post-pipeline `set_message_state` entry, `db.set_workflow_message_state(asst_id, wid, payload)`. The assistant `mid` is first known here; unlocked because the row is not yet the active leaf and no other caller can name it.
4. `db.set_active_leaf(conversation_id, asst_id)`.

Empty `resp_text.strip()` short-circuits steps 2-4 only: no assistant row, no attachments, no message state, returns `(None, [])`. Step 1 has already run regardless.

### 7.8 `_consume_pipeline`

Reads from `_run_pipeline`, dispatches by `event["event"]`:

| event | Effect |
|---|---|
| `"token"` | Accumulate `accumulated_text`; re-yield. |
| `"_result"` | Set `res`. Call `_persist_result`. If rejected non-empty and `asst_id` not None, yield `workflow_attachments_rejected`. NOT re-yielded. |
| `"_editor_reasoning"` | Copy onto `res`. NOT re-yielded. |
| `"_refined_result"` | Overwrite `res["resp_text"]`; rewrite the assistant DB row when one was persisted. NOT re-yielded. |
| anything else | Re-yield verbatim. |

Trailing `yield {"event": "done"}`.

Note: when `resp_text` is empty, `_persist_result` short-circuits (sec. 7.7) and returns `(None, [])`, so any staged `attach_artifact` or `set_message_state` entries are dropped, the former without a `workflow_attachments_rejected` event. An artifact-only or message-state-only hook produces nothing on a turn whose writer emitted no text.

### 7.9 Wire-event order on a normal turn

`user_message_created`? -> PRE passthrough events* -> `director_start`? -> `reasoning(director)`? -> `director_done`? -> `reasoning(writer)`? -> `token`* -> `reasoning(editor)`? -> `writer_rewrite`? -> `editor_done`? -> POST-hook events* (`writer_rewrite` from `draft_replaced`, plus passthrough, interleaved per hook in priority order) -> `workflow_attachments_rejected`? -> `done`.

`?` = conditional. `director_start` and `reasoning(director)` run only when the agent is on and a pre-writer tool is enabled. `director_done` fires unconditionally (outside the director block), absent only when the turn aborts at the post-director stop check. Each `reasoning(pass)` fires only when that pass's reasoning flag is set (director on by default, writer/editor off); `user_message_created` is suppressed when the caller pre-persisted the user row.

---

## 8. HTTP routes (`backend/api/routes/`)

### 8.1 Per-route reference cards

Disabled-workflow gating: the four hook-firing routes -- `/trigger`, `/regenerate`, `/reroll-gen`, `/rehydrate` -- return 404 when the owning workflow is disabled (global or per-workflow), checked before the route takes its lock and before the hook runs (sec. 3.7); `/regenerate`, `/reroll-gen`, and `/rehydrate` first load the conversation and target attachment, since that read resolves the owning workflow id. To the caller this is indistinguishable from a missing handler, and the server log disambiguates. The hookless routes (manifest, config, enabled, activate, delete, access) are never gated.

#### GET `/api/workflows` (manifest)

Handler `api_list_workflows`. No locks. Response: JSON list of `{id, display_name, config_schema, config_defaults}` in registration order. No errors.

#### PUT `/api/workflows/{wid}/config`

Handler `api_set_workflow_config`. Body model `WorkflowConfigUpdate`: `{"config": dict}`, REQUIRED -- missing key is FastAPI 422 before handler. Lock: `workflow_config_lock()`. DB: `set_workflow_config(wid, data.config)` then `get_workflow_config(wid)`. Response: `{"config": <effective>}` (post-write read; empty dict slot falls back to `config_defaults`). 404 if unregistered.

#### GET `/api/workflows/{wid}/config`

Handler `api_get_workflow_config`. No locks. DB: `get_workflow_config(wid)`. Response `{"config": <effective>}`. 404 if unregistered.

#### POST `/api/workflows/{wid}/enabled`

Handler `api_set_workflow_enabled`. Body model `WorkflowEnabledUpdate`: `{"enabled": bool}`, REQUIRED -- a body missing the key is FastAPI 422 before the handler. No lock (the per-key `set_workflow_enabled` write is atomic). DB: `set_workflow_enabled(wid, enabled)` then `get_settings()`. Response: `{"workflow_enabled": <full decoded {wid: bool} map>}`. 404 if unregistered. Ungated -- this is the control that re-enables a suspended workflow. Per-workflow on/off contract: sec. 3.7.

#### POST `/api/conversations/{cid}/workflows/{wid}/trigger`

Handler `api_trigger_workflow`. Body: raw `dict` (default `{}`). Lookup: `get_subscription(wid, HookType.ON_DEMAND)` 404 if None. Outer lock `workflow_state_lock(cid, wid)`; under it, DB reads: `get_conversation(cid)` (404), `get_character_card(card_id)` if any, `get_messages(cid)`, `get_settings()`, then build the `LLMClient`. Inner lock `workflow_character_state_lock(conv.get("character_card_id") or "", wid)`; under it, build `OnDemandCtx` and `await sub.callable(od_ctx, body)`. Returns the hook's return value verbatim (the on_demand contract is a dict). Hook exception -> 500.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate`

Handler `api_regenerate_attachment`. Body: raw `dict`. Pre-lock: `get_conversation(cid)` (404), then `get_workflow_attachment_by_id(aid)` (404 if missing or `message_id != mid`), `get_subscription(att["workflow_id"], HookType.REGENERATE)` (404). `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)`. DB reads (in lock): `get_message_by_id(mid)` (404 if cid mismatch), `get_messages_before(cid, mid)` (history strictly before anchor), `get_settings()`; build LLMClient; `get_character_card(card_id)`; then build `RegenCtx`. `await sub.callable(regen_ctx, body) -> list[dict]`. Non-list coerced to `[]`; non-dict entries dropped silently (logged, not rejected -- a rejection record needs a filename to surface in the UI). Each dict entry is stamped with `workflow_id` and `parent_attachment_id=root_id`. Rejections come from two stages and merge into one list: (1) pre-insert -- `validate_workflow_attachment_shape` failures; (2) at insert -- entries the LRU-budget batch insert refuses (oversize without rehydrate metadata). Survivors batch-insert via `insert_workflow_attachments(mid, fixed)`, which returns `(new_ids, helper_rejected)`. Both rejection sets are projected to `{filename, workflow_id, mime, reason, originating_attachment_id}` (`originating_attachment_id=root_id`). Response: `{"attachments": new_ids, "rejected_workflow_atts": <stage-1 + stage-2>}`. A raise inside the insert (`ValueError | LookupError | OSError`) -> 500.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen`

Handler `api_reroll_gen_attachment`. Body unused. `get_conversation(cid)` (404), `att` (404), `anchor` (404), `sub` (REROLL_GEN, 404). `params` = `att["generation_metadata"]` decoded as JSON, coerced to `{}` on empty / parse fail / non-dict. `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)`. `seed = _generated_seed()` (`secrets.token_hex(16)`). Build `RerollGenCtx` via `_build_reroll_gen_ctx` + LLMClient. `await sub.callable(ctx, params, seed) -> bytes | (bytes, dict | None)` -- normalize via `_split_reroll_gen_result`. Empty/non-bytes => 500. Build new sibling dict: fresh `seed`, inherited `generation_metadata=params`, optional new `consumption_metadata`, `workflow_id=sub.workflow_id`, `parent_attachment_id=root_id`, `filename=att.get("filename") or sub.workflow_id`, `mime=att.get("mime_type") or "application/octet-stream"`, `annotation` copied from `att`. `insert_workflow_attachment(mid, new_attachment)`. Response: `{"attachment_id": new_id, "rejected_workflow_atts": [...0-or-1...]}`.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate`

Handler `api_rehydrate_attachment`. Body unused. Pre-lock: `get_conversation(cid)` (404), `get_workflow_attachment_by_id(aid)` (404 if missing or `message_id != mid`), `get_message_by_id(mid)` (404 if cid mismatch). 409 gates: `att["data_b64"] != EVICTED_MARKER` (already restored), `att["seed"]` empty. `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)`. In-lock re-read of `att`; 409 if `data_b64` no longer evicted (race). 404 if no REROLL_GEN sub. Same `_build_reroll_gen_ctx` + `await sub.callable(ctx, params, seed)` where `seed = att["seed"]` (stored). `_split_reroll_gen_result` normalize. Write via `rehydrate_attachment(aid, bytes, consumption_metadata=...)` (`backend/workflows/attachment_cache.py`) -- in-place UPDATE on the same row. `RehydrateAlreadyDoneError` (subclass of `ValueError`) -> 409. Other `(LookupError, ValueError)` -> 500. Response: `{"attachment_id": aid}` (echoed).

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/activate`

Handler `api_activate_workflow_attachment`. Body: `{"sibling_id": int | None}`. No hook. Pre-lock: `get_conversation(cid)` (404), `get_message_by_id(mid)` (404 if cid mismatch); non-int `sibling_id` rejected 400, including the `bool`-is-`int` case. The URL `aid` is interpreted as the ROOT id (verified inside `set_active_sibling` -- not pre-checked at the route). Lock: `_workflow_root_lock(aid)`. DB: `set_active_sibling(aid, sibling_id, expected_message_id=mid)` (`backend/workflows/attachment_cache.py`). `LookupError` -> 404, `ValueError` -> 400. Response: `{"active_sibling_id": <echoed>}`.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete`

Handler `api_delete_workflow_attachment`. Body: `{"scope": "variant" | "group"}`. No hook. Pre-lock (in order): `get_conversation(cid)` (404), `get_message_by_id(mid)` (404 if cid mismatch), `scope` validation (400, checked before the attachment lookup), `get_workflow_attachment_by_id(aid)` (404 if missing or `message_id != mid`). `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)`. DB: `delete_workflow_attachments(aid, scope=scope, expected_message_id=mid)` (`backend/workflows/attachment_cache.py`). `LookupError` -> 404, `ValueError` -> 400. Response: `{"deleted_ids": [...], "group_empty": bool, "root_id": <post-op>, "active_sibling_id": int | None}`.

#### POST `/api/conversations/{cid}/workflow-attachments/access`

Handler `api_record_workflow_attachment_access`. Body: `{"ids": list[int]}`. No hook, no `_workflow_root_lock`. Validation: 404 on missing conversation; 400 if `ids` not a list; per-element drop on `isinstance(bool)` first, then `isinstance(int)` keep, else drop; empty filtered list short-circuits to `{"ok": True, "recorded": 0}`. JOIN `workflow_attachments` x `messages` filtered by `m.conversation_id = ?` -- silently drops ids not on this conversation. Survivors re-ordered to input order. Call `record_access(ordered_valid)` (`backend/workflows/attachment_cache.py`). Response: `{"ok": True, "recorded": n}`.

### 8.2 Helpers used by attachment routes

- `_workflow_root_lock(root_id)` -- backed by the `_workflow_root_locks` dict. Per-int-key asyncio lock.
- `_decode_stored_consumption_metadata(att)`. Parses `att["consumption_metadata"]` JSON; None on empty, malformed, or non-dict.
- `_split_reroll_gen_result(result, wid) -> (data, cm)`. Accepts `bytes` or `(bytes, dict | None)`; non-dict second element coerced to None with WARNING. Used by `/reroll-gen` and `/rehydrate`.
- `_build_reroll_gen_ctx(cid, mid, aid, att, settings, client) -> RerollGenCtx`.
- `_generated_seed() -> str`. `secrets.token_hex(16)` (32-char lowercase hex).

---

## 9. Attachment cache (`backend/workflows/attachment_cache.py`)

### 9.1 Schema

Migration `backend/database/migrations/0020_workflows.py` (sole migration touching this subsystem).

Table `workflow_attachments`:

| Column | Type | Constraint |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `message_id` | INTEGER | NOT NULL, FK `messages(id)` ON DELETE CASCADE |
| `mime_type` | TEXT | NOT NULL |
| `data_b64` | TEXT | NOT NULL (`EVICTED_MARKER` when evicted) |
| `filename` | TEXT | nullable |
| `created_at` | TEXT | NOT NULL |
| `workflow_id` | TEXT | NOT NULL |
| `parent_attachment_id` | INTEGER | FK `workflow_attachments(id)` ON DELETE CASCADE |
| `annotation` | TEXT | nullable |
| `seed` | TEXT | nullable |
| `generation_metadata` | TEXT | nullable (JSON) |
| `consumption_metadata` | TEXT | nullable (JSON) |
| `active_sibling_id` | INTEGER | FK `workflow_attachments(id)` ON DELETE SET NULL |
| `recent_accesses` | TEXT | nullable (JSON list of ints, max length 3) |

Added by 0020 (PRAGMA-guarded ADD COLUMN):

- `conversations.workflow_state` TEXT DEFAULT NULL
- `messages.workflow_state` TEXT DEFAULT NULL
- `character_cards.workflow_state` TEXT DEFAULT NULL
- `settings.workflow_config` TEXT NOT NULL DEFAULT `'{}'`
- `settings.attachment_cache_budget_bytes` INTEGER NOT NULL DEFAULT 524288000 (500 MiB)
- `settings.attachment_access_counter` INTEGER NOT NULL DEFAULT 0

No CREATE INDEX besides implicit PRIMARY KEY.

### 9.2 EVICTED_MARKER + budget

- `EVICTED_MARKER = "[evicted]"`. Replaces `data_b64` on eviction; all other columns preserved.
- Budget: `settings.attachment_cache_budget_bytes` (live read each call via `_get_budget_bytes_on`).
- "LRU-3": `recent_accesses` keeps at most 3 counter values (`new_list = ([assigned] + cur)[:3]`). Eviction sort key is the *oldest* of those values (`_lru3_key`); rows evict oldest-counter-first, so a single recent touch does not indefinitely pin an otherwise-idle row. Rows with empty/missing `recent_accesses` sort last (`+inf`) and are never first to evict.

### 9.3 Validation: `validate_workflow_attachment_shape(att) -> (bool, reason | None)`

Gates in order: dict, non-empty str `workflow_id`, str `filename`, str `mime`, XOR `data`/`path`, `data` bytes/bytearray non-empty, `path` str, `path` is regular file, `path` non-empty file, `path` stat-able. Returns `(True, None)` on pass. Used by `/regenerate` route to partition rejected entries before insert.

### 9.4 Public API

| Function | Sig | Transaction | Errors |
|---|---|---|---|
| `insert_workflow_attachment(message_id, attachment, *, mark_active=True)` | `(int | None, dict | None)` | BEGIN IMMEDIATE | `ValueError`, `LookupError`, `OSError` |
| `insert_workflow_attachments(message_id, attachments, *, db=None, mark_active=True)` | `(list[int], list[dict])` | BEGIN IMMEDIATE (or caller-owned) | same |
| `rehydrate_attachment(aid, data, *, consumption_metadata=None)` | `None` | BEGIN IMMEDIATE | `LookupError`, `RehydrateAlreadyDoneError`, `ValueError` |
| `record_access(ids: list[int])` | `None` | BEGIN IMMEDIATE | -- |
| `set_active_sibling(root_id, sibling_id | None, *, expected_message_id=None)` | `None` | BEGIN IMMEDIATE | `LookupError`, `ValueError` |
| `delete_workflow_attachments(target_id, *, scope, expected_message_id=None)` | `dict` | BEGIN IMMEDIATE | `LookupError`, `ValueError` |
| `get_workflow_attachment_by_id(aid)` | `dict | None` | (read-only, `database/queries/workflow_attachments.py`) | -- |
| `get_budget_bytes()` | `int` | (read-only) | -- |
| `evict(aid)` | `None` | BEGIN IMMEDIATE | -- |

Only `insert_workflow_attachment` is re-exported from `toolkit.py`. The others are called by the routes, the orchestrator, or the cache's own internal paths.

### 9.5 Insert flow

1. Reject if `not _is_produces_artifacts_workflow(workflow_id)` -> tagged `WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON`.
2. `_check_flat_parent_on` -- parent must exist, must have `parent_attachment_id IS NULL`, must be on same message.
3. Size via `_estimate_size`. If `size > budget` and rehydratable (`seed` non-empty str AND `generation_metadata` dict; `_is_rehydratable`), insert as marker; if not rehydratable, reject with `OVERSIZE_NO_METADATA_REASON`.
4. Otherwise evict existing rows via `_lru3_key`-sorted candidates until residual `(occupied + new_size) - budget <= 0`.
5. `insert_workflow_attachment_row` (`backend/database/queries/workflow_attachments.py`) issues `SELECT id FROM messages WHERE id=?` then `INSERT INTO workflow_attachments(...) VALUES(?,?,?,?,?,?,?,?,?,?,?)`.
6. Birth-as-access via `_record_access_inner`.
7. Optional `_set_active_sibling_on` when `mark_active=True`.

`insert_workflow_attachments` (batch) runs three partition stages: Step 0 routes non-`produces_artifacts` workflows to `rejected_atts` (same policy as the single-row path); Step A markers/rejects oversize new atts biggest-first (tie-break by input index), so markering one big att can spare many small existing rows; Step B then runs the same step-4 eviction over existing rows for any residual shortfall.

### 9.6 Sibling group

- Two-level: roots have `parent_attachment_id IS NULL`; siblings share `parent_attachment_id = root_id`.
- `_check_flat_parent_on` rejects siblings of siblings.
- `delete_workflow_attachments` scope `"group"`: deletes root + every sibling.
- Scope `"variant"` on a non-root: deletes the row only.
- Scope `"variant"` on a root with survivors: promotes the oldest-id survivor to root (`parent_attachment_id = NULL`), inherits the deleted root's annotation, and re-parents the remaining siblings onto the promoted row. `active_sibling_id` is then recomputed:
  - Kept if the old pointer named a row that survived -- a sibling, or the promoted row itself (in which case the new root points at itself).
  - Reset to NULL otherwise (renderer falls back to newest-wins), including the case where the old pointer named the now-deleted root.
- `active_sibling_id` legal values: any sibling in group OR root id OR NULL (newest-wins fallback). FK `ON DELETE SET NULL` clears it when the target is deleted.

### 9.7 `record_access`

- Bumps global `settings.attachment_access_counter` by `len(ids)`.
- Assigns counters to each id in input-list order (first id gets smallest, last gets largest).
- Per-row UPDATE: `recent_accesses = JSON([new_counter] + existing)[:3]`.
- Missing ids skipped, counter values still consumed.

### 9.8 Rejection reason constants

- `OVERSIZE_NO_METADATA_REASON = "too large to cache, no recovery metadata"`
- `WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON = "workflow does not declare produces_artifacts"`

Validator-emitted reasons come from each gate in `validate_workflow_attachment_shape`.

### 9.9 Exception-to-HTTP map

| Exception | Raised at | Caught at | HTTP |
|---|---|---|---|
| `RehydrateAlreadyDoneError` | rehydrate row no longer evicted | `main.py` | 409 |
| `LookupError` (set_active_sibling) | root/sibling missing / wrong message | `main.py` | 404 |
| `ValueError` (set_active_sibling) | not a root / sibling not in group | `main.py` | 400 |
| `LookupError` (delete) | target missing / wrong message | `main.py` | 404 |
| `ValueError` (delete) | bad scope | `main.py` | 400 |
| `(ValueError, LookupError, OSError)` (insert) | shape / parent / FS | `main.py`, `main.py` | 500 |

---

## 10. State tiers (summary)

| Tier | Storage | Key | Lock | Reached from / via |
|---|---|---|---|---|
| `turn_scratch` | in-memory dict | per turn | -- | PreCtx, PostCtx (same identity) |
| `workflow_state` | `conversations.workflow_state` JSON | (cid, wid) | `workflow_state_lock(cid, wid)` | PRE, POST, ON_DEMAND, REGENERATE, REROLL_GEN (any ctx carrying `conversation_id`); toolkit get/set |
| `workflow_character_state` | `character_cards.workflow_state` JSON | (character_id, wid) | `workflow_character_state_lock(character_id, wid)` (held inside `workflow_state_lock`) | PRE, POST, ON_DEMAND, REGENERATE (any ctx carrying `character_id`; not REROLL_GEN); toolkit get/set |
| `workflow_message_state` | `messages.workflow_state` JSON | (mid, wid) | `workflow_state_lock(cid, wid)` of the owning conversation; no message-specific lock | toolkit get/set; orchestrator persist-time apply of post-pipeline `set_message_state` |
| `workflow_config` | `settings.workflow_config[$.<wid>]` JSON | wid only (global) | `workflow_config_lock()` (single global) | toolkit get/set; HTTP PUT/GET |
| `workflow_attachments` | `workflow_attachments` table | mid-anchored; root-keyed | `_workflow_root_lock(root_id)` serializes the mutating routes on a root group | cache helpers; six attachment routes (five take the lock; the access route does not) |
| workflow enablement | `settings.workflows_globally_enabled` + `settings.workflow_enabled` JSON | global master + per `wid` | none (atomic per-key `json_set`) | `effective_workflow_enabled` (sec. 3.7); PRE/POST fan-out, hook-firing route gates, frontend `effectiveWorkflowEnabled` |

POST_PIPELINE hooks commit `workflow_message_state` for the in-flight assistant message by yielding `set_message_state`; the orchestrator writes the slot in `_persist_result` once the new `mid` exists (sec 7.4, 7.7). The toolkit `set_workflow_message_state` setter still only addresses already-persisted `mid`s, since the assistant `mid` is assigned during `_persist_result`, after the POST loop.

---

## 11. Frontend boot + state surface

### 11.1 Boot

Order:

1. `loadWorkflowManifest()` (`chat.js`) -- `await api.get("/workflows")` into `S.workflowManifest`.
2. `loadWorkflowModules()` (`workflow_loader.js`) -- for each manifest entry: `await import("/static/workflows/<id>/index.js")` (sequential, manifest order). 404s and module throws caught. If any module loaded, the loader re-runs `renderToolsPanel()` (`workflow_loader.js`): the Tools panel paints once before modules load, so freshly pushed cards would otherwise stay hidden behind the stale paint.

Both run inside `initAll` in `app.js`.

### 11.2 Module convention

A workflow's frontend code lives under `frontend/workflows/<id>/`. The framework dynamic-imports only `index.js` (served at `/static/workflows/<id>/index.js`). Multi-file workflows fan out through ordinary relative imports from `index.js`. CSS: inject a `<link href="/static/workflows/<id>/<file>.css">` from `index.js`, guarded by element id; framework does not load workflow CSS.

**A workflow imports from `/static/workflow_api.js` and nothing else in the app** (its own `./` files aside). That single module is THE plugin surface — the facade documented in sec. 11.5. Do not import `/static/chat.js`, `/static/state.js`, `/static/api.js`, `/static/utils.js`, `/static/modal.js`, `/static/audio_player.js`, or the `workflow_*.js` helpers directly: those deep imports are the deprecated-but-stable ABI v1 (kept working for external plugins only), and the in-repo layer check (`scripts/check_frontend_layers.py`) fails a `frontend/workflows/**` file that imports anything but `workflow_api.js` and relative paths.

Top-level `register*` calls run on import. Manifest order = module load order = registry push order.

### 11.3 `S.workflow*` slots (`state.js`)

| Slot | Initial | Write path | Read path (built-in) |
|---|---|---|---|
| `workflowInspectorCardRenderers` | `[]` | `registerWorkflowInspectorCard(wid, () => htmlString)` -> `[{workflowId, render}]` | `_buildSecondaryAgentsHtml` (`chat.js`); skips disabled (sec. 3.7) |
| `workflowToolsPanelRenderers` | `[]` | `registerWorkflowToolsPanelCard(wid, () => htmlString)` -> `[{workflowId, render}]` (card *body*, folded into the workflow's on/off card) | `buildWorkflowToggleRows` (`settings.js`); body shown only while enabled |
| `workflowMessageButtonRenderers` | `[]` | `registerWorkflowMessageButton(wid, (msg) => htmlString)` -> `[{workflowId, render}]` | `_renderExtraButtons` (`chat.js`); skips disabled |
| `workflowEventHandlers` | `{}` | `registerWorkflowEventHandler(wid, "my_event", (data, msgDiv) => ...)` -> `{[event]: {workflowId, handler}}` | `handleSSEEvent` default (`chat.js`); skips disabled |
| `workflowAttachmentRenderers` | `{}` | `registerAttachmentRenderer(wid, (ctx) => htmlString)` (facade; wraps the slot assign) | `_renderWorkflowSwipeContainer` (`chat.js`) |
| `workflowPipelines` | `[]` | via `registerWorkflowPipeline` only | SSE `reasoning` routing (`chat.js`); Inspector Secondary rail |
| `workflowState` | `{}` | `setWorkflowState(wid, <opaque>)` / `getWorkflowState(wid)` (facade) | author only (framework never reads) |
| `workflowPhases` | `{}` | via `setWorkflowPhase` / `clearWorkflowPhase` only | `_renderWorkflowPhasesPill` (`chat.js`) |
| `workflowTextEffects` | `[]` | via `registerTextEffect` only | segmentation gate (`chat.js`) |
| `workflowClickHandlers` | `[]` | via `registerClickHandler` only | segmentation gate (`chat.js`); click router (`workflow_text_interaction.js`) |
| `workflowManifest` | `[]` | (framework writes at boot) | `workflow_loader.js` (module-load loop); `chat.js` regen/reroll-button gates + label helpers |
| `reasoningByPass` | `{}` | (framework writes via SSE + `registerWorkflowPipeline` seed; reset per turn / conversation switch) | rail render |
| `inspectorTab` | `"main"` | via `setInspectorTab` only | tab paint |
| `toolsTab` | `"main"` | via `setToolsTab` only | tab paint |
| `rejectedWorkflowAtts` | `[]` | (framework writes via `_mergeWorkflowRejections`; per-tuple replace, empty incoming clears) | rejection chip render |

An author may read its own entry from `S.workflowManifest` (matched by `id`) for `display_name`, `config_schema`, or `config_defaults` (`main.py`). Config *values* are not in the manifest -- read or write the live config slot via `GET` / `PUT /workflows/<id>/config`.

### 11.4 Exported registrars (via the facade)

Plugins import these from `/static/workflow_api.js`. Their implementations live in `workflow_registry.js`; `state.js` re-exports them so the ABI v1 deep-import path stays valid for external plugins.

```
registerWorkflowPipeline({id, label?, passes:[{id, label?}]})
registerTextEffect({id, label?})
registerClickHandler({id, label?, priority?, claims?, onClick})
```

`registerWorkflowPipeline` validation:

- `id` non-empty string.
- Each `p.id` is a string.
- `p.id NOT in {"director","writer","editor"}` (reserved).
- `p.id` must start with `id + ":"`.
- `p.id` must not contain a second `:` after that prefix.
- Failures: `console.error` and abort the registration (no throw). Any invalid pass drops the whole pipeline -- nothing is seeded or pushed.
- On success: seeds `S.reasoningByPass[p.id] = ""` for passes not already present.

`registerTextEffect`:

- `id` non-empty string (else `console.error` and skip).
- `label` -> `id`.
- Registering any effect enables body word-segmentation -- without a registered effect or click handler, `.seg` spans are never produced (`chat.js`).

`registerClickHandler` (validation + defaults):

- `label` -> `id`.
- `priority` -> `0` (integers only).
- `claims` -> `() => true` (claims all).
- `onClick` required (function).

All three registrars are idempotent on `id` (replace in place).

Production-surface registrars -- the four enablement-gated slots from sec. 11.3. Prefer these over pushing/assigning raw functions: each stamps the entry with `workflowId` so the framework can gate it (sec. 3.7). The read sites now destructure `{workflowId, render}` / `{workflowId, handler}`, so a bare function pushed directly carries no `workflowId` to gate on and no `render`/`handler` for the reader to call.

```
registerWorkflowInspectorCard(workflowId, render)        # render: () => htmlString
registerWorkflowToolsPanelCard(workflowId, render)       # render: () => htmlString (card body)
registerWorkflowMessageButton(workflowId, render)        # render: (msg) => htmlString
registerWorkflowEventHandler(workflowId, event, handler) # handler: (data, msgDiv|null) => void
```

The three array registrars are idempotent on `workflowId` (re-registration replaces in place); `registerWorkflowEventHandler` is keyed by `event` (one handler per event name, last writer wins). Bad args `console.error` and skip. The consumption renderer is the deliberate exception -- register it with `registerAttachmentRenderer(wid, (ctx) => htmlString)` (facade wrapper for the slot assign); it is never gated, so a disabled workflow's existing artifacts still render.

`effectiveWorkflowEnabled(wid)` (facade) -- the frontend mirror of the backend truth table (sec. 3.7), read off `S.settings`. Safe before settings load (defaults to enabled); a malformed map degrades to enabled.

### 11.5 The plugin facade (`workflow_api.js`) — ABI v2

`frontend/workflow_api.js` is **THE plugin surface**. Everything a workflow is allowed to touch is re-exported (or wrapped) here, so a plugin never reaches into `state.js` / `chat.js` / `audio_player.js` / etc. directly.

**Stability policy — additive only.** New exports may be added; an existing export **never changes name or signature**. That single rule is the extensibility contract. `WORKFLOW_API_VERSION` (currently `1`) bumps only when surface is added (still additive). The stage-0 ABI snapshot check (`scripts/check_frontend_layers.py`) diffs this file's exports against a frozen list, so an accidental rename/removal fails CI. Canonical names throughout — no aliases (`setWorkflowPhase` is `setWorkflowPhase`, one name per operation).

**ABI reference.** Tier `frozen` = payload/signature is contract; `stable` = additive-only like the rest.

| Export | Signature | Semantics | Tier |
|---|---|---|---|
| `WORKFLOW_API_VERSION` | `number` | Facade version (additive bumps). | frozen |
| `registerWorkflowPipeline` | `({id, label?, passes})` | Register a reasoning pipeline (Inspector rail + `reasoning` routing). | stable |
| `registerTextEffect` | `({id, label?})` | Register a text-effect driver; enables `.seg` segmentation. | stable |
| `registerClickHandler` | `({id, label?, priority?, claims?, onClick})` | Claim clickable word units. | stable |
| `registerWorkflowInspectorCard` | `(wid, () => html)` | Inspector Secondary card (gated). | stable |
| `registerWorkflowToolsPanelCard` | `(wid, () => html)` | Tools-panel card body (gated). | stable |
| `registerWorkflowMessageButton` | `(wid, (msg) => html)` | Per-message toolbar button (gated). | stable |
| `registerWorkflowEventHandler` | `(wid, event, (data, msgDiv) => void)` | Custom SSE event handler (gated). | stable |
| `registerAttachmentRenderer` | `(wid, (ctx) => html)` | Attachment widget renderer (ungated by design). | stable |
| `registerAction` | `(wid, name, (el, event) => void)` | Handler for a `data-wf-action="wid:name"` element (see below). | stable |
| `api` | `{get,post,put,del,upload}` | HTTP helper. | stable |
| `convUrl` | `(...parts) => string` | Build `/conversations/...` paths. | stable |
| `esc` / `escAttr` | `(s) => string` | HTML / attribute escaping. | stable |
| `toast` | `(msg, isError?)` | Transient notification. | stable |
| `showModal` / `closeModal` | `(html)` / `()` | Framework modal. | stable |
| `playAudio` | `({channel, segments, loop?, volume?, stopOn?})` | Play on a shared audio channel. | stable |
| `stopChannel`/`stopAll`/`pauseChannel`/`resumeChannel`/`seekChannel`/`setChannelVolume`/`setChannelRepeat`/`replayChannel` | channel controls | See sec. 15.3. | stable |
| `channelState` | `(channel) => state\|null` | Live channel state (sec. 15.4). | stable |
| `onChannel` | `(channel, (ev) => void)` | Subscribe to channel events (sec. 15.5). | stable |
| `messageSegments` | `(msgId) => [{wordIndex, sentIndex, word}]` | Rendered word units (sec. 16.3). | stable |
| `startTextEffect` / `clearTextEffect` | `({msgId, effectId, grain?, variant?})` / `()` | Drive/stop a text effect (sec. 16.5). | stable |
| `setWorkflowPhase` / `clearWorkflowPhase` | `(channel, label)` / `(channel?)` | Status pill (sec. 13.1). | stable |
| `refreshConversationMessages` | `(msgId?) => Promise` | Refetch + repaint messages (sec. 13.4). | stable |
| `selectWorkflowPipelinePass` | `(pipelineId, passId)` | Select a pipeline pass in the rail. | stable |
| `broadcastWorkflowMutation` | `({convId, msgId})` | Announce a cross-tab artifact mutation (sec. 18). | stable |
| `effectiveWorkflowEnabled` | `(wid) => bool` | Frontend enablement mirror (sec. 3.7). | stable |
| `subscribe` | `(topic, (detail) => void) => off` | Subscribe to a **public** state topic: `messages`, `conversations`, `settings`, `workflow-phase` (payload shapes frozen). Plugins are subscribe-only. | frozen |
| `requestRepaint` | `()` | rAF-debounced `renderMessages`; **no-ops while streaming**. | stable |
| `getActiveConvId` | `() => id\|null` | Active conversation id. | stable |
| `getMessages` | `() => msg[]` | Live messages array (read-only). | stable |
| `getManifestEntry` | `(wid) => entry\|null` | This workflow's `/api/workflows` entry. | stable |
| `canMutate` | `() => bool` | Whether this tab may perform mutating actions (multi-tab gate). | stable |
| `getWorkflowState` / `setWorkflowState` | `(wid)` / `(wid, v)` | Opaque per-workflow UI-state slot. | stable |

**Wiring buttons — `registerAction` + `data-wf-action`.** A plugin never uses a `window.*` global or an inline `on*` attribute. Put `data-wf-action="<wid>:<name>"` on the element (add `data-wf-on="change"` for an `<input>`/`<select>` that fires on change instead of click), stash any parameters in `data-*`, and register the handler with `registerAction(wid, name, (el, event) => …)`. One framework-owned delegated listener on `document` resolves the attribute and calls your handler with the element that carries it. (This is the plugin-sized slice of the core `data-action` dispatcher stage 5 introduces; both use the same attribute convention.)

**Skeleton `index.js`:**

```js
import { registerWorkflowToolsPanelCard, registerAction, toast } from "/static/workflow_api.js";

const WID = "my_workflow";

registerWorkflowToolsPanelCard(
  WID,
  () => `<div class="tool-card-desc">What this workflow does.</div>
         <button data-wf-action="${WID}:hello">Say hi</button>`,
);
registerAction(WID, "hello", () => toast("hi from my_workflow"));
```

**Scope.** Backend registration still edits `backend/workflows/__init__.py` by design (the frontend side is already zero-core-edit via the manifest loader) — that is outside this refactor.

---

## 12. SSE dispatch (`frontend/chat.js`)

### 12.1 `processSSEStream`

Frames `event: <name>` / `data: <json>` pairs from a `fetch` body stream. Per pair, calls `handleSSEEvent(event, data, container, msgDiv, onToken, onRewrite)`. Clears `S.pendingRefineDiff` and resets reasoning state at entry. Reading aborted via signal throws an `AbortError`.

### 12.2 `handleSSEEvent`

Built-in cases:

| event | Effect |
|---|---|
| `director_start` | phase=directing; clear inspected; `renderInspector` |
| `director_done` | set `S.lastDirectorData`; advance reasoning pass; `renderInspector` |
| `token` | phase=generating; appends the token to the response buffer, mirrors it into `S.streamingContent`, and repaints |
| `writer_rewrite` | phase=refining; build sentence diff; `onRewrite(refined_text)` |
| `reasoning` | route by `data.pass`: a built-in pass (`director`/`writer`/`editor`) appends to `S.reasoningDirector`/`Writer`/`Editor`; otherwise match against a registered pipeline's pass ids in `S.workflowPipelines` and append to `S.reasoningByPass[pass]` |
| `phase_status` | requires `data.channel` to start `"workflow:"`; calls `clearWorkflowPhase(channel)` when `state === "done"` or the label is missing/blank, else `setWorkflowPhase(channel, label)` |
| `editor_done` | append `tool_calls` to `S.lastDirectorData` |
| `user_message_created` | patch pending user row id; optional in-flight edit POST |
| `error` | toast |
| `workflow_attachments_rejected` | `_mergeWorkflowRejections(msgId, null, rejected)`; no re-render |

Default branch: looks up `S.workflowEventHandlers[event]` (a `{workflowId, handler}` record). If its `workflowId` is effectively enabled (sec. 3.7) and `handler` is a function, parses `data` with `JSON.parse`, falling back to the raw string on parse failure, then invokes `handler(payload, msgDiv)` -- `payload` is the parsed JSON or raw string, `msgDiv` is the streaming message element or `null`. The call is wrapped in `try/catch`; throws are logged via `console.error` and do not abort the stream.

No `done` case, so `done` falls through to the default branch and reaches `S.workflowEventHandlers["done"]` if a handler is registered.

### 12.3 Reserved event names (do not author-emit as custom)

These 10 names are intercepted by built-in `case`s in `handleSSEEvent` before the custom-handler default branch, so registering a handler for them has no effect: `token`, `director_start`, `director_done`, `writer_rewrite`, `reasoning`, `phase_status`, `editor_done`, `user_message_created`, `workflow_attachments_rejected`, `error`. Separately, event names a workflow's pipeline hooks emit are filtered server-side: the pipeline drops any underscore-prefixed name from `post_pipeline` and `pre_pipeline` output (both hook loops in `workflow_bridge.py`), since the `_`-prefix is reserved for internal persistence signals (`_result`, `_refined_result`, `_editor_reasoning`). These never reach the frontend.

### 12.4 `afterStream`

Awaited unconditionally at end of `runStreamRequest` and `sendMessage`. Refetches `/conversations/<id>/messages`, refreshes director state, finalizes streaming DOM, clears workflow phases as backstop (`clearWorkflowPhase()` no arg).

---

## 13. Phase pill + reasoning rail + tabs + helpers

### 13.1 Phase pill

```
setWorkflowPhase(channel, label)    # chat.js
clearWorkflowPhase(channel?)        # chat.js
```

`channel` convention: `"workflow:<id>"` (the SSE handler enforces this prefix for inbound). For multiple concurrent same-workflow ops, suffix it (e.g. `"workflow:tts:regen:<rootId>"`) so they don't clobber each other.

- `setWorkflowPhase`: blank/whitespace `label` -> delete entry; otherwise set.
- `clearWorkflowPhase()` no arg wipes the whole map.
- `_renderWorkflowPhasesPill` -- the most recently *added* channel wins the single visible slot. Re-setting an existing channel updates its label in place without reordering, so it is not promoted to newest.
- Backstop: `afterStream` calls `clearWorkflowPhase()` -- pair every `setWorkflowPhase` with a `clearWorkflowPhase` in a `finally`, but stream-end is forgiving.

### 13.2 Reasoning rail

`registerWorkflowPipeline({id, label?, passes:[{id, label?}, ...]})` declares a Secondary-tab rail. Each pass `id` must start with `<wid>:`, contain no second colon, and not be a reserved built-in (`director`/`writer`/`editor`); `registerWorkflowPipeline` (`state.js`) rejects the whole pipeline if any pass violates this. The check accepts an empty trailing segment (`"tts:"`), so name the pass segment non-empty by convention.

The router (`chat.js`) finds the pipeline whose `passes` contains `data.pass`, then:

- Matched pass: the delta accumulates in `S.reasoningByPass[passKey]` regardless of which tab is open.
- Live paint happens only when the Inspector Secondary tab is open (`S.inspectorTab === "secondary"`) AND this pass is the one selected in the rail -- the box `#reasoning-box-<pipelineId>` carries the selected pass as `data-pass-id`, and the router paints only on a match.
- Otherwise the text accumulates silently; `renderInspectorSecondary` paints it the next time the tab opens or the pass is selected.

A pass id that matches neither a built-in nor any registered pipeline is dropped with a `console.warn` (`chat.js`).

Emit reasoning from a workflow hook via `forced_tool_call(..., pass_id="<wid>:<pass>")` or yield `{"event": "reasoning", "data": {"pass": "<wid>:<pass>", "delta": "..."}}` directly. Both yield the same event; the orchestrator forwards it to SSE, where the router consumes it.

`selectWorkflowPipelinePass(pipelineId, passId)` (`chat.js`) -- programmatic pass selection; rebuilds the Inspector Secondary content even if that tab is hidden.

### 13.3 Tabs

```
setInspectorTab("main" | "secondary")    # chat.js
setToolsTab("main" | "secondary")        # chat.js
```

Switching to Inspector Secondary triggers `renderInspectorSecondary` (rebuild). Switching to Tools Secondary only toggles visibility.

### 13.4 Refetch helpers

```
refreshConversationMessages(msgId?)   # chat.js async, may return false (in-flight gates)
renderMessages()                       # chat.js no-arg local repaint
broadcastWorkflowMutation({convId, msgId})   # tabLock.js peer-tab refresh
```

`refreshConversationMessages` returns `false` when there is no active conversation (`S.activeConvId`), while streaming (`S.isStreaming`), while editing (`editingMsgId` / `editingPendingUserMsg` / `magicInputMsgId`), or when `msgId` is one a rehydrate/action/swipe is mid-flight on. `renderMessages` repaints from current `S.messages` (no fetch) -- use after a local config change that affects how renderers paint.

### 13.5 HTTP / DOM helpers

All imported from `/static/workflow_api.js` (not the deep modules named below — those are the ABI v1 originals):

```
api.get(path)                # api.js prepends /api (via _req)
api.post(path, body)         # JSON body
api.put(path, body)          # JSON body
convUrl(...parts)            # utils.js -> "/conversations/<part1>/<part2>/..."
esc(s) / escAttr(s)          # utils.js HTML- / attribute-escape; null/undefined -> ""
toast(msg, isError?)         # utils.js transient notification
showModal(html) / closeModal()   # modal.js
```

Paths passed to `api.*` must NOT include `/api` -- `_req` adds it. A conversation-scoped call: `api.post(convUrl(cid, "foo"), body)`, equivalently `api.post("/conversations/" + cid + "/foo", body)`; both hit `/api/conversations/<cid>/foo`.

### 13.6 Author-callable HTTP routes

- `POST /api/conversations/<cid>/workflows/<wid>/trigger` -- ON_DEMAND. Body + response are author-defined.
- `GET /api/workflows/<wid>/config` -- live effective config.
- `PUT /api/workflows/<wid>/config` body `{config: {...}}` -- full replacement; `{config: {}}` resets to defaults.

No first-party JS wrapper for any of these; call `api.*` directly with the path minus the `/api` prefix. The config routes are not conversation-scoped, so build them by hand; the trigger route is, so `convUrl` applies. E.g. `api.get("/workflows/" + wid + "/config")`, `api.put("/workflows/" + wid + "/config", {config})`, `api.post(convUrl(cid, "workflows", wid, "trigger"), body)`.

---

## 14. Attachment widget rendering

### 14.1 Group iteration

`_renderWorkflowArtifacts(msg)` (`chat.js`) buckets attachments via `_workflowAttachmentGroups(msg)` by `parent_attachment_id` (parent missing -> root), then wraps the groups in `<div class="workflow-artifacts">`. Groups sorted by `rootId`; siblings sorted by id.

Per group, `_renderWorkflowSwipeContainer(msg, rootId, atts)` decides branch:

| Branch | Condition | Behavior |
|---|---|---|
| Minimized | `_workflowMinimized.has(rootId)` | Header only; no body; author renderer NOT invoked. |
| Evicted | `_isAttachmentEvicted(active)` -- `(att.b64 || att.data_b64 || "")` equals the `"[evicted]"` sentinel | `_evictedAttachmentHtml(...)` + `actionButtons`. |
| Renderer | `S.workflowAttachmentRenderers[active.workflow_id]` is a function | `renderer(ctx)`. |
| Default | otherwise | `defaultHtml`. |

Active sibling selection: `_activeIndexForGroup` (wrapping `_activeAttachmentForGroup`) -- `root.active_sibling_id` if it matches a sibling, else newest.

### 14.2 Renderer `ctx`

A registered renderer (`S.workflowAttachmentRenderers[workflow_id]`) receives one argument:

```
{
  att: <attachment row>,                              // consumption_metadata already JSON-parsed at load (chat.js); null if malformed
  buttons: {regen: <html>, reroll: <html>},            // pre-built button strings (already inside defaultHtml)
  defaultHtml: <full default rendering, media + buttons>
}
```

Choose exactly one layout strategy, never both -- they share the same button strings, so combining them paints the regen/reroll strip twice:
- Splice `defaultHtml` whole (custom chrome around the stock widget), or
- Build custom markup and splice `buttons.regen` / `buttons.reroll` where you want them.

A renderer that throws falls back to `defaultHtml` (the throw is logged to the console); a renderer that returns a falsy value yields an empty widget body.

### 14.3 Default widget (`frontend/default_widget.js`)

| MIME prefix | HTML |
|---|---|
| `image/` | `<img src="data:...;base64,...">` |
| `audio/` | `<audio controls src="...">` |
| `video/` | `<video controls src="...">` |
| else | `<a download="<filename>" href="data:...">...</a>` |

Source aliases: `att.b64 || att.data_b64`, `att.mime || att.mime_type` (fallback `application/octet-stream`), `att.filename || att.workflow_id || "artifact"`.

### 14.4 Chrome (framework-owned; renderer body wrapped in `.workflow-widget`)

- Header `.workflow-artifact-header` -- `.workflow-artifact-label` (the manifest entry's `display_name`, falling back to the raw `workflow_id` then `"artifact"`), Minimize `.workflow-min-btn`, Delete `.workflow-del-btn`.
- Body `.workflow-artifact-body` -- contains renderer output inside `<div class="workflow-widget" data-workflow-id="<wid>" data-attachment-id="<aid>">`.
- Nav `.workflow-artifact-nav` -- `.workflow-swipe-btn` arrows. No cycle: each arrow is disabled at its end of the list, and both are disabled when the group has one sibling or other tabs are open (`S.hasMultipleTabs`).
- Counter `.workflow-artifact-counter` -- `idx+1 / total` when `total > 1`.
- `instanceId` = `ws-<msgId>-<rootId>`; carried on `data-msg-id` / `data-root-id`.

### 14.5 Inspector + Tools cards

Inspector Secondary card iteration: `_buildSecondaryAgentsHtml` (`chat.js`). Each enabled entry's `render()` output is concatenated raw (no per-card wrap); entries whose `workflowId` is disabled are skipped (sec. 3.7).

Tools Secondary card iteration: `buildWorkflowToggleRows` (`settings.js`, called by `renderToolsPanel`). Unlike the inspector cards, a tools-panel renderer does **not** return a standalone card -- the framework renders one card per manifest workflow (name + on/off toggle), and the matching `workflowToolsPanelRenderers` entry's `render()` supplies that card's *body* (description + any controls), folded in only while the workflow is effectively enabled. So a shipped workflow is a single entry, not a separate toggle and settings card.

Per-message buttons: `_renderExtraButtons(msg)` (`chat.js`). Each enabled entry's `render(msg)` is spliced into the toolbar between magic and delete buttons.

### 14.6 `window.workflow*` handlers (`chat.js`)

Owned by the framework; bound onto the buttons the chrome, nav arrows, and widget bodies emit. The POST-driven handlers hit the per-attachment route family `/conversations/<cid>/messages/<mid>/workflow-attachments/<attId>/<op>` (sec. 8); the table names only the `<op>` segment:

| Handler | Behavior |
|---|---|
| `workflowRegenerate(msgId, attId, btn)` | tab-lock gate, per-root in-flight lock, set pill, POST `.../regenerate`, merge rejections, refetch + render |
| `workflowReroll(msgId, attId, btn)` | same shape, POST `.../reroll-gen` |
| `workflowRehydrate(msgId, attId, btn)` | tab-lock gate, per-attId in-flight, POST `.../rehydrate`, refetch + render; 409 treated as already-restored |
| `workflowArtifactStep(instanceId, delta)` | sibling nav; optimistic `root.active_sibling_id` update + DOM swap + POST `.../activate` |
| `workflowToggleMinimize(instanceId)` | toggles `_workflowMinimized` Set + `localStorage["orb.workflowMinimized"]`; no server |
| `workflowDeleteAttachment(instanceId)` | opens the delete-choice modal, then `workflowConfirmDelete(scope)` on confirm. The variant-vs-whole-group choice appears only for a group with >1 sibling; a single-variant group gets a plain confirm |
| `workflowConfirmDelete(scope)` | confirm dispatcher |

LocalStorage key: `WF_MINIMIZED_LS_KEY = "orb.workflowMinimized"`. Persisted: a collapsed widget stays collapsed across reloads and is shared across same-origin tabs; the in-memory Set is rebuilt per load.

### 14.7 Rejection chips

`_mergeWorkflowRejections(msgId, originatingId, incoming)`: drop-then-append by `(msgId, originatingId)` tuple. Empty `incoming` clears that tuple's entries.

| Surface | Trigger | originatingId |
|---|---|---|
| Per-widget chip (filtered + placed in `_renderWorkflowSwipeContainer`) | regenerate/reroll response | `root_id` |
| Footer chip (`_renderWorkflowRejection`) | SSE `workflow_attachments_rejected` | `null` |

Both surfaces emit their HTML through the shared `_workflowRejectionChipHtml`, which renders `<div class="workflow-rejected-warning">...</div>`.

### 14.8 Access reporting client

- IntersectionObserver `_workflowViewportObserver` (`chat.js`, re-attached per render by `_refreshWorkflowViewportObserver`). Threshold `0.1`. On first entry of a message (deduped per session via `_workflowObservedMsgIds`, declared): queues one active-sibling id per group into `_workflowViewportPendingIds`.
- Swipe success also queues the new active sibling id.
- Debounce `_scheduleWorkflowViewportFlush`: 250ms `setTimeout` -> `_flushWorkflowViewportReport` POSTs `{ids: [...]}` to `/conversations/<cid>/workflow-attachments/access`.
- IDs are sent in Set insertion order (`[..._workflowViewportPendingIds]`); the backend assigns access counters in that order (sec. 9).
- Conversation switch resets the observed-message set, pending set, and timer (`chat.js`).

### 14.9 Evicted card

`_evictedAttachmentHtml(msg, att)` (`chat.js`) renders filename label + Rehydrate button (or "Bytes evicted" disabled span if `att.seed` is missing). Onclick targets `window.workflowRehydrate(msg.id, att.id, this)`. Multi-tab gating disables the button.

---

## 15. Audio system (`frontend/audio_player.js`, `audio_schedule.js`, `audio_transport.js`)

### 15.1 `playAudio({channel, segments, loop?, volume?, stopOn?})` (`audio_player.js`)

Returns `{channel, stop(), isActive()}`. Channels mix; replaying a channel replaces only that channel (last-write-wins per channel, enforced by monotonic token).

| Field | Rule |
|---|---|
| `channel` | required non-empty string; bad/missing -> no-op stub session |
| `segments` | array of segments (see below); each `normalizeSegment` malformed entry skipped with WARNING |
| `loop` | default `false`; runtime override via `setChannelRepeat` |
| `volume` | clamped to `[0, 1]` (non-finite -> 1); sticky per channel |
| `stopOn` | `{newTurn?, convSwitch?}` stored on the channel; omitted keys default to `true` at turn/conv teardown |

### 15.2 Segment shapes (`audio_schedule.js`)

Exactly one of `row` / `b64` / `silence` per entry:

| Field | Meaning |
|---|---|
| `seg.row` | attachment row id; bytes read live from `S.messages` via `_findAttachment` (`audio_player.js`); evicted rows skipped (no auto-rehydrate) |
| `seg.b64` | inline base64; optional `seg.mime` (carried through, NOT used by decoder -- Web Audio sniffs format) |
| `seg.silence` | seconds; `<=0` or non-finite drops; `>600` clamps to 600 |
| `seg.start` | default 0; negative drops |
| `seg.end` | default = clip end (null sentinel) |

### 15.3 Per-channel controls

```
stopChannel(channel, reason="skipped")
stopAll()
pauseChannel(channel)
resumeChannel(channel)
seekChannel(channel, offsetSec)
setChannelVolume(channel, vol)
setChannelRepeat(channel, on)
replayChannel(channel)
channelState(channel)                      # null if never played / hard-stopped
onChannel(channel, handler)                # returns unsubscribe
```

A naturally-ended channel keeps its plan; `replayChannel`, `seekChannel`, and `setChannelRepeat(on=true)` can re-arm without re-calling `playAudio`.

### 15.4 `channelState` shape

```
{
  playing, paused, loop,
  segmentCount, segmentIndex,        // 0-based; >= 0 whenever channelState is non-null
  stream:  {elapsedSec, remainingSec, durationSec},
  segment: {elapsedSec, remainingSec, durationSec},
}
```

Drive a karaoke effect off `segmentIndex` plus the per-clip `segment` grain (`segment.elapsedSec / segment.durationSec`): `segmentIndex` selects the current clip, the grain places the cursor within it. A silent gap counts as a segment, so both advance through gaps. `stream.elapsedSec` is the whole-stream cursor -- use it for overall progress, not per-clip word timing.

### 15.5 `onChannel` events

| type | Extra fields | Fires when |
|---|---|---|
| `play` | `reason: "start" \| "resume" \| "repeat"` | `start`: first play, `replayChannel`, or a seek that re-arms a naturally-ended channel. `resume`: `resumeChannel`. `repeat`: `setChannelRepeat(on=true)` re-arming a naturally-ended channel |
| `pause` | -- | `pauseChannel` |
| `close` | `reason: "ended" \| "skipped" \| "lifecycle" \| "superseded"` | `ended`: the clip finishes -- on its own, by seeking to the very end, or by resuming past it. `skipped`: `stopChannel` / bar Stop. `lifecycle`: framework teardown on a new turn or conversation switch (`onTurnStart` / `onConvSwitch`), or a blanket `stopAll`. `superseded`: replaced by a newer `playAudio` or `replayChannel` |
| `seek` | `fromSec`, `toSec` | live or paused seek |

Exactly one `close` per audible life. Loop laps don't re-emit `play`. A plan superseded while still decoding emits neither `play` nor `close`.

### 15.6 Transport bar (`audio_transport.js`)

Mounted above the composer inside `#chat-input-area` at boot (`initAudioPlayer`); the engine repaints it on every state change. Channel-selector tabs (one per active channel) plus a single control row bound to the selected channel: play/pause/replay button, repeat toggle, a draggable/clickable progress scrubber (seeks via `seekChannel`), time readout, volume slider, and stop. A dismiss button hides the bar without stopping audio; a floating button reopens it.

---

## 16. Text effects, segmentation, click handlers

### 16.1 When `.seg` spans exist

The chat render path wraps words in `.seg` spans (`segmentBody`, `workflow_segmentation.js`) and tags the claimed ones (`markClickable`) via `_applyWorkflowTextSegments` (`chat.js`). Both entry points require the same two things: at least one of `S.workflowTextEffects` / `S.workflowClickHandlers` is non-empty, and the body is not in editor-diff review. The two entry points:

- After streaming completes, in place on the new message: `finalizeStreamingDiv` (`chat.js`).
- Full re-render: `_segmentRenderedMessages` (`chat.js`).

Only finalized messages with a positive-integer `data-msg-id` are segmented (`chat.js`); pending and streaming rows lack one until finalized.

### 16.2 Segmentation produces

Each `.seg` span:

- `class="seg"`
- `data-seg="<wordIndex>"`
- `data-sent="<sentIndex>"`

Words split across inline markup share the same `data-seg` (coalesced at read time).

### 16.3 `messageSegments(msgId)` (`workflow_segmentation.js`)

Returns ordered `[{wordIndex, sentIndex, word}]`. `word` text coalesces multiple `.seg` fragments sharing the same `data-seg`. Empty array when the message body isn't in DOM yet.

### 16.4 `segDescriptor` (`workflow_segmentation.js`)

Passed to `claims(seg)` and `onClick(seg, msgId)`:

| Field | Source |
|---|---|
| `wordIndex` | `Number(span.dataset.seg)` |
| `sentIndex` | `Number(span.dataset.sent)` |
| `word` | lazy getter; concatenates `textContent` of all spans sharing `data-seg` |
| `sentenceText` | lazy getter; concatenates spans sharing `data-sent` |
| `msgId` | merged in via `extra`; the click router reads it from the closest `.message[data-msg-id]` (`workflow_text_interaction.js`), the render-time claim pass `markClickable` passes the message id it already holds |
| `role` | merged in via `extra`; `"user"`/`"assistant"` (`workflow_text_interaction.js`) |

### 16.5 `startTextEffect({msgId, effectId, grain?, variant?})` (`workflow_text_effects.js`)

Returns `{markActive(unitIndex), stop()}` -- hold this handle and drive `markActive` from your own events (e.g. audio time updates). Global single session: starting a new one supersedes the prior, after which the old handle's `markActive` no-ops via an internal token check.

| Param | Default | Allowed |
|---|---|---|
| `grain` | `"word"` | `"word"`, `"sentence"` |
| `variant` | `"highlight"` | `"highlight"`, `"underline"`, `"pulse"` (unknown -> highlight + `console.error`) |

Painter applies CSS class `"fx-" + variant` to `.seg[data-seg=<idx>]` (word grain) or `.seg[data-sent=<idx>]` (sentence grain).

`clearTextEffect()` -- tears down the global session.

### 16.6 `registerClickHandler({id, label?, priority?, claims?, onClick})` (facade)

`priority` (default 0) breaks contention when several workflows claim one word -- higher wins, registration order on ties. The sort happens at click time in `_claimantsFor` (`workflow_text_interaction.js`), not at registration. `claims(seg)` decides which words the handler wants (default: all). `onClick(seg, msgId)` runs on click.

### 16.7 Click router (`workflow_text_interaction.js`)

Delegated `click` listener on `#chat-messages`. Steps:

1. Resolve target `.seg.seg-clickable`.
2. Build `segDescriptor`.
3. `_claimantsFor(ctx)` runs each `S.workflowClickHandlers[*].claims(ctx)` (throwing claims logged + skipped), sorts by priority descending.
4. Fire:
   - One claimant: a plain click fires its `onClick`.
   - Multiple claimants (`.seg-multi`): a plain click fires the top-priority claimant. To pick another, the user opens a chooser listing every claimant in priority order:
     - Desktop: a caret revealed on hover, clicked to open the chooser.
     - Touch: a long-press, which swallows the synthetic click so the top claimant does not also fire.

### 16.8 CSS classes

| Class | Source |
|---|---|
| `.seg` | `workflow_segmentation.js` (structural marker; styled only via `.seg.<modifier>` compounds) |
| `.seg-clickable` | `workflow_text_interaction.js` (added to any claimed word) |
| `.seg-multi` | `workflow_text_interaction.js` (added to words with >1 claimant) |
| `.fx-highlight` / `.fx-underline` / `.fx-pulse` | `workflow_text_effects.js` toggle |
| `.wf-seg-caret` | `workflow_text_interaction.js` (hover chooser button) |
| `.wf-claim-popover` / `.wf-claim-item` | `workflow_text_interaction.js` |

CSS for all these lives in `frontend/style.css`. Author addresses units by index; framework owns DOM and classes.

---

## 17. Authoring checklist

To ship a new workflow:

### 17.1 Backend

1. Create `backend/workflows/<id>/` with at minimum `__init__.py` and `hooks.py`.
2. In the workflow module's `__init__.py`, build a `Workflow(...)` instance with `id`, `display_name`, optional `tools` (list of `ToolSpec`; sec. 3.2), optional `config_schema` / `config_defaults`, and `produces_artifacts` if you persist attachments.
3. Implement hook callables in `hooks.py` matching the signatures in sec. 4.6. Use `backend.workflows.toolkit` for all internal access.
4. Wire registration in `backend/workflows/__init__.py` (NOT the workflow's own subdir): import each hook callable from `<id>/hooks.py` (alias them, e.g. `as _myflow_post`, so module-level names from different workflows do not collide -- see sec. 3.4), then call `register_workflow(my_workflow)` + one `subscribe(my_workflow.id, HookType.X, fn)` per hook. Keep the `finalize_registry()` call at the bottom of the file -- it is a no-op for non-producers but fails import for a `produces_artifacts=True` workflow missing `REGENERATE`/`REROLL_GEN`.
5. State stores: hold the matching lock for read-modify-write (locks recap, 17.6).

### 17.2 Frontend

**Import everything from `/static/workflow_api.js`** — never `/static/chat.js`, `/static/state.js`, or the other deep modules (those are the deprecated ABI v1; the layer check rejects them for in-repo plugins). See the skeleton in sec. 11.5.

1. Create `frontend/workflows/<id>/index.js`. Top-level imports and `register*` calls run on import.
2. Register renderers via `registerWorkflowInspectorCard("<id>", ...)` / `registerWorkflowToolsPanelCard("<id>", ...)` / `registerWorkflowMessageButton("<id>", ...)` as needed (sec. 11.4). These carry your workflow id so the framework hides them while your workflow is disabled (sec. 3.7); the read sites expect the `{workflowId, render}` shape, so a bare function pushed directly will not render.
3. Register your attachment renderer with `registerAttachmentRenderer("<id>", (ctx) => html)` if you produce artifacts (this consumption surface is intentionally never gated).
4. Register custom SSE handlers via `registerWorkflowEventHandler("<id>", "<custom_event>", handler)` for non-reserved events the backend hook yields.
5. If your backend hook emits `reasoning` with a pipeline pass id, call `registerWorkflowPipeline({id: "<wid>", passes: [{id: "<wid>:<passname>"}]})`.
6. Wire any buttons/inputs with `registerAction("<id>", "<name>", (el) => …)` + `data-wf-action="<id>:<name>"` on the element (`data-wf-on="change"` for change-firing inputs) — never a `window.*` global or an inline `on*` attribute (sec. 11.5).
6. Inject CSS via `<link>` to `/static/workflows/<id>/<file>.css` from `index.js` (guard by element id).
7. For workflow phase pill, use `setWorkflowPhase(channel, label)` from frontend code OR yield `{event: "phase_status", data: {channel, label, state?}}` from a hook. `channel` is any string starting with `"workflow:"` (subkey it per operation, e.g. `"workflow:<id>:regen:<rootId>"`); `state == "done"` or a blank label clears it.

### 17.3 Config form

1. Workflow's `config_schema` (a JSON Schema dict) ships in the manifest.
2. Form populates from `GET /api/workflows/<id>/config` (effective values).
3. Save via `PUT /api/workflows/<id>/config` with `{config: {...}}` (full replacement; `{}` resets to defaults).
4. Backend reads via `get_workflow_config(wid)` (default-fallback aware).

### 17.4 Per-character data

1. Read/write via `get_workflow_character_state(character_id, wid)` / `set_workflow_character_state(...)`.
2. Hold `workflow_character_state_lock(character_id, wid)` (nested in `workflow_state_lock`) for RMW; import both from the toolkit. The PRE/POST iterators and the on-demand `/trigger` handler already hold both, so hook code on those paths needs no acquire; only call sites outside those paths must acquire.

### 17.5 Artifact production (POST_PIPELINE)

1. Yield `{type: "attach_artifact", attachment: {filename, mime, data: bytes OR path: str (exactly one), workflow_id: "<id>", source: "workflow:<id>", seed?, generation_metadata?, consumption_metadata?, annotation?}}` from the POST_PIPELINE hook. A `path` is read off disk. The entry is dropped unless BOTH `source == "workflow:<id>"` AND `workflow_id == "<id>"`.
2. Supply `seed` (non-empty str) AND `generation_metadata` (dict) so the row stays recoverable: eviction blanks a row's bytes unconditionally, and `/rehydrate` needs the stored seed to regenerate them. Separately, an attachment larger than the entire cache budget is rejected at insert when it lacks both (`OVERSIZE_NO_METADATA_REASON`); an in-budget attachment is accepted but becomes unrecoverable after eviction without them.
3. Implement the `REGENERATE` hook returning `list[dict]` of new sibling dicts. Each must satisfy the regenerate shape gate -- `filename` (str), `mime` (str), and exactly one of `data` (bytes) or `path` (str); the route stamps `workflow_id` and `parent_attachment_id=root_id` itself, and `source` is not required on this path (unlike POST_PIPELINE). A non-list return is treated as empty. Non-dict entries are skipped (server log). Dicts that fail the shape validator are not silently dropped -- they are returned to the caller in the response `rejected_workflow_atts` with a `reason`.
4. Implement `REROLL_GEN` hook returning `bytes` or `(bytes, dict | None)` from `(ctx, params, seed)`. The same hook backs both `/reroll-gen` (fresh seed) and `/rehydrate` (stored seed).

### 17.6 Locks recap

| Doing... | Hold... |
|---|---|
| RMW `workflow_state` | `workflow_state_lock(cid, wid)` (from the toolkit) |
| RMW `workflow_character_state` | `workflow_state_lock(cid, wid)` + `workflow_character_state_lock(character_id, wid)` (nested, in that order; both from the toolkit) |
| RMW `workflow_message_state` | `workflow_state_lock(cid, wid)` |
| RMW `workflow_config` | `workflow_config_lock()` |
| Mutating sibling group on a root | `_workflow_root_lock(root_id)` (held by route; not author code) |

---

## 18. Quick reference: where to look

| Task | Read |
|---|---|
| Add a new hook type | `contracts.py` + `registry.py` + matching dispatch: `iter_subscriptions` in `workflow_bridge.py` (fan-out pipeline hooks) or `get_subscription` in `main.py` (single-dispatch hooks) |
| Custom SSE event from backend to frontend | yield non-reserved name from hook -> `S.workflowEventHandlers["name"]` -- sec. 12.2, 12.3 |
| Drive in-turn status text | yield `phase_status` with `channel: "workflow:<id>"` -- sec. 12.2, 13.1 |
| Out-of-band status text | `setWorkflowPhase("workflow:<id>:...", label)` then `clearWorkflowPhase` in finally -- sec. 13.1 |
| Force a single tool call from a hook | `forced_tool_call(...)` -- sec. 6.4 |
| Author-side LLM client | `ctx.client` (PreCtx/PostCtx/OnDemandCtx/RegenCtx/RerollGenCtx) -- sec. 4 |
| Add a Tools-panel card | `registerWorkflowToolsPanelCard(wid, render)` (top-level in the workflow's `index.js`) -- sec. 11.4 |
| Karaoke-style text highlighting | `playAudio` + `channelState` polling + `startTextEffect(...).markActive` -- sec. 15.4, 16.5 |
| Render a custom widget for own attachments | `S.workflowAttachmentRenderers[wid] = (ctx) => htmlString` -- sec. 11.3, 14.2 |
| Read evicted attachment | not allowed; surface Rehydrate button or read `att.consumption_metadata` only -- sec. 9.2, 14.9 |
| Force cross-tab refresh after an out-of-band mutation | `broadcastWorkflowMutation({convId, msgId})` after the response -- sec. 13.4 |
