# Workflow development guide

Navigation map for authoring a workflow. Reader is assumed to know the rest of Orb's backend (FastAPI + aiosqlite, three-pass pipeline in `backend/orchestrator.py`) and frontend (vanilla JS modules mutating the global `S` object in `frontend/state.js`), and to be new to the workflow framework. Every section points at code; build the mental model from the cited source.

---

## 1. What a workflow is

A workflow is a Python record in the process-local registry -- one record per workflow id -- plus zero or more hook bindings into the turn pipeline and HTTP routes. Workflows can:

- Augment the in-turn pipeline (pre/post hooks).
- Emit out-of-turn HTTP responses (on-demand trigger).
- Produce per-message byte artifacts persisted in `workflow_attachments`. The artifact route surface is regenerate / reroll-gen (produce new bytes), rehydrate (re-synthesize in place), and activate / delete / access (lifecycle and access-tracking).
- Carry state across four DB-backed tiers (conversation, message, character, config) plus one in-memory per-turn scratch tier.
- Ship a frontend module that registers renderers (message buttons, attachment widgets, inspector/tools-panel cards -- a config panel is just a tools-panel renderer), click/text-effect/SSE handlers.

Built-in registered workflow: `tts` (`backend/workflows/tts/`, `frontend/workflows/tts/`). It binds four of the five hook types (post-pipeline, on-demand, regenerate, reroll-gen -- not pre-pipeline). For persistent state it uses the character and config tiers only. Cross-reference it as the worked example.

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
| `backend/locks.py` | `workflow_state_lock`, `workflow_character_state_lock`, `workflow_config_lock`. |
| `backend/main.py` | Workflow HTTP routes (1671--2211) + `_workflow_root_lock` (157). |
| `backend/orchestrator.py` | Pre-pipeline hook loop (`_iterate_pre_pipeline_hooks`) + post-pipeline hook loop (inline, over `iter_subscriptions(HookType.POST_PIPELINE)`) + `_stage_workflow_attachment` + `_persist_result` + `_consume_pipeline`. |
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
2. **Register + bind hooks**. Author calls `register_workflow(w)` + one `subscribe(w.id, HookType.X, fn)` per hook + `finalize_registry()`. ALL three calls live in `backend/workflows/__init__.py:108-115`, NOT in the workflow's own subdir.

### 3.1 `Workflow` dataclass -- data shape only (`registry.py:41-58`)

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

Live example: shipped TTS builds its `Workflow(...)` instance at `backend/workflows/tts/__init__.py:46-58`.

### 3.2 `ToolSpec` (`contracts.py:49-66`)

```
@dataclass class ToolSpec:
  name: str            # must equal schema["function"]["name"]
  schema: dict         # OpenAI-style tool schema
  choice: dict         # pre-built tool_choice payload
  standalone: bool     # default True; keeps tool out of pipeline union
```

### 3.3 `HookType` (`contracts.py:225-237`)

| Member | Value | Dispatch | Fires from |
|---|---|---|---|
| `PRE_PIPELINE` | `"pre_pipeline"` | Fan-out (every subscriber, priority-ascending) | During the turn, inside the pipeline |
| `POST_PIPELINE` | `"post_pipeline"` | Fan-out | During the turn, inside the pipeline |
| `ON_DEMAND` | `"on_demand"` | Single-dispatch by workflow id | `POST .../conversations/{cid}/workflows/{workflow_id}/trigger` |
| `REGENERATE` | `"regenerate"` | Single-dispatch by workflow id | `POST .../workflow-attachments/{aid}/regenerate` |
| `REROLL_GEN` | `"reroll_gen"` | Single-dispatch by workflow id | `POST .../workflow-attachments/{aid}/reroll-gen` and `.../{aid}/rehydrate` |

Single-dispatch hooks fire from their own HTTP routes, never from the turn pipeline. Note the name clash on `regenerate`: the message-level route `POST .../messages/{msg_id}/regenerate` reruns the three-pass pipeline via `handle_regenerate`, firing PRE_PIPELINE/POST_PIPELINE but no single-dispatch hook. The `REGENERATE` hook fires only from the attachment-level `POST .../workflow-attachments/{aid}/regenerate` route (`main.py:1751`).

### 3.4 Registration sequence

The package `__init__.py` imports each workflow's instance plus its hook callables and runs the three registration calls against them. Hooks are aliased on import (`_tts_*`) so that when a second workflow lands, its identically-named hooks (`post_pipeline`, etc.) will not shadow TTS's in the shared package namespace.

Live shape -- imports at `backend/workflows/__init__.py:66-72`, calls at `:108-115`:

```
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
finalize_registry()                                                    # step 3 (keep at file bottom)
```

- `register_workflow(w)` -- `registry.py:91-133`. Idempotent on `w.id`; re-registering the same id preserves the original insertion position, so manifest order stays stable across reloads (docstring `registry.py:107-108`). Raises `ToolNameCollision` if any declared tool name is a built-in, or if a newly-claimed name (one not already owned by a prior registration of this id) collides with another workflow's tool. Both checks run before any mutation, so a rejected call leaves the registry, `TOOLS`, and `STANDALONE_TOOLS` untouched. On re-registration the new `tools` list is diffed against the prior one: names new to this registration are registered, dropped names are removed from `TOOLS`/`STANDALONE_TOOLS`, and names in both have schema/choice/standalone overwritten (`registry.py:126-131`).
- `subscribe(workflow_id, hook_type, fn, *, priority=0)` -- `registry.py:136-150`. Appends a `Subscription` to `w.subscriptions`. Raises `LookupError` if id unknown, `ValueError` on duplicate hook for same id, `ValueError` on `REGENERATE`/`REROLL_GEN` without `produces_artifacts=True`.
- `finalize_registry()` -- `registry.py:180-200`. Every `produces_artifacts=True` workflow MUST also have `REGENERATE` and `REROLL_GEN` bindings; missing either raises `WorkflowMandateError` at import time.

### 3.5 Lookups (`registry.py`)

- `get_workflow(workflow_id) -> Workflow | None` -- `:208`.
- `get_subscription(workflow_id, hook_type) -> Subscription | None` -- `:164`. Collapses "unknown id" and "unbound hook" to None.
- `iter_subscriptions(hook_type) -> list[Subscription]` -- `:153`. Priority-ascending, registration-order tie-break (stable sort).
- `list_workflows() -> list[Workflow]` -- `:203`. Registration order.
- `workflow_has_hook(w, hook_type) -> bool` -- `:176`.

### 3.6 Manifest route

`GET /api/workflows` (`main.py:1674`). Returns a list; each entry `{id, display_name, config_schema, config_defaults}`. Frontend fetches once at boot via `loadWorkflowManifest` (`chat.js:2855`) into `S.workflowManifest`.

---

## 4. Hook context dataclasses (`contracts.py`)

All Ctx are `@dataclass(frozen=True)`. Mutable fields routed through `_readonly(...)` (recursive: `dict -> MappingProxyType`, `list/tuple -> tuple`, `set/frozenset -> frozenset`, `bytearray -> bytes`; `:24-46`). `turn_scratch`, `client`, `kv_tracker` stay unwrapped.

### 4.1 PreCtx (`:69-105`) -- paired with PRE_PIPELINE

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

### 4.2 PostCtx (`:108-146`) -- paired with POST_PIPELINE

Same shape with these substitutions:

| Field | Note |
|---|---|
| `draft` | str -- current draft, updated by prior hooks' `draft_replaced`. |
| `effective_msg` | str -- user message after director rewrite. |
| `director_output` | MappingProxyType -- `{active_moods, raw, calls, latency, rewritten_msg, extra_fields, progressive_fields}`. |
| `enabled_tools` | MappingProxyType -- merged pipeline tool-enable map (replaces `enabled_tools_pre_merge`). |
| `prefix` (note differs) | Final pipeline prefix; pre-pipeline extras already appended. |
| `history` | tuple -- same read-only prior-message list PreCtx received; excludes this turn's user message and the in-flight assistant message (the current user message is `effective_msg`). |
| No `last_user_message` / `enabled_tools_pre_merge`. |

### 4.3 OnDemandCtx (`:149-164`) -- paired with ON_DEMAND

Fields: `conversation_id`, `history`, `last_user_message`, `settings`, `client`, `character_id`, `character`. No `turn_scratch`, `kv_tracker`, `prefix`, `enabled_tools`, `schema_overrides`.

### 4.4 RegenCtx (`:167-192`) -- paired with REGENERATE

Fields: `conversation_id`, `message_id`, `attachment_id`, `original_attachment`, `history` (strictly before anchor message), `last_user_message`, `settings`, `client`, `character_id`, `character`. No turn-scoped fields.

### 4.5 RerollGenCtx (`:195-222`) -- paired with REROLL_GEN

Fields: `conversation_id`, `message_id`, `attachment_id`, `original_attachment`, `settings`, `client`, `prior_consumption_metadata`. No history, no character. Shared backend for `/reroll-gen` and `/rehydrate`; the hook does not branch on route.

### 4.6 Hook callable signatures (`contracts.py:240-244`)

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

### 5.1 Shared in-process locks (`backend/locks.py`)

| Lock | Key | Scope | Defined |
|---|---|---|---|
| `workflow_state_lock(cid, wid)` | `(cid, wid)` | Per `(conversation, workflow)` | `:53-57`, dict `:50` |
| `workflow_character_state_lock(character_id, wid)` | `(character_id, wid)` | Per `(character_card, workflow)` | `:63-67`, dict `:60` |
| `workflow_config_lock()` | (none) | Process-global; serializes all `workflow_config` RMW across every workflow id | `:73-78`, dict `:70` |

Non-reentrant `asyncio.Lock`s. Nesting order at every site: `workflow_state_lock` outer, `workflow_character_state_lock` inner.

### 5.2 `_workflow_root_lock(root_id)` (`backend/main.py:156-160`, dict `:153`)

Distinct, int-keyed space (`dict[int, asyncio.Lock]`), keyed on the root attachment id. Held by the five attachment-mutating routes: `/regenerate`, `/reroll-gen`, `/rehydrate`, `/activate`, `/delete`. It serializes concurrent edits to one attachment's variant group (the root row plus its sibling variants), so two callers cannot interleave a read-modify-write on the same group. It is never nested with `workflow_state_lock` or `workflow_character_state_lock` at any call site and so sits outside their ordering rule.

### 5.3 Acquisition sites

| Lock | Held by |
|---|---|
| `workflow_state_lock` (outer) + `workflow_character_state_lock` (inner) | PRE-pipeline iterator (`orchestrator.py:601`), POST-pipeline iterator (`orchestrator.py:378`), `/trigger` route (`main.py:1720` outer + `:1734` inner). Workflow code doing read-modify-write on workflow_state acquires the same locks via the `toolkit` re-export (`backend/workflows/toolkit.py`). |
| `workflow_config_lock` | `PUT /api/workflows/{workflow_id}/config` (`main.py:1695`). Workflow code doing read-modify-write on workflow_config acquires it via the `toolkit` re-export. |

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
get_workflow_state(cid, wid)              -> dict | None        # :213
set_workflow_state(cid, wid, payload)                            # :218
get_workflow_message_state(mid, wid)      -> dict | None         # :223
set_workflow_message_state(mid, wid, payload)                    # :228
get_workflow_character_state(char_id, wid) -> dict | None        # :233
set_workflow_character_state(char_id, wid, payload)              # :237
get_workflow_config(wid)                  -> dict (default-fallback) # :241
set_workflow_config(wid, payload)                                # :263
```

Passing `payload=None` to a `set_*` state writer deletes that slot. `set_workflow_config(wid, {})` clears the persisted slot, so the next `get_workflow_config(wid)` returns a fresh copy of the workflow's `config_defaults`. None of these acquire locks; callers doing a read-modify-write MUST hold the matching lock from sec. 5.

### 6.4 `forced_tool_call` (`_forced_call.py:43-136`)

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

### 6.5 `overlay_enable_tools(base, contribution) -> dict[str, bool]` (`registry.py:275-301`)

Fresh `dict` copy of `base` with `contribution`'s True entries merged. Accepts `set` / `frozenset` (presence => True), `Mapping[str, bool]` (True entries kept, False dropped), or `None` (returns a fresh copy of `base` unchanged); an empty `set`/`Mapping` likewise yields an unchanged copy. Use to compute the merged enable map for `forced_tool_call`.

### 6.6 `insert_workflow_attachment` (re-export from cache)

The only attachment writer exposed to authors. See sec. 9.

### 6.7 Workflow locks (re-exports from `backend.locks`)

`workflow_state_lock(cid, wid)`, `workflow_character_state_lock(character_id, wid)`, and `workflow_config_lock()`. Hold the matching lock across a read-modify-write on the corresponding state tier (sec. 5, sec. 10). `workflow_character_state_lock` nests inside `workflow_state_lock` (conversation lock outer, character lock inner). There is no dedicated message-state lock: serialize a message-state RMW under `workflow_state_lock(cid, wid)` of the message's owning conversation.

---

## 7. In-turn integration (`backend/orchestrator.py`)

### 7.1 Turn entry points

| Function | def line | First built-in event | Last event |
|---|---|---|---|
| `handle_turn` | `:1082` | `user_message_created` (`:1141`) | `done` (`:1074`) |
| `handle_regenerate` | `:1246` | `director_start` (`:168`) or, when the director block is skipped, `director_done` (`:228`) | `done` (`:1074`) |
| `handle_super_regenerate` | `:1369` | same as `handle_regenerate` | `done` (`:1074`) |

All three run PRE-pipeline hooks first. `handle_regenerate` / `handle_super_regenerate` skip `user_message_created` -- they do not persist a new user row. `done` (`:1074`) fires last from `_consume_pipeline` on any turn that completes without raising -- it sits after the pipeline's `try/finally`, so a pipeline exception propagates past it.

`handle_magic_rewrite` (`:1515`) does NOT use pre/post hooks; out of scope for workflow integration.

### 7.2 Per-turn shared identities

- `turn_scratch: dict = {}` allocated once per turn; entry-point lines `:1157`, `:1276`, `:1423`. Same object reference into every PreCtx and PostCtx (`:612` PRE wrap site, `:391` POST wrap site -- both `turn_scratch=turn_scratch`, no `_readonly`). Writes during PRE visible to POST.
- `schema_overrides: dict = {"direct_scene": build_direct_scene_tool(ctx["director_fragments"])}` -- entry-point lines `:1159`, `:1278`, `:1425`. Threaded into pre-pipeline iter, `_run_pipeline`, every pass (`_director_pass`, `_writer_pass`, `editor_pass`), and exposed read-only on PreCtx/PostCtx for `forced_tool_call` reuse.
- `client = LLMClient(...)` built in `_load_pipeline_context` (`:706`); attached to PreCtx.client / PostCtx.client (raw, not macros-wrapped).
- `kv_tracker` -- per-turn `_KVCacheTracker`; ref-shared across all passes and ctx fields.

### 7.3 PRE_PIPELINE iteration (`_iterate_pre_pipeline_hooks` `:567-675`)

For each subscription (priority-ascending):

1. Acquire `workflow_state_lock(cid, wid)` then `workflow_character_state_lock(character_id or "", wid)` (`:601-603`).
2. Build `PreCtx` (`:605-618`).
3. `async for ev in sub.callable(pre_ctx)`. Dispatch on `ev.get("type")`:

| Event `type` | Effect | Code |
|---|---|---|
| `"enable_tools"` | Merge `ev["tools"]` into `accumulators["merged_enabled_tools"]`: `set`/`frozenset` -> each name True; `dict` -> entries whose value is exactly `True`. Names not in `TOOLS`, dict values that are not `True`, and a `tools` payload that is not set/frozenset/dict each drop (the whole event, for a bad payload) with WARNING. | `:621-651` |
| `"system_prompt"` | Append `ev["block"]` to `accumulators["extras"]` if it is a non-whitespace `str` (empty/whitespace-only dropped with WARNING). | `:652-661` |
| neither | Forward `ev` to SSE stream verbatim. | `:673` |

Reserved-name rule: any `ev["event"]` that is a string starting with `_` is dropped with WARNING (`:665-672`).

Error containment: each subscription's body wrapped in `try / except Exception` (`:604-675`). One bad hook is logged-and-skipped.

Post-loop application (entry points `:1185-1190` and analogues): `extras` non-empty triggers `_build_prefixes(ctx, history, extra_system_blocks=extras)` rebuild. `merged_enabled_tools` is fed to `_run_pipeline(enabled_tools=...)`.

### 7.4 POST_PIPELINE iteration (inside `_run_pipeline` `:372-472`)

For each subscription:

1. Acquire `workflow_state_lock(conversation_id or "", wid)` + `workflow_character_state_lock(character_id or "", wid)` (`:378-380`).
2. Build `PostCtx` (`:382-397`).
3. `async for ev in sub.callable(post_ctx)`. Dispatch on `ev.get("type")`:

| Event `type` | Effect | Code |
|---|---|---|
| `"draft_replaced"` | One per hook. `ev["draft"]` must be a str differing from current `draft`, else WARNING + drop. On accept: `draft = ev["draft"]`, yield `{"event": "writer_rewrite", "data": {"refined_text": draft}}`. | `:400-420` |
| `"attach_artifact"` | Gated on `get_workflow(wid)` resolving with `produces_artifacts` truthy (unknown workflow or unset flag -> WARNING + drop). Validated via `_stage_workflow_attachment`. Survivors appended to local `staged_attachments`. No SSE event at attach time. | `:421-440` |
| `"set_message_state"` | `ev["state"]` must be a dict (else WARNING + drop). On accept: staged under the hook's `workflow_id` (last-wins), then written to the new assistant message's per-message state slot in `_persist_result` once the assistant row exists. No SSE event. | `:441-456` |
| neither | Forward `ev` to SSE stream (`:470`). Underscore-prefixed `ev["event"]` dropped (`:462-469`). |

Error containment: each subscription wrapped in `try / except Exception` (`:381-472`).

### 7.5 `_stage_workflow_attachment(att, workflow_id) -> dict | None` (`:478-564`)

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

The orchestrator owns these `event:` names: built-ins it emits itself, and underscore-prefixed internals it drops before they reach the wire (PRE `:666-672`, POST `:463-469`). A hook that yields one collides with the orchestrator's own use. (Producer line = the `yield` statement; the literal `"event"` key may sit a line below.)

| Event | Producer line |
|---|---|
| `user_message_created` | `:1141` (only `handle_turn`) |
| `director_start` | `:168` |
| `director_done` | `:228` |
| `prompt_rewritten` | `:205` |
| `token` | `:273` |
| `reasoning` | `:188`, `:267`, `:332` (built-ins); custom pipelines: see sec. 13.2 |
| `writer_rewrite` | `:340` (editor), `:419` (post-pipeline draft_replaced) |
| `editor_done` | `:345` |
| `workflow_attachments_rejected` | `_consume_pipeline:1031` |
| `done` | `_consume_pipeline:1074` |
| `error` | entry-point guard returns + `except` blocks |
| `_result`, `_editor_reasoning`, `_refined_result` | Internal; never reach SSE wire. |

Any other event name passes through.

`phase_status` is hook-emitted, not reserved: a workflow yields it as a passthrough event to drive the built-in phase pill, and `chat.js` handles it (sec. 13.1).

### 7.7 `_persist_result` (`:858-908`)

Runs unconditionally (subject to each step's own guard):

1. `db.update_director_state(...)` if `enable_agent` truthy (`:872-877`).
2. `db.update_message_content(user_msg_id, effective_msg)` if director rewrote (`:878-879`).

Then, only when `resp_text.strip()` (`:885`):

3. `db.add_message(..., attachments=staged, ...)` -- single transaction. Internally lazy-imports `insert_workflow_attachments` from cache. Returns `(asst_id, rejected_workflow_atts)` (`:890`).
4. For each post-pipeline `set_message_state` entry, `db.set_workflow_message_state(asst_id, wid, payload)` (`:902-903`). The assistant `mid` is first known here; unlocked because the row is not yet the active leaf and no other caller can name it.
5. `db.set_active_leaf(conversation_id, asst_id)` (`:904`).

Empty `resp_text.strip()` short-circuits steps 3-5 only: no assistant row, no attachments, no message state, returns `(None, [])` (`:906-908`). Steps 1-2 have already run regardless.

### 7.8 `_consume_pipeline` (`:992-1074`)

Reads from `_run_pipeline`, dispatches by `event["event"]`:

| event | Effect |
|---|---|
| `"token"` | Accumulate `accumulated_text`; re-yield. |
| `"_result"` | Set `res`. Call `_persist_result`. If rejected non-empty and `asst_id` not None, yield `workflow_attachments_rejected`. NOT re-yielded. |
| `"_editor_reasoning"` | Copy onto `res`. NOT re-yielded. |
| `"_refined_result"` | Overwrite `res["resp_text"]`; rewrite the assistant DB row when one was persisted. NOT re-yielded. |
| anything else | Re-yield verbatim. |

Trailing `yield {"event": "done"}` at `:1074`.

Note: when `resp_text` is empty, `_persist_result` short-circuits (sec. 7.7) and returns `(None, [])`, so any staged `attach_artifact` or `set_message_state` entries are dropped, the former without a `workflow_attachments_rejected` event. An artifact-only or message-state-only hook produces nothing on a turn whose writer emitted no text.

### 7.9 Wire-event order on a normal turn

`user_message_created`? -> PRE passthrough events* -> `director_start`? -> `reasoning(director)`? -> `prompt_rewritten`? -> `director_done`? -> `reasoning(writer)`? -> `token`* -> `reasoning(editor)`? -> `writer_rewrite`? -> `editor_done`? -> POST-hook events* (`writer_rewrite` from `draft_replaced`, plus passthrough, interleaved per hook in priority order) -> `workflow_attachments_rejected`? -> `done`.

`?` = conditional. `director_start` and `reasoning(director)` run only when the agent is on and a pre-writer tool is enabled (`:167`); `prompt_rewritten` additionally requires the director to have rewritten the message. `director_done` fires unconditionally (`:228`, outside the `:167` block), absent only when the turn aborts at the post-director stop check (`:211`). Each `reasoning(pass)` fires only when that pass's reasoning flag is set (director on by default, writer/editor off; `:111-113`); `user_message_created` is suppressed when the caller pre-persisted the user row.

---

## 8. HTTP routes (`backend/main.py`)

### 8.1 Per-route reference cards

#### GET `/api/workflows` (manifest)

Handler `api_list_workflows` (`:1675`). No locks. Response: JSON list of `{id, display_name, config_schema, config_defaults}` in registration order. No errors.

#### PUT `/api/workflows/{wid}/config`

Handler `api_set_workflow_config` (`:1689`). Body model `WorkflowConfigUpdate` (`:242-245`): `{"config": dict}`, REQUIRED -- missing key is FastAPI 422 before handler. Lock: `workflow_config_lock()` (`:1695`). DB: `set_workflow_config(wid, data.config)` then `get_workflow_config(wid)`. Response: `{"config": <effective>}` (post-write read; empty dict slot falls back to `config_defaults`). 404 if unregistered.

#### GET `/api/workflows/{wid}/config`

Handler `api_get_workflow_config` (`:1703`). No locks. DB: `get_workflow_config(wid)`. Response `{"config": <effective>}`. 404 if unregistered.

#### POST `/api/conversations/{cid}/workflows/{wid}/trigger`

Handler `api_trigger_workflow` (`:1711`). Body: raw `dict` (default `{}`). Lookup: `get_subscription(wid, HookType.ON_DEMAND)` 404 if None. Outer lock `workflow_state_lock(cid, wid)` (`:1720`); under it, DB reads: `get_conversation(cid)` (404), `get_character_card(card_id)` if any, `get_messages(cid)`, `get_settings()`, then build the `LLMClient`. Inner lock `workflow_character_state_lock(conv.get("character_card_id") or "", wid)` (`:1734`); under it, build `OnDemandCtx` and `await sub.callable(od_ctx, body)`. Returns the hook's return value verbatim (the on_demand contract is a dict). Hook exception -> 500.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate`

Handler `api_regenerate_attachment` (`:1752`). Body: raw `dict`. Pre-lock: `get_conversation(cid)` (404), then `get_workflow_attachment_by_id(aid)` (404 if missing or `message_id != mid`), `get_subscription(att["workflow_id"], HookType.REGENERATE)` (404). `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)` (`:1771`). DB reads (in lock): `get_message_by_id(mid)` (404 if cid mismatch), `get_messages_before(cid, mid)` (history strictly before anchor), `get_settings()`; build LLMClient; `get_character_card(card_id)`; then build `RegenCtx`. `await sub.callable(regen_ctx, body) -> list[dict]`. Non-list coerced to `[]`; non-dict entries dropped silently (logged, not rejected -- a rejection record needs a filename to surface in the UI). Each dict entry is stamped with `workflow_id` and `parent_attachment_id=root_id`. Rejections come from two stages and merge into one list: (1) pre-insert -- `validate_workflow_attachment_shape` failures; (2) at insert -- entries the LRU-budget batch insert refuses (oversize without rehydrate metadata). Survivors batch-insert via `insert_workflow_attachments(mid, fixed)`, which returns `(new_ids, helper_rejected)`. Both rejection sets are projected to `{filename, workflow_id, mime, reason, originating_attachment_id}` (`originating_attachment_id=root_id`). Response: `{"attachments": new_ids, "rejected_workflow_atts": <stage-1 + stage-2>}`. A raise inside the insert (`ValueError | LookupError | OSError`) -> 500.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen`

Handler `api_reroll_gen_attachment` (`:1924`). Body unused. `get_conversation(cid)` (404), `att` (404), `anchor` (404), `sub` (REROLL_GEN, 404). `params` = `att["generation_metadata"]` decoded as JSON, coerced to `{}` on empty / parse fail / non-dict. `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)` (`:1959`). `seed = _generated_seed()` (`secrets.token_hex(16)`, `:1917`). Build `RerollGenCtx` via `_build_reroll_gen_ctx` (`:1904`) + LLMClient. `await sub.callable(ctx, params, seed) -> bytes | (bytes, dict | None)` -- normalize via `_split_reroll_gen_result` (`:1882`). Empty/non-bytes => 500. Build new sibling dict: fresh `seed`, inherited `generation_metadata=params`, optional new `consumption_metadata`, `workflow_id=sub.workflow_id`, `parent_attachment_id=root_id`, `filename=att.get("filename") or sub.workflow_id`, `mime=att.get("mime_type") or "application/octet-stream"`, `annotation` copied from `att`. `insert_workflow_attachment(mid, new_attachment)`. Response: `{"attachment_id": new_id, "rejected_workflow_atts": [...0-or-1...]}`.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate`

Handler `api_rehydrate_attachment` (`:2016`). Body unused. Pre-lock: `get_conversation(cid)` (404), `get_workflow_attachment_by_id(aid)` (404 if missing or `message_id != mid`), `get_message_by_id(mid)` (404 if cid mismatch). 409 gates: `att["data_b64"] != EVICTED_MARKER` (already restored), `att["seed"]` empty. `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)` (`:2050`). In-lock re-read of `att`; 409 if `data_b64` no longer evicted (race). 404 if no REROLL_GEN sub. Same `_build_reroll_gen_ctx` + `await sub.callable(ctx, params, seed)` where `seed = att["seed"]` (stored). `_split_reroll_gen_result` normalize. Write via `rehydrate_attachment(aid, bytes, consumption_metadata=...)` (`backend/workflows/attachment_cache.py:143`) -- in-place UPDATE on the same row. `RehydrateAlreadyDoneError` (subclass of `ValueError`) -> 409. Other `(LookupError, ValueError)` -> 500. Response: `{"attachment_id": aid}` (echoed).

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/activate`

Handler `api_activate_workflow_attachment` (`:2108`). Body: `{"sibling_id": int | None}`. No hook. Pre-lock: `get_conversation(cid)` (404), `get_message_by_id(mid)` (404 if cid mismatch); non-int `sibling_id` rejected 400, including the `bool`-is-`int` case (`:2123-2124`). The URL `aid` is interpreted as the ROOT id (verified inside `set_active_sibling` -- not pre-checked at the route). Lock: `_workflow_root_lock(aid)` (`:2127`). DB: `set_active_sibling(aid, sibling_id, expected_message_id=mid)` (`backend/workflows/attachment_cache.py:748`). `LookupError` -> 404, `ValueError` -> 400. Response: `{"active_sibling_id": <echoed>}`.

#### POST `/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete`

Handler `api_delete_workflow_attachment` (`:2137`). Body: `{"scope": "variant" | "group"}`. No hook. Pre-lock (in order): `get_conversation(cid)` (404), `get_message_by_id(mid)` (404 if cid mismatch), `scope` validation (400, checked before the attachment lookup), `get_workflow_attachment_by_id(aid)` (404 if missing or `message_id != mid`). `root_id = att["parent_attachment_id"] or aid`. Lock: `_workflow_root_lock(root_id)` (`:2158`). DB: `delete_workflow_attachments(aid, scope=scope, expected_message_id=mid)` (`backend/workflows/attachment_cache.py:812`). `LookupError` -> 404, `ValueError` -> 400. Response: `{"deleted_ids": [...], "group_empty": bool, "root_id": <post-op>, "active_sibling_id": int | None}`.

#### POST `/api/conversations/{cid}/workflow-attachments/access`

Handler `api_record_workflow_attachment_access` (`:2168`). Body: `{"ids": list[int]}`. No hook, no `_workflow_root_lock`. Validation: 404 on missing conversation; 400 if `ids` not a list; per-element drop on `isinstance(bool)` first, then `isinstance(int)` keep, else drop; empty filtered list short-circuits to `{"ok": True, "recorded": 0}`. JOIN `workflow_attachments` x `messages` filtered by `m.conversation_id = ?` -- silently drops ids not on this conversation. Survivors re-ordered to input order. Call `record_access(ordered_valid)` (`backend/workflows/attachment_cache.py:306`). Response: `{"ok": True, "recorded": n}`.

### 8.2 Helpers used by attachment routes

- `_workflow_root_lock(root_id)` -- `:156-160` (backed by the `_workflow_root_locks` dict at `:153`). Per-int-key asyncio lock.
- `_decode_stored_consumption_metadata(att)` -- `:1867`. Parses `att["consumption_metadata"]` JSON; None on empty, malformed, or non-dict.
- `_split_reroll_gen_result(result, wid) -> (data, cm)` -- `:1882`. Accepts `bytes` or `(bytes, dict | None)`; non-dict second element coerced to None with WARNING. Used by `/reroll-gen` and `/rehydrate`.
- `_build_reroll_gen_ctx(cid, mid, aid, att, settings, client) -> RerollGenCtx` -- `:1904`.
- `_generated_seed() -> str` -- `:1917`. `secrets.token_hex(16)` (32-char lowercase hex).

---

## 9. Attachment cache (`backend/workflows/attachment_cache.py`)

### 9.1 Schema

Migration `backend/database/migrations/0020_workflows.py` (sole migration touching this subsystem).

Table `workflow_attachments` (`:95-114`):

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

Added by 0020 (PRAGMA-guarded ADD COLUMN, `:43-49`, `:55-63`):

- `conversations.workflow_state` TEXT DEFAULT NULL
- `messages.workflow_state` TEXT DEFAULT NULL
- `character_cards.workflow_state` TEXT DEFAULT NULL
- `settings.workflow_config` TEXT NOT NULL DEFAULT `'{}'`
- `settings.attachment_cache_budget_bytes` INTEGER NOT NULL DEFAULT 524288000 (500 MiB)
- `settings.attachment_access_counter` INTEGER NOT NULL DEFAULT 0

No CREATE INDEX besides implicit PRIMARY KEY.

### 9.2 EVICTED_MARKER + budget

- `EVICTED_MARKER = "[evicted]"` (`:27`). Replaces `data_b64` on eviction; all other columns preserved.
- Budget: `settings.attachment_cache_budget_bytes` (live read each call via `_get_budget_bytes_on`, `:103`).
- "LRU-3": `recent_accesses` keeps at most 3 counter values (`:299` -- `new_list = ([assigned] + cur)[:3]`). Eviction sort key is the *oldest* of those values (`_lru3_key` -- `:71-86`); rows evict oldest-counter-first, so a single recent touch does not indefinitely pin an otherwise-idle row. Rows with empty/missing `recent_accesses` sort last (`+inf`) and are never first to evict.

### 9.3 Validation: `validate_workflow_attachment_shape(att) -> (bool, reason | None)` (`:363-417`)

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
| `get_workflow_attachment_by_id(aid)` | `dict | None` | (read-only, `database/queries/workflow_attachments.py:50`) | -- |
| `get_budget_bytes()` | `int` | (read-only) | -- |
| `evict(aid)` | `None` | BEGIN IMMEDIATE | -- |

Only `insert_workflow_attachment` is re-exported from `toolkit.py`. The others are called by the routes, the orchestrator, or the cache's own internal paths.

### 9.5 Insert flow

1. Reject if `not _is_produces_artifacts_workflow(workflow_id)` -> tagged `WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON`.
2. `_check_flat_parent_on` (`:420`) -- parent must exist, must have `parent_attachment_id IS NULL`, must be on same message.
3. Size via `_estimate_size`. If `size > budget` and rehydratable (`seed` non-empty str AND `generation_metadata` dict; `_is_rehydratable` `:350`), insert as marker; if not rehydratable, reject with `OVERSIZE_NO_METADATA_REASON`.
4. Otherwise evict existing rows via `_lru3_key`-sorted candidates until residual `(occupied + new_size) - budget <= 0`.
5. `insert_workflow_attachment_row` (`backend/database/queries/workflow_attachments.py:64`) issues `SELECT id FROM messages WHERE id=?` then `INSERT INTO workflow_attachments(...) VALUES(?,?,?,?,?,?,?,?,?,?,?)`.
6. Birth-as-access via `_record_access_inner`.
7. Optional `_set_active_sibling_on` when `mark_active=True`.

`insert_workflow_attachments` (batch) runs three partition stages (`:625-704`): Step 0 routes non-`produces_artifacts` workflows to `rejected_atts` (same policy as the single-row path); Step A markers/rejects oversize new atts biggest-first (tie-break by input index), so markering one big att can spare many small existing rows; Step B then runs the same step-4 eviction over existing rows for any residual shortfall.

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

### 9.8 Rejection reason constants (`:58-59`)

- `OVERSIZE_NO_METADATA_REASON = "too large to cache, no recovery metadata"`
- `WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON = "workflow does not declare produces_artifacts"`

Validator-emitted reasons come from each gate in `validate_workflow_attachment_shape`.

### 9.9 Exception-to-HTTP map

| Exception | Raised at | Caught at | HTTP |
|---|---|---|---|
| `RehydrateAlreadyDoneError` | rehydrate row no longer evicted | `main.py:2095` | 409 |
| `LookupError` (set_active_sibling) | root/sibling missing / wrong message | `main.py:2129` | 404 |
| `ValueError` (set_active_sibling) | not a root / sibling not in group | `main.py:2131` | 400 |
| `LookupError` (delete) | target missing / wrong message | `main.py:2160` | 404 |
| `ValueError` (delete) | bad scope | `main.py:2162` | 400 |
| `(ValueError, LookupError, OSError)` (insert) | shape / parent / FS | `main.py:1847`, `main.py:1993` | 500 |

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

POST_PIPELINE hooks commit `workflow_message_state` for the in-flight assistant message by yielding `set_message_state`; the orchestrator writes the slot in `_persist_result` once the new `mid` exists (sec 7.4, 7.7). The toolkit `set_workflow_message_state` setter still only addresses already-persisted `mid`s, since the assistant `mid` is assigned during `_persist_result`, after the POST loop.

---

## 11. Frontend boot + state surface

### 11.1 Boot

Order:

1. `loadWorkflowManifest()` (`chat.js:2855`) -- `await api.get("/workflows")` into `S.workflowManifest`.
2. `loadWorkflowModules()` (`workflow_loader.js:8`) -- for each manifest entry: `await import("/static/workflows/<id>/index.js")` (sequential, manifest order). 404s and module throws caught. If any module loaded, the loader re-runs `renderToolsPanel()` (`workflow_loader.js:26`): the Tools panel paints once before modules load, so freshly pushed cards would otherwise stay hidden behind the stale paint.

Both run inside `initAll` in `app.js`.

### 11.2 Module convention

A workflow's frontend code lives under `frontend/workflows/<id>/`. The framework dynamic-imports only `index.js` (served at `/static/workflows/<id>/index.js`). Multi-file workflows fan out through ordinary relative imports from `index.js`. CSS: inject a `<link href="/static/workflows/<id>/<file>.css">` from `index.js`, guarded by element id; framework does not load workflow CSS.

Top-level `register*` and `S.workflow*` push/assign calls run on import. Manifest order = module load order = registry push order.

### 11.3 `S.workflow*` slots (`state.js:65-91`)

| Slot | Initial | Write path | Read path (built-in) |
|---|---|---|---|
| `workflowInspectorCardRenderers` | `[]` | `S.workflowInspectorCardRenderers.push(() => htmlString)` | `_buildSecondaryAgentsHtml` (`chat.js:2729`) |
| `workflowToolsPanelRenderers` | `[]` | `.push(() => htmlString)` | `renderToolsPanel` (`settings.js:1124`) |
| `workflowMessageButtonRenderers` | `[]` | `.push((msg) => htmlString)` | `_renderExtraButtons` (`chat.js:133`) |
| `workflowEventHandlers` | `{}` | `S.workflowEventHandlers["my_event"] = (data, msgDiv) => ...` | `handleSSEEvent` default (`chat.js:2369`) |
| `workflowAttachmentRenderers` | `{}` | `S.workflowAttachmentRenderers[wid] = (ctx) => htmlString` | `_renderWorkflowSwipeContainer` (`chat.js:280`) |
| `workflowPipelines` | `[]` | via `registerWorkflowPipeline` only | SSE `reasoning` routing (`chat.js:2256`); Inspector Secondary rail |
| `workflowState` | `{}` | `S.workflowState[wid] = <opaque>` | author only (framework never reads) |
| `workflowPhases` | `{}` | via `setWorkflowPhase` / `clearWorkflowPhase` only | `_renderWorkflowPhasesPill` (`chat.js:2809`) |
| `workflowTextEffects` | `[]` | via `registerTextEffect` only | segmentation gate (`chat.js:855`, `:1481`) |
| `workflowClickHandlers` | `[]` | via `registerClickHandler` only | segmentation gate (`chat.js:855`, `:1481`); click router (`workflow_text_interaction.js`) |
| `workflowManifest` | `[]` | (framework writes at boot) | `workflow_loader.js:8` (module-load loop); `chat.js` regen/reroll-button gates + label helpers (`:199`, `:210`, `:251`, `:2851`) |
| `reasoningByPass` | `{}` | (framework writes via SSE + `registerWorkflowPipeline` seed; reset per turn / conversation switch) | rail render |
| `inspectorTab` | `"main"` | via `setInspectorTab` only | tab paint |
| `toolsTab` | `"main"` | via `setToolsTab` only | tab paint |
| `rejectedWorkflowAtts` | `[]` | (framework writes via `_mergeWorkflowRejections`; per-tuple replace, empty incoming clears) | rejection chip render |

An author may read its own entry from `S.workflowManifest` (matched by `id`) for `display_name`, `config_schema`, or `config_defaults` (`main.py:1678`). Config *values* are not in the manifest -- read or write the live config slot via `GET` / `PUT /workflows/<id>/config`.

### 11.4 Exported registrars (`state.js`)

```
registerWorkflowPipeline({id, label?, passes:[{id, label?}]})    # :102
registerTextEffect({id, label?})                                  # :140
registerClickHandler({id, label?, priority?, claims?, onClick})   # :156
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
- Registering any effect enables body word-segmentation -- without a registered effect or click handler, `.seg` spans are never produced (`chat.js:855`, `:1481`).

`registerClickHandler` (validation + defaults):

- `label` -> `id`.
- `priority` -> `0` (integers only).
- `claims` -> `() => true` (claims all).
- `onClick` required (function).

All three registrars are idempotent on `id` (replace in place).

---

## 12. SSE dispatch (`frontend/chat.js`)

### 12.1 `processSSEStream` (`:2104`)

Frames `event: <name>` / `data: <json>` pairs from a `fetch` body stream. Per pair, calls `handleSSEEvent(event, data, container, msgDiv, onToken, onRewrite)`. Clears `S.pendingRefineDiff` (`:2114`) and resets reasoning state (`:2117-2123`) at entry. Reading aborted via signal throws an `AbortError`.

### 12.2 `handleSSEEvent` (`:2178`)

Built-in cases:

| event | Effect |
|---|---|
| `director_start` | phase=directing; clear inspected; `renderInspector` |
| `director_done` | set `S.lastDirectorData`; advance reasoning pass; `renderInspector` |
| `prompt_rewritten` | patch user content + DOM |
| `token` | phase=generating; appends the token to the response buffer, mirrors it into `S.streamingContent`, and repaints |
| `writer_rewrite` | phase=refining; build sentence diff; `onRewrite(refined_text)` |
| `reasoning` | route by `data.pass`: a built-in pass (`director`/`writer`/`editor`) appends to `S.reasoningDirector`/`Writer`/`Editor`; otherwise match against a registered pipeline's pass ids in `S.workflowPipelines` and append to `S.reasoningByPass[pass]` |
| `phase_status` | requires `data.channel` to start `"workflow:"`; calls `clearWorkflowPhase(channel)` when `state === "done"` or the label is missing/blank, else `setWorkflowPhase(channel, label)` |
| `editor_done` | append `tool_calls` to `S.lastDirectorData` |
| `user_message_created` | patch pending user row id; optional in-flight edit POST |
| `error` | toast |
| `workflow_attachments_rejected` | `_mergeWorkflowRejections(msgId, null, rejected)`; no re-render |

Default branch (`:2369`): looks up `S.workflowEventHandlers[event]`; if a function, parses `data` with `JSON.parse`, falling back to the raw string on parse failure, then invokes `handler(payload, msgDiv)` -- `payload` is the parsed JSON or raw string, `msgDiv` is the streaming message element or `null`. The call is wrapped in `try/catch`; throws are logged via `console.error` and do not abort the stream.

No `done` case, so `done` falls through to the default branch and reaches `S.workflowEventHandlers["done"]` if a handler is registered.

### 12.3 Reserved event names (do not author-emit as custom)

These 11 names are intercepted by built-in `case`s in `handleSSEEvent` before the custom-handler default branch, so registering a handler for them has no effect: `token`, `director_start`, `director_done`, `prompt_rewritten`, `writer_rewrite`, `reasoning`, `phase_status`, `editor_done`, `user_message_created`, `workflow_attachments_rejected`, `error`. Separately, event names a workflow's pipeline hooks emit are filtered server-side: the orchestrator drops any underscore-prefixed name from `post_pipeline` (`orchestrator.py:463`) and `pre_pipeline` (`:666`) output, since the `_`-prefix is reserved for internal persistence signals (`_result`, `_refined_result`, `_editor_reasoning`). These never reach the frontend.

### 12.4 `afterStream` (`:1999`)

Awaited unconditionally at end of `runStreamRequest` (`:2415`) and `sendMessage` (`:2496`). Refetches `/conversations/<id>/messages`, refreshes director state, finalizes streaming DOM, clears workflow phases as backstop (`clearWorkflowPhase()` no arg, `:2014`).

---

## 13. Phase pill + reasoning rail + tabs + helpers

### 13.1 Phase pill

```
setWorkflowPhase(channel, label)    # chat.js:2834
clearWorkflowPhase(channel?)        # chat.js:2841
```

`channel` convention: `"workflow:<id>"` (the SSE handler enforces this prefix for inbound). For multiple concurrent same-workflow ops, suffix it (e.g. `"workflow:tts:regen:<rootId>"`) so they don't clobber each other.

- `setWorkflowPhase`: blank/whitespace `label` -> delete entry; otherwise set.
- `clearWorkflowPhase()` no arg wipes the whole map.
- `_renderWorkflowPhasesPill` (`:2809`) -- the most recently *added* channel wins the single visible slot. Re-setting an existing channel updates its label in place without reordering, so it is not promoted to newest.
- Backstop: `afterStream` calls `clearWorkflowPhase()` -- pair every `setWorkflowPhase` with a `clearWorkflowPhase` in a `finally`, but stream-end is forgiving.

### 13.2 Reasoning rail

`registerWorkflowPipeline({id, label?, passes:[{id, label?}, ...]})` declares a Secondary-tab rail. Each pass `id` must start with `<wid>:`, contain no second colon, and not be a reserved built-in (`director`/`writer`/`editor`); `registerWorkflowPipeline` (`state.js:102`) rejects the whole pipeline if any pass violates this. The check accepts an empty trailing segment (`"tts:"`), so name the pass segment non-empty by convention.

The router (`chat.js:2256`) finds the pipeline whose `passes` contains `data.pass`, then:

- Matched pass: the delta accumulates in `S.reasoningByPass[passKey]` regardless of which tab is open.
- Live paint happens only when the Inspector Secondary tab is open (`S.inspectorTab === "secondary"`) AND this pass is the one selected in the rail -- the box `#reasoning-box-<pipelineId>` carries the selected pass as `data-pass-id`, and the router paints only on a match.
- Otherwise the text accumulates silently; `renderInspectorSecondary` paints it the next time the tab opens or the pass is selected.

A pass id that matches neither a built-in nor any registered pipeline is dropped with a `console.warn` (`chat.js:2272`).

Emit reasoning from a workflow hook via `forced_tool_call(..., pass_id="<wid>:<pass>")` or yield `{"event": "reasoning", "data": {"pass": "<wid>:<pass>", "delta": "..."}}` directly. Both yield the same event; the orchestrator forwards it to SSE, where the router consumes it.

`selectWorkflowPipelinePass(pipelineId, passId)` (`chat.js:2743`) -- programmatic pass selection; rebuilds the Inspector Secondary content even if that tab is hidden.

### 13.3 Tabs

```
setInspectorTab("main" | "secondary")    # chat.js:2760
setToolsTab("main" | "secondary")        # chat.js:2785
```

Switching to Inspector Secondary triggers `renderInspectorSecondary` (rebuild). Switching to Tools Secondary only toggles visibility.

### 13.4 Refetch helpers

```
refreshConversationMessages(msgId?)   # chat.js:759   async, may return false (in-flight gates)
renderMessages()                       # chat.js:1396  no-arg local repaint
broadcastWorkflowMutation({convId, msgId})   # tabLock.js:27   peer-tab refresh
```

`refreshConversationMessages` returns `false` when there is no active conversation (`S.activeConvId`), while streaming (`S.isStreaming`), while editing (`editingMsgId` / `editingPendingUserMsg` / `magicInputMsgId`), or when `msgId` is one a rehydrate/action/swipe is mid-flight on. `renderMessages` repaints from current `S.messages` (no fetch) -- use after a local config change that affects how renderers paint.

### 13.5 HTTP / DOM helpers

```
api.get(path)                # frontend/api.js:12      prepends /api (via _req, :3)
api.post(path, body)         # :15                     JSON body
api.put(path, body)          # :18                     JSON body
convUrl(...parts)            # frontend/utils.js:48    -> "/conversations/<part1>/<part2>/..."
esc(s)                       # frontend/utils.js:7     HTML-escape; null/undefined -> ""
showModal(html) / closeModal()   # frontend/modal.js:15, :21
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

`_renderWorkflowArtifacts(msg)` (`chat.js:375`) buckets attachments via `_workflowAttachmentGroups(msg)` (`:354`) by `parent_attachment_id` (parent missing -> root), then wraps the groups in `<div class="workflow-artifacts">`. Groups sorted by `rootId`; siblings sorted by id.

Per group, `_renderWorkflowSwipeContainer(msg, rootId, atts)` (`:280-352`) decides branch:

| Branch | Condition | Behavior |
|---|---|---|
| Minimized | `_workflowMinimized.has(rootId)` | Header only; no body; author renderer NOT invoked. |
| Evicted | `_isAttachmentEvicted(active)` (`:174`) -- `(att.b64 || att.data_b64 || "")` equals the `"[evicted]"` sentinel (`:172`) | `_evictedAttachmentHtml(...)` + `actionButtons`. |
| Renderer | `S.workflowAttachmentRenderers[active.workflow_id]` is a function | `renderer(ctx)`. |
| Default | otherwise | `defaultHtml`. |

Active sibling selection: `_activeIndexForGroup` (`:229`, wrapping `_activeAttachmentForGroup` `:218`) -- `root.active_sibling_id` if it matches a sibling, else newest.

### 14.2 Renderer `ctx`

A registered renderer (`S.workflowAttachmentRenderers[workflow_id]`) receives one argument:

```
{
  att: <attachment row>,                              // consumption_metadata already JSON-parsed at load (chat.js:43); null if malformed
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

Inspector Secondary card iteration: `_buildSecondaryAgentsHtml` (`chat.js:2729`). Each `S.workflowInspectorCardRenderers[i]()` output is concatenated raw (no per-card wrap).

Tools Secondary card iteration: `renderToolsPanel` (`settings.js:1124`; secondary-card loop at `:1190`). Same shape, iterating `S.workflowToolsPanelRenderers` (a distinct array from the inspector's `S.workflowInspectorCardRenderers`).

Per-message buttons: `_renderExtraButtons(msg)` (`chat.js:133`). Each `S.workflowMessageButtonRenderers[i](msg)` spliced into the toolbar between magic and delete buttons.

### 14.6 `window.workflow*` handlers (`chat.js`)

Owned by the framework; bound onto the buttons the chrome, nav arrows, and widget bodies emit. The POST-driven handlers hit the per-attachment route family `/conversations/<cid>/messages/<mid>/workflow-attachments/<attId>/<op>` (sec. 8); the table names only the `<op>` segment:

| Handler | Line | Behavior |
|---|---|---|
| `workflowRegenerate(msgId, attId, btn)` | `:538` | tab-lock gate, per-root in-flight lock, set pill, POST `.../regenerate`, merge rejections, refetch + render |
| `workflowReroll(msgId, attId, btn)` | `:576` | same shape, POST `.../reroll-gen` |
| `workflowRehydrate(msgId, attId, btn)` | `:453` | tab-lock gate, per-attId in-flight, POST `.../rehydrate`, refetch + render; 409 treated as already-restored |
| `workflowArtifactStep(instanceId, delta)` | `:403` | sibling nav; optimistic `root.active_sibling_id` update + DOM swap + POST `.../activate` |
| `workflowToggleMinimize(instanceId)` | `:617` | toggles `_workflowMinimized` Set + `localStorage["orb.workflowMinimized"]`; no server |
| `workflowDeleteAttachment(instanceId)` | `:638` | opens the delete-choice modal, then `workflowConfirmDelete(scope)` on confirm. The variant-vs-whole-group choice appears only for a group with >1 sibling; a single-variant group gets a plain confirm |
| `workflowConfirmDelete(scope)` | `:673` | confirm dispatcher |

LocalStorage key: `WF_MINIMIZED_LS_KEY = "orb.workflowMinimized"` (`:259`). Persisted: a collapsed widget stays collapsed across reloads and is shared across same-origin tabs; the in-memory Set is rebuilt per load.

### 14.7 Rejection chips

`_mergeWorkflowRejections(msgId, originatingId, incoming)` (`:532`): drop-then-append by `(msgId, originatingId)` tuple. Empty `incoming` clears that tuple's entries.

| Surface | Trigger | originatingId |
|---|---|---|
| Per-widget chip (filtered + placed in `_renderWorkflowSwipeContainer`, `:305-308`) | regenerate/reroll response | `root_id` |
| Footer chip (`_renderWorkflowRejection`, `:386`) | SSE `workflow_attachments_rejected` | `null` |

Both surfaces emit their HTML through the shared `_workflowRejectionChipHtml` (`:240`), which renders `<div class="workflow-rejected-warning">...</div>`.

### 14.8 Access reporting client

- IntersectionObserver `_workflowViewportObserver` (`chat.js:1528`, re-attached per render by `_refreshWorkflowViewportObserver` `:1566`). Threshold `0.1`. On first entry of a message (deduped per session via `_workflowObservedMsgIds`, declared `:1508`): queues one active-sibling id per group into `_workflowViewportPendingIds` (`:1539`).
- Swipe success also queues the new active sibling id (`:433`).
- Debounce `_scheduleWorkflowViewportFlush` (`:1549`): 250ms `setTimeout` -> `_flushWorkflowViewportReport` (`:1554`) POSTs `{ids: [...]}` to `/conversations/<cid>/workflow-attachments/access`.
- IDs are sent in Set insertion order (`[..._workflowViewportPendingIds]`); the backend assigns access counters in that order (sec. 9).
- Conversation switch resets the observed-message set, pending set, and timer (`chat.js:1031-1036`).

### 14.9 Evicted card

`_evictedAttachmentHtml(msg, att)` (`chat.js:179`) renders filename label + Rehydrate button (or "Bytes evicted" disabled span if `att.seed` is missing). Onclick targets `window.workflowRehydrate(msg.id, att.id, this)`. Multi-tab gating disables the button.

---

## 15. Audio system (`frontend/audio_player.js`, `audio_schedule.js`, `audio_transport.js`)

### 15.1 `playAudio({channel, segments, loop?, volume?, stopOn?})` (`audio_player.js:329`)

Returns `{channel, stop(), isActive()}`. Channels mix; replaying a channel replaces only that channel (last-write-wins per channel, enforced by monotonic token).

| Field | Rule |
|---|---|
| `channel` | required non-empty string; bad/missing -> no-op stub session |
| `segments` | array of segments (see below); each `normalizeSegment` malformed entry skipped with WARNING |
| `loop` | default `false`; runtime override via `setChannelRepeat` |
| `volume` | clamped to `[0, 1]` (non-finite -> 1); sticky per channel |
| `stopOn` | `{newTurn?, convSwitch?}` stored on the channel; omitted keys default to `true` at turn/conv teardown |

### 15.2 Segment shapes (`audio_schedule.js:39`)

Exactly one of `row` / `b64` / `silence` per entry:

| Field | Meaning |
|---|---|
| `seg.row` | attachment row id; bytes read live from `S.messages` via `_findAttachment` (`audio_player.js:144`); evicted rows skipped (no auto-rehydrate) |
| `seg.b64` | inline base64; optional `seg.mime` (carried through, NOT used by decoder -- Web Audio sniffs format) |
| `seg.silence` | seconds; `<=0` or non-finite drops; `>600` clamps to 600 |
| `seg.start` | default 0; negative drops |
| `seg.end` | default = clip end (null sentinel) |

### 15.3 Per-channel controls

```
stopChannel(channel, reason="skipped")    # :379
stopAll()                                  # :393
pauseChannel(channel)                      # :455
resumeChannel(channel)                     # :468
seekChannel(channel, offsetSec)            # :491
setChannelVolume(channel, vol)             # :400
setChannelRepeat(channel, on)              # :532
replayChannel(channel)                     # :573
channelState(channel)                      # :426    null if never played / hard-stopped
onChannel(channel, handler)                # :590    returns unsubscribe
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

The chat render path wraps words in `.seg` spans (`segmentBody`, `workflow_segmentation.js:100`) and tags the claimed ones (`markClickable`) via `_applyWorkflowTextSegments` (`chat.js:1475`). Both entry points require the same two things: at least one of `S.workflowTextEffects` / `S.workflowClickHandlers` is non-empty, and the body is not in editor-diff review. The two entry points:

- After streaming completes, in place on the new message: `finalizeStreamingDiv` (`chat.js:841`, gate at `:855`).
- Full re-render: `_segmentRenderedMessages` (`chat.js:1480`).

Only finalized messages with a positive-integer `data-msg-id` are segmented (`chat.js:1482-1484`); pending and streaming rows lack one until finalized.

### 16.2 Segmentation produces

Each `.seg` span:

- `class="seg"`
- `data-seg="<wordIndex>"`
- `data-sent="<sentIndex>"`

Words split across inline markup share the same `data-seg` (coalesced at read time).

### 16.3 `messageSegments(msgId)` (`workflow_segmentation.js:175`)

Returns ordered `[{wordIndex, sentIndex, word}]`. `word` text coalesces multiple `.seg` fragments sharing the same `data-seg`. Empty array when the message body isn't in DOM yet.

### 16.4 `segDescriptor` (`workflow_segmentation.js:137`)

Passed to `claims(seg)` and `onClick(seg, msgId)`:

| Field | Source |
|---|---|
| `wordIndex` | `Number(span.dataset.seg)` |
| `sentIndex` | `Number(span.dataset.sent)` |
| `word` | lazy getter; concatenates `textContent` of all spans sharing `data-seg` |
| `sentenceText` | lazy getter; concatenates spans sharing `data-sent` |
| `msgId` | merged in via `extra`; the click router reads it from the closest `.message[data-msg-id]` (`workflow_text_interaction.js:48-54`), the render-time claim pass `markClickable` passes the message id it already holds (`:62`) |
| `role` | merged in via `extra`; `"user"`/`"assistant"` (`workflow_text_interaction.js:52-54`, `:62`) |

### 16.5 `startTextEffect({msgId, effectId, grain?, variant?})` (`workflow_text_effects.js:18`)

Returns `{markActive(unitIndex), stop()}` -- hold this handle and drive `markActive` from your own events (e.g. audio time updates). Global single session: starting a new one supersedes the prior, after which the old handle's `markActive` no-ops via an internal token check.

| Param | Default | Allowed |
|---|---|---|
| `grain` | `"word"` | `"word"`, `"sentence"` |
| `variant` | `"highlight"` | `"highlight"`, `"underline"`, `"pulse"` (unknown -> highlight + `console.error`) |

Painter applies CSS class `"fx-" + variant` to `.seg[data-seg=<idx>]` (word grain) or `.seg[data-sent=<idx>]` (sentence grain).

`clearTextEffect()` (`:39`) -- tears down the global session.

### 16.6 `registerClickHandler({id, label?, priority?, claims?, onClick})` (`state.js:156`)

`priority` (default 0) breaks contention when several workflows claim one word -- higher wins, registration order on ties. The sort happens at click time in `_claimantsFor` (`workflow_text_interaction.js:36`), not at registration. `claims(seg)` decides which words the handler wants (default: all). `onClick(seg, msgId)` runs on click.

### 16.7 Click router (`workflow_text_interaction.js`)

Delegated `click` listener on `#chat-messages`. Steps:

1. Resolve target `.seg.seg-clickable`.
2. Build `segDescriptor`.
3. `_claimantsFor(ctx)` runs each `S.workflowClickHandlers[*].claims(ctx)` (throwing claims logged + skipped), sorts by priority descending.
4. Fire:
   - One claimant: a plain click fires its `onClick`.
   - Multiple claimants (`.seg-multi`): a plain click fires the top-priority claimant. To pick another, the user opens a chooser listing every claimant in priority order:
     - Desktop: a caret revealed on hover (`:158`), clicked to open the chooser.
     - Touch: a long-press (`:93`), which swallows the synthetic click so the top claimant does not also fire.

### 16.8 CSS classes

| Class | Source |
|---|---|
| `.seg` | `workflow_segmentation.js:86` (structural marker; styled only via `.seg.<modifier>` compounds) |
| `.seg-clickable` | `workflow_text_interaction.js:65` (added to any claimed word) |
| `.seg-multi` | `:66` (added to words with >1 claimant) |
| `.fx-highlight` / `.fx-underline` / `.fx-pulse` | `workflow_text_effects.js:52` toggle |
| `.wf-seg-caret` | `workflow_text_interaction.js:121` (hover chooser button) |
| `.wf-claim-popover` / `.wf-claim-item` | `:174`, `:178` |

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

1. Create `frontend/workflows/<id>/index.js`. Top-level imports and registry pushes run on import.
2. Push renderers into `S.workflowInspectorCardRenderers` / `S.workflowToolsPanelRenderers` / `S.workflowMessageButtonRenderers` as needed.
3. Assign your attachment renderer to `S.workflowAttachmentRenderers["<id>"]` if you produce artifacts.
4. Assign custom SSE handlers to `S.workflowEventHandlers["<custom_event>"]` for non-reserved events the backend hook yields.
5. If your backend hook emits `reasoning` with a pipeline pass id, call `registerWorkflowPipeline({id: "<wid>", passes: [{id: "<wid>:<passname>"}]})`.
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
| Add a new hook type | `contracts.py:225` + `registry.py:136` + matching dispatch: `iter_subscriptions` in `orchestrator.py` (fan-out hooks) or `get_subscription` in `main.py` (single-dispatch hooks) |
| Custom SSE event from backend to frontend | yield non-reserved name from hook -> `S.workflowEventHandlers["name"]` -- sec. 12.2, 12.3 |
| Drive in-turn status text | yield `phase_status` with `channel: "workflow:<id>"` -- sec. 12.2, 13.1 |
| Out-of-band status text | `setWorkflowPhase("workflow:<id>:...", label)` then `clearWorkflowPhase` in finally -- sec. 13.1 |
| Force a single tool call from a hook | `forced_tool_call(...)` -- sec. 6.4 |
| Author-side LLM client | `ctx.client` (PreCtx/PostCtx/OnDemandCtx/RegenCtx/RerollGenCtx) -- sec. 4 |
| Add a Tools-panel card | push into `S.workflowToolsPanelRenderers` (top-level in the workflow's `index.js`) -- sec. 11.3 |
| Karaoke-style text highlighting | `playAudio` + `channelState` polling + `startTextEffect(...).markActive` -- sec. 15.4, 16.5 |
| Render a custom widget for own attachments | `S.workflowAttachmentRenderers[wid] = (ctx) => htmlString` -- sec. 11.3, 14.2 |
| Read evicted attachment | not allowed; surface Rehydrate button or read `att.consumption_metadata` only -- sec. 9.2, 14.9 |
| Force cross-tab refresh after an out-of-band mutation | `broadcastWorkflowMutation({convId, msgId})` after the response -- sec. 13.4 |
