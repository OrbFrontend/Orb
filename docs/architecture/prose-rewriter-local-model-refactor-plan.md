# Prose Rewriter / Local Model Separation Refactor Plan

- Status: ready for implementation
- Scope: backend architecture only
- Schema changes: none
- HTTP/SSE contract changes: none
- Frontend changes: none

## 1. Decision

Refactor the prose rewriter into a feature slice that consumes shared local-model
infrastructure:

~~~text
api ───────────────┐
                   ├──> features/prose_rewriter ───> inference/local_models
pipeline/editor ───┘

workflows/image_gen ───────────────────────────────> inference/local_ml
~~~

This is the right dependency direction for Orb. The feature owns the prose
rewriter's product behavior; inference owns model artifacts and execution
mechanics.

Do not introduce one universal LocalLLM base class. Orb has three different
capability shapes already:

- raw in-process text completion;
- in-process embedding/classification;
- continuously batched generation through a supervised llama-server child.

They share assets and some lifecycle infrastructure, but not one useful
inference call. Use composition, immutable configuration records, and small
capability protocols instead of inheritance.

## 2. Findings confirmed by review

### 2.1 The current code is not all in inference

The turn placement and safe-failure adapter already live in
`backend/pipeline/passes/editor/slm_rewrite.py`. On-demand locking, SSE
translation, and persistence live in `backend/api/routes/messages.py`. Those are
appropriate integration boundaries and should remain in their layers.

The mixed part is the implementation beneath them:

- `backend/inference/prose_rewriter/text.py` contains the trained prompt contract
  and corpus-specific repairs;
- `backend/inference/prose_rewriter/rewrite.py` contains paragraph planning,
  budgets, progress ordering, and rewrite policy;
- `backend/inference/prose_rewriter/server.py` combines reusable process/HTTP/host
  mechanics with prose-specific launch policy;
- `backend/inference/prose_rewriter/runtime.py` is mostly a reusable llama-server
  binary manager;
- `backend/inference/local_ml.py` owns shared artifact plumbing while importing the
  prose rewriter's `Variant` type.

### 2.2 The circular catalog ownership is the first seam to fix

`backend/inference/local_ml.py` imports `Variant` from the prose rewriter, while
`backend/inference/prose_rewriter/catalog.py` performs deferred imports back into
`local_ml` (`MODELS` in `variants()`, `model_dir` in `variant_path()`). The
deferred imports make the cycle loadable; they do not make the ownership sound.

The generic artifact and variant contracts must move below both callers before
any feature files move.

### 2.3 The API management route is a second mixed boundary

`backend/api/routes/local_ml.py` is generic in name but directly implements prose
selection repair (`_sync_selection`), prewarming (`_prewarm`, `_BACKGROUND`,
`_spawn`), host release, runtime download, batch validation, and status
enrichment. These behaviors should be exposed by a prose-rewriter feature service
and composed by the API.

The generic route should continue to own generic HTTP concerns such as validating
a feature id and serializing common artifact status. A feature-specific router
may keep the existing prose-rewriter URLs while delegating to the feature.

### 2.4 The shared event service belongs to the feature

The on-demand API currently imports `ProseRewrite`, `resolve_prose_rewrite`, and
`prose_rewrite_step` from a pipeline editor module. That dependency is legal, but
it makes an on-demand feature operation depend on pipeline internals.

The resolved config contract, failure-to-no-op behavior, progress queue, and
rewrite event stream are common to both callers. Move them to
`features/prose_rewriter/`. Keep only turn placement in the pipeline.

### 2.5 Do not generalize the refactor to all Local ML features

The POV classifier is consumed by `workflows/image_gen`, and `workflows` is below
`features` in Orb's layer stack. Moving every model-specific adapter into
`features` would force a lower layer to import upward.

This plan therefore moves only prose-rewriter product behavior. Existing
autocomplete and classifier call surfaces remain available from
`inference/local_ml.py` — `available`, `complete`, `build_prompt`, `ascore`,
`aclassify`, `aclassify_pov`, `pov_input`, `pov_from_logits`, `POV_ROWS`,
`GO_EMOTIONS` — because `workflows/toolkit.py` re-exports the module as the
workflow author's API and `features/cards/expressions.py` imports `GO_EMOTIONS`
from it.

### 2.6 Model pruning makes lazy registration unsafe

`local_ml.prune_stale` removes every GGUF whose basename is not claimed by the
complete model catalog. If feature model specs were registered only when their
feature happened to be imported, downloading an unrelated model could treat an
unregistered prose checkpoint as stale and delete it.

Keep a deterministic, complete built-in artifact manifest in the shared
local-model infrastructure. The manifest may contain the prose model artifacts
and runtime kind; prompt behavior, selection policy, and launch tuning remain in
the feature.

If registration is introduced in the future, it must have an explicit
register-then-seal bootstrap, and destructive asset operations must refuse to run
before the registry is sealed. That mechanism is unnecessary for this refactor.

### 2.7 Share a host implementation, not one resident host

A llama-server process has one model loaded. A single global host shared by
several future features would make one feature drain and restart another's
resident model.

The shared layer should provide `ManagedLlamaServerHost`. The prose feature owns
its `HOST` instance. A runtime-level manager tracks all host instances only for
operations that affect the shared binary:

- application shutdown;
- replacing the downloaded llama-server build;
- an explicit release-all operation.

Storage accounting reads the binary directory directly and needs no host.

### 2.8 Confirmed properties of the tree this plan lands in

Verified against the working tree before writing the phases:

- `pipeline/` already imports `features/` in six modules (`context`,
  `persistence`, `state`, `sheet_update`, `world_proposal`,
  `passes/world_change`). A pipeline module importing the prose feature is
  established practice, not a new edge. AGENTS.md lists `pipeline/` and
  `features/` on one line of the layer stack; the codebase's real rule is
  *pipeline may import features, features never import pipeline, slices never
  import peers*. Phase 5 fixes that wording.
- No feature slice imports another slice, `pipeline/`, `workflows/`, or `api/`
  today. The full package edge set is downward-only, which is what makes the
  Phase 5 backend layer checker a zero-fix addition.
- There is no backend import-direction checker. `scripts/lint.sh` runs Ruff,
  Pyright, `scripts/check_frontend_layers.py` and the frontend node tests only.
- `inference/__init__.py` does not re-export `prose_rewriter` or `local_ml`;
  every caller imports the submodule (`from ...inference import local_ml`). No
  facade edit is needed there.
- `docs/features/prose-rewriter.md` names no Python module paths, so the only
  doc it needs is the `backend/data/llama-bin/` sentence, which does not move.

## 3. Target package layout

~~~text
backend/
├── inference/
│   ├── local_ml.py                    # in-process calls + compatibility re-exports
│   └── local_models/
│       ├── __init__.py                # facade; owns available(feature)
│       ├── catalog.py                 # ModelSpec, ModelVariantSpec, RuntimeKind, MODELS
│       ├── assets.py                  # paths, presence, download, delete, prune
│       ├── dependencies.py            # per-runtime dependency checks + install hint
│       └── llama_server/
│           ├── __init__.py            # facade for the four modules below
│           ├── binary.py              # find/fetch/pin/probe flags/bin_bytes
│           ├── process.py             # Child protocol, async child, threaded child
│           ├── client.py              # argv, health/tokenize/completion transport
│           ├── host.py                # LaunchProfile + load/swap/drain/release/idle
│           └── manager.py             # registered hosts, release_all, shutdown_all
├── features/
│   └── prose_rewriter/
│       ├── __init__.py                # public feature facade
│       ├── catalog.py                 # variant view and selection
│       ├── config.py                  # persisted -> resolved config -> LaunchProfile
│       ├── text.py                    # exact prompt, stop token, repairs
│       ├── rewrite.py                 # paragraph rewrite algorithm
│       ├── service.py                 # rewrite events, readiness, feature-owned HOST
│       └── integration.py             # settings persistence, selection repair, prewarm
├── pipeline/
│   └── passes/editor/editor.py        # turn placement (see §7.4 on slm_rewrite.py)
└── api/
    └── routes/
        ├── local_ml.py                # common Local ML endpoints + status composition
        └── prose_rewriter.py          # the one feature-named URL: the runtime fetch (§7.3)
~~~

The ownership boundaries are required; the filenames are not. Collapse a split
that produces a module with no independent responsibility, and say so in the
commit message.

## 4. Symbol move map

Every symbol currently in the four files being decomposed. "verbatim" means the
body does not change; only the module header, imports, and (where noted) the
name.

### 4.1 `inference/local_ml.py` (713 lines)

| Symbol | Destination | Notes |
|---|---|---|
| `ModelSpec`, `.local_name`, `.all_names` | `local_models/catalog.py` | verbatim |
| `_PROSE_1_7B_REPO/_REV`, `_PROSE_4B_REPO/_REV` | `local_models/catalog.py` | verbatim; pins stay one place |
| `MODELS` | `local_models/catalog.py` | verbatim, prose variants included (§2.6) |
| `_ROOT` | `local_models/assets.py` | **depth +1** — see §8.1 |
| `model_dir`, `resolve_path`, `present`, `prune_stale` | `local_models/assets.py` | verbatim |
| `variant_spec`, `download`, `delete_model` | `local_models/assets.py` | verbatim |
| `_import_llama`, `_shell_quote`, `install_cmd`, `deps_ok` | `local_models/dependencies.py` | `install_cmd` needs the same `_ROOT` fix |
| `available` | `local_models/__init__.py` | the one function spanning assets + dependencies |
| `__main__` prune self-check | `local_models/assets.py` `__main__` | keeps the runnable self-check convention |
| everything else | stays | handles/locks/atexit, `_load_blocking`, `acomplete`, `complete`, `_score_blocking`, `ascore`, `_head_logits`, `aclassify`, `pov_input`, `pov_from_logits`, `aclassify_pov`, `build_prompt`, `GO_EMOTIONS`, `POV_ROWS`, sampling constants, `build_prompt`/POV self-checks |

`local_ml.py` then re-exports, for callers and tests that address it by module:
`MODELS`, `ModelSpec`, `model_dir`, `resolve_path`, `present`, `available`,
`deps_ok`, `install_cmd`, `download`, `delete_model`, `prune_stale`,
`variant_spec`. Add them to an explicit `__all__`.

New generic helper (needed by the status route, currently `prose_rewriter.on_disk`):

~~~python
def variant_present(variant: ModelVariantSpec) -> bool: ...   # assets.py
def variant_path(variant: ModelVariantSpec) -> str: ...       # assets.py
~~~

### 4.2 `inference/prose_rewriter/catalog.py` (76 lines)

| Symbol | Destination | Notes |
|---|---|---|
| `Variant` | `local_models/catalog.py` as `ModelVariantSpec` | verbatim body; **one** name for the type — no alias in the feature |
| `FEATURE` | `features/prose_rewriter/catalog.py` | verbatim |
| `variants()` | `features/prose_rewriter/catalog.py` | deferred import becomes a top-level one |
| `resolve()`, `on_disk()`, `variant_path()` | `features/prose_rewriter/catalog.py` | `variant_path`/`on_disk` delegate to `assets` |

### 4.3 `inference/prose_rewriter/server.py` (729 lines)

| Symbol | Destination | Notes |
|---|---|---|
| `_can_spawn_async`, `_decode`, `Child`, `_AsyncChild`, `_ThreadChild`, `spawn` | `llama_server/process.py` | verbatim; keep the `# noqa: S603` comments |
| `_free_port`, `_error_text` | `llama_server/client.py` | verbatim |
| `_HELP_CACHE`, `_help_text` | `llama_server/binary.py` as `supports_flag(binary, flag)` | keeps the `--help` probe with the binary it describes |
| `LlamaServer` | `llama_server/client.py` as `LlamaServerClient` | see §5.3 for the constructor change |
| `BOOT_TIMEOUT` | `llama_server/client.py` | verbatim |
| `ModelHost` | `llama_server/host.py` as `ManagedLlamaServerHost` | see §5.5 |
| `HOST` | `features/prose_rewriter/service.py` | feature-owned, manager-registered |
| `CTX_PER_SLOT`, `MIN_SLOTS`, `MAX_SLOTS`, `DEFAULT_SLOTS`, `_SLOT_ARGV` | `features/prose_rewriter/config.py` | `_SLOT_ARGV` becomes `SLOT_ALLOCATION` (§5.4) |
| `STOP_TOKEN` | `features/prose_rewriter/text.py` | it is part of the prompt contract, next to `<\|im_start\|>` |
| `IDLE_TIMEOUT` (`ORB_PROSE_REWRITER_IDLE`) | `features/prose_rewriter/config.py` | env name unchanged; passed to the host constructor |
| alias `"prose-rewriter"`, `"Prose rewriter …"` log strings | `features/prose_rewriter/config.py` / `service.py` | the host logs by its own `name` |

### 4.4 `inference/prose_rewriter/runtime.py` (284 lines)

Moves to `llama_server/binary.py` essentially whole: `LlamaServerMissing`,
`IS_WINDOWS`, `EXE`, `BINARY_NAME`, `DEFAULT_BUILD`, `REPO_SLUG`, `USER_AGENT`,
`_BUILD_TAG`, `bin_dir`, `_executable`, `_named`, `find_binary`, `runtime_ok`,
`_arch`, `asset_name`, `_system`, `_api`, `resolve_release`, `_unpack`,
`_flatten`, `fetch`, `bin_bytes`.

Two edits only: `_ROOT` depth (§8.1), and `IS_WINDOWS` gains a second importer
(`process.py`), which imports it from `binary.py` rather than recomputing it.

### 4.5 `inference/prose_rewriter/rewrite.py`, `text.py`, `__init__.py`

| Symbol | Destination | Notes |
|---|---|---|
| all of `text.py` | `features/prose_rewriter/text.py` | **byte-identical bodies**; move first, alone, in its own commit |
| `TEMPERATURE`, `TOP_P`, `budget`, `assemble`, `_admissible` | `features/prose_rewriter/rewrite.py` | verbatim |
| `arewrite` | `features/prose_rewriter/rewrite.py` | signature change (§5.6) |
| `available` | `features/prose_rewriter/service.py` | verbatim |
| `select_batch_size`, `resolve_batch_size`, `_BATCH_SIZE_ALLOWLIST`, `*_BATCH_SIZE` | `features/prose_rewriter/config.py` | verbatim |
| `shutdown`, `state` | `features/prose_rewriter/service.py` | `shutdown` is superseded by `manager.shutdown_all` for the lifespan, but stays for symmetry with `state()` |

### 4.6 `pipeline/passes/editor/slm_rewrite.py` (135 lines)

| Symbol | Destination | Notes |
|---|---|---|
| `ProseRewrite` | `features/prose_rewriter/config.py` as `ProseRewriteConfig` | TypedDict shape unchanged |
| `resolve_prose_rewrite` | `features/prose_rewriter/config.py` as `resolve_config` | verbatim |
| `_DONE`, `prose_rewrite_step` | `features/prose_rewriter/service.py` as `rewrite_events` | verbatim |
| `FEATURE` | already in the feature catalog | drop the re-export |
| module docstring (pre-audit ordering) | `pipeline/passes/editor/editor.py` | the ordering lives where the call is |

## 5. Shared contracts

### 5.1 Artifact catalog — `local_models/catalog.py`

~~~python
RuntimeKind = Literal["llama_cpp", "llama_server"]

@dataclass(frozen=True)
class ModelVariantSpec:
    id: str
    label: str          # presentation: the Local ML panel renders these for ANY
    detail: str         # variant-bearing feature, so they are generic, not prose-only
    repo_id: str
    path: str           # path inside the HF repo
    revision: str       # pinned commit sha
    size_mb: int
    @property
    def local_name(self) -> str: ...   # flat basename under data/models/

@dataclass(frozen=True)
class ModelSpec:
    repo_id: str
    filename: str
    size_mb: int
    revision: str
    runtime: RuntimeKind = "llama_cpp"
    variants: tuple[ModelVariantSpec, ...] = ()

MODELS: dict[str, ModelSpec]           # complete at import time
~~~

The manifest stays complete at import so `prune_stale` always sees every claimed
basename. Preserve the invariant that every basename is claimed exactly once.

Prose-only tuning (slot allocation, alias, idle timeout, prompt tokens) must not
move into the shared catalog merely to avoid one feature import.

`runtime` becomes a `Literal` rather than a bare `str`. Pyright then rejects a
typo at the definition site; `deps_ok`'s `if runtime == "llama_cpp"` branch is
unchanged.

### 5.2 Asset store — `local_models/assets.py`

Owns `model_dir`, `resolve_path`, `present`, `variant_present`, `variant_path`,
`variant_spec`, `download`, `delete_model`, `prune_stale`.

Asset functions accept a trusted `ModelSpec`/`ModelVariantSpec` or a feature id
resolved through the closed catalog, never a raw request-derived path. The API
resolves ids before calling them, exactly as it does today.

### 5.3 Generic llama-server client — `llama_server/client.py`

~~~python
class LlamaServerClient:
    def __init__(self, profile: LaunchProfile, binary: Path) -> None: ...
    slots: int                     # = profile.parallel; rewrite.py sizes its semaphore from this
    async def start(self) -> None: ...
    async def wait_ready(self, timeout: float = BOOT_TIMEOUT) -> None: ...
    async def stop(self) -> None: ...
    def tail(self, n: int = 12) -> str: ...
    @property
    def alive(self) -> bool: ...
    async def count_tokens(self, text: str) -> int: ...
    async def generate(
        self, prompt: str, *, n_predict: int, temperature: float, top_p: float,
        stop: Sequence[str] = (), cache_prompt: bool = True,
    ) -> tuple[str, bool]: ...
~~~

The stop sequence becomes a request parameter. `<|im_end|>` belongs to the prose
checkpoints, not to llama-server. `"stop"` is included in the JSON body only when
`stop` is non-empty, so a caller with no stop sequence produces the body a
stop-less client would have sent; with `stop=("<|im_end|>",)` the body is
byte-identical to today's.

The client continues to own loopback-only ephemeral-port HTTP, health polling,
tokenize/completion parsing, SSE cancellation by closing the request, the
old/new `stop_type` vs `stopped_eos`/`stopped_word` compatibility, and bounded
boot diagnostics drained from the child before the failure is reported.

Argv assembly moves here from `LlamaServer.__init__` and reads only the profile:

~~~python
def _argv(profile: LaunchProfile, binary: Path, port: int) -> list[str]
~~~

The registry check that `LlamaServer.__init__` performs today
(`catalog.resolve(variant.id)`) does **not** move here — the client cannot import
a feature catalog. It moves up to `features/prose_rewriter/config.launch_profile`,
which is the only constructor of a prose profile. See §8.3.

### 5.4 Launch profile — `llama_server/host.py`

~~~python
@dataclass(frozen=True)
class LaunchProfile:
    model_id: str        # opaque load-key identity (the variant id, for the prose feature)
    model_path: str      # trusted absolute path, resolved by the caller's closed catalog
    alias: str
    gpu_layers: int      # 999 | 0 — an int chosen by the feature, never a settings string
    ctx_size: int
    parallel: int
    http_threads: int
    cont_batching: bool = True
    no_webui: bool = True     # asked for only if binary.supports_flag says so
    label: str = ""           # log text
    size_mb: int = 0          # log text

    def __post_init__(self) -> None:
        # Every argv-bound number is an int owned by this process, not a value
        # that arrived over HTTP. type() is exact on purpose: bool is an int.
        for value in (self.gpu_layers, self.ctx_size, self.parallel, self.http_threads):
            if type(value) is not int:
                raise TypeError("launch profile numbers must be code-owned ints")
~~~

Persisted or request values must first resolve through a closed feature-owned
allowlist. No raw string or unchecked integer from settings may reach argv. This
preserves the current command-injection barrier.

The prose feature owns the batch-size mapping because those values follow the
rewriter's 512-token training envelope and throughput policy:

~~~python
# features/prose_rewriter/config.py — literal, code-owned, closed.
SLOT_ALLOCATION: dict[int, tuple[int, int, int]] = {
    1: (1280, 1, 6),
    2: (2560, 2, 8),
    3: (3840, 3, 10),
    4: (5120, 4, 12),
}
~~~

Same barrier as today's `_SLOT_ARGV`: the request-derived value is used only as a
dict key, and what comes back is a code-owned literal. The literals are ints
rather than strings now, and `client._argv` calls `str()` on them; the tainted
value still never reaches the sink.

Every field of a prose `LaunchProfile` is a pure function of
`(variant, gpu, batch_size)`, so dataclass equality is exactly today's
`variant.id == … and gpu == … and slots == …` comparison. That is what makes it a
sound load key.

### 5.5 Managed host — `llama_server/host.py`

~~~python
class ManagedLlamaServerHost:
    # Registers itself with the manager unless told not to. Default-on because
    # the failure mode of a forgotten register() is the one this subsystem's
    # comments warn about three times: an orphaned child holding the GPU after
    # Orb exits. Tests that build a throwaway host pass register=False.
    def __init__(self, *, name: str, idle_timeout: float, register: bool = True) -> None: ...
    state: str            # idle | loading | ready | failed
    error: str
    profile: LaunchProfile | None
    def mark_stale(self, profile: LaunchProfile | None) -> None: ...
    @property
    def healthy(self) -> bool: ...
    async def ensure(self, profile: LaunchProfile) -> LlamaServerClient: ...
    def use(self, profile: LaunchProfile) -> AbstractAsyncContextManager[LlamaServerClient]: ...
    async def release(self) -> None: ...
    async def shutdown(self) -> None: ...
~~~

Behavior is today's `ModelHost`, with three edits:

1. `(variant, gpu, slots)` collapses into one `LaunchProfile`; comparisons become
   `self.profile == profile`.
2. The `MIN_SLOTS <= slots <= MAX_SLOTS` guards in `__init__`, `ensure` and `use`
   are deleted. The generic host has no opinion about slot counts; the feature's
   profile builder is where an unsupported batch size is rejected, and the test
   for it moves with the rule (§6).
3. `IDLE_TIMEOUT` and the two `"Prose rewriter …"` log strings become `self.name`
   and `self._idle_timeout`, both constructor arguments.

Everything else is preserved verbatim, and each of these is separately tested
today: exactly one concurrent load/swap; in-flight accounting that does not
serialize generation; `use()` raising the in-flight count *before* releasing the
swap lock; drain-before-replace; `state = "loading"` set before the drain;
mark-stale; idle unload; the `release()` drain that keeps `state == "ready"` until
the child is actually gone.

It must not import a feature module. Compare profiles, not catalog types.

### 5.6 Host manager — `llama_server/manager.py`

~~~python
def register(host: ManagedLlamaServerHost) -> ManagedLlamaServerHost: ...
def unregister(host: ManagedLlamaServerHost) -> None: ...   # tests
def hosts() -> tuple[ManagedLlamaServerHost, ...]: ...
async def release_all() -> None: ...     # every host lets go of files; reloads lazily
async def shutdown_all() -> None: ...    # every child stopped; app teardown
~~~

A plain list, not a `WeakSet`: hosts are module-level singletons that must not be
collectable while a child is running. `register` is idempotent on identity.

`release_all`/`shutdown_all` are no-ops on an empty registry, run the hosts
**concurrently**, and never let one host's failure skip the rest — `gather(...,
return_exceptions=True)` with each exception logged. Concurrency is not
premature: `release()` drains with a 120 s ceiling, and serializing N of those
would put N × 120 s between the user pressing Fetch and the binary being
replaced, or between SIGINT and the process exiting.

Registration happens when `features/prose_rewriter/service.py` is imported. In a
running app that is guaranteed by `api/routes/local_ml.py` importing the feature
at module import time; nothing that can spawn a child can do so without going
through the service first.

## 6. Feature contracts

### 6.1 `features/prose_rewriter/config.py`

~~~python
class ProseRewriteConfig(TypedDict):
    variant_id: str
    gpu: bool
    batch_size: int

MIN_BATCH_SIZE, MAX_BATCH_SIZE, DEFAULT_BATCH_SIZE: int
def select_batch_size(value: object) -> int | None
def resolve_batch_size(value: object) -> int
def resolve_config(settings: Mapping[str, Any]) -> ProseRewriteConfig | None
def launch_profile(config: ProseRewriteConfig) -> LaunchProfile
def launch_profile_for(variant: ModelVariantSpec, gpu: bool, batch_size: int) -> LaunchProfile

class UnknownVariant(ValueError): ...        # the API maps these to 404 / 400;
class UnsupportedBatchSize(ValueError): ...  # the slice never imports FastAPI
~~~

`launch_profile_for` is the trust barrier and the only constructor of a prose
`LaunchProfile`. It:

1. re-resolves the variant through `features/prose_rewriter/catalog.resolve` and
   raises `ValueError` if the argument is not the registered record (today's
   `LlamaServer.__init__` check, moved intact);
2. rejects a batch size outside `SLOT_ALLOCATION` with the same
   `"slots must be between 1 and 4"` message the host raises today;
3. resolves the trusted model path through `assets.variant_path`;
4. maps `gpu` to `999`/`0`, and fills alias/label/size for the logs.

`launch_profile(config)` is the resolved-config convenience wrapper used by
`service.rewrite_events`.

### 6.2 `features/prose_rewriter/service.py`

~~~python
HOST = manager.register(ManagedLlamaServerHost(name="prose_rewriter", idle_timeout=IDLE_TIMEOUT))

def available(variant_id: str | None) -> bool
def state() -> dict[str, str]                       # {"state": …, "error": …}
async def shutdown() -> None
def rewrite_events(draft: str, config: ProseRewriteConfig) -> AsyncGenerator[dict, None]
~~~

`rewrite_events` owns the common safe-failure contract, unchanged from
`prose_rewrite_step`:

- ordered whole-draft progress snapshots (`draft_update`);
- one `warning` plus the original draft on any failure;
- cancellation of all sibling paragraph tasks when the generator is abandoned;
- exactly one terminal `rewritten` event.

The pipeline and the on-demand API both consume this, so neither reimplements
failure or progress behavior.

### 6.3 `features/prose_rewriter/integration.py`

The database-aware management surface, moved out of `api/routes/local_ml.py`:

~~~python
async def status_extra(settings: Mapping[str, Any]) -> dict   # selected/gpu/batch_size/runtime_ok/state/error
async def sync_selection(*, prefer: str | None = None) -> dict  # returns the whole local_ml_config blob
async def apply_config(variant_id: str | None, gpu: bool, batch_size: int) -> dict
async def on_enabled(enabled: bool) -> None                     # prewarm on enable
async def release_host() -> None                                # before a delete or a binary replace
async def fetch_runtime(backend: str) -> str                    # release_all, then binary.fetch
~~~

It also owns `_BACKGROUND`/`_spawn`/`_prewarm` — the strong task references that
keep a fire-and-forget prewarm from being collected mid-load.

Keeping management integration separate prevents `service.py` from becoming a new
feature-level god module. Pure rewrite calls need no database dependency, and
`rewrite_events` must stay importable without one.

### 6.4 `features/prose_rewriter/__init__.py`

Facade re-exporting, in the Standard Slice Shape: `FEATURE`, `HOST`,
`ProseRewriteConfig`, `resolve_config`, `rewrite_events`, `available`, `state`,
`shutdown`, `resolve`, `on_disk`, `variants`, `select_batch_size`,
`resolve_batch_size`, `MIN_BATCH_SIZE`, `MAX_BATCH_SIZE`, `DEFAULT_BATCH_SIZE`,
and `integration` as a module.

## 7. API and caller composition

Keep all existing URLs and response fields.

### 7.1 Status payload split

`/api/local-ml/status` keeps the exact object it returns today. The generic route
builds everything the shared catalog can answer; the controller supplies the rest.

| Key | Owner |
|---|---|
| `deps_ok`, `reason`, `install_cmd` (top level) | generic |
| `present`, `enabled`, `size_mb`, `deps_ok`, `reason`, `runtime` | generic |
| `variants[].{id,label,detail,size_mb,present}` | generic (`assets.variant_present`) |
| `runtime_ok` | generic, keyed on `spec.runtime == "llama_server"` — it is a fact about the shared binary, not about the rewriter |
| `selected`, `gpu`, `batch_size`, `state`, `error` | `integration.status_extra` |

Emitting `runtime_ok` from the generic route is byte-compatible today: the only
`llama_server` feature is also the only variant-bearing one, so the key appears
on exactly the same object it appears on now. `batch_size` must default to
`resolve_batch_size({})` — `4` — when nothing is configured, which
`test_status_enumerates_the_rewriter_variants` asserts on a fresh install.

The frontend reads exactly these (`frontend/settings.js`: `st.deps_ok`,
`st.install_cmd`, `info.variants`, `info.runtime_ok`, `info.selected`,
`info.batch_size`, `info.state`, `info.error`, `v.present`). A contract test pins
the union.

### 7.2 Controller map

~~~python
# api/routes/local_ml.py — composition in the top layer, not a callback downward.
class _FeatureManagement(Protocol):
    async def status_extra(self, settings: Mapping[str, Any]) -> dict: ...
    async def sync_selection(self, *, prefer: str | None = None) -> dict: ...
    async def apply_config(self, body: Mapping[str, Any]) -> dict: ...
    async def on_enabled(self, enabled: bool) -> None: ...
    async def release_host(self) -> None: ...

_MANAGEMENT: dict[str, _FeatureManagement] = {prose_rewriter.FEATURE: prose_rewriter.integration}
~~~

The shared catalog describes artifacts; the API controller describes feature
behavior. Do not put feature lifecycle hooks on `ModelSpec` — that would turn the
inference catalog into a registry of higher-layer behavior and recreate the
dependency problem in a less visible form.

**Status codes stay in the API; validation stays in the feature.** The config
route answers 404 for an unknown variant and 400 for a bad batch size, and a
feature module must not import FastAPI to say so. `features/prose_rewriter/config.py`
therefore defines two small errors and the route owns the mapping:

~~~python
class UnknownVariant(ValueError): ...        # -> 404
class UnsupportedBatchSize(ValueError): ...  # -> 400
~~~

`apply_config` raises them with today's exact messages (§8.9) and the route
re-raises as `HTTPException(404|400, detail=str(exc))`. `feature not in
_MANAGEMENT` is the generic 404 that replaces today's `if not spec.variants`,
and it keeps that branch's wording.

### 7.3 Route ownership

**Revised from the first draft.** `POST /api/local-ml/{feature}/config` stays
generic and delegates to the controller; `api/routes/prose_rewriter.py` owns only
the one URL that is literally feature-named:

- `POST /api/local-ml/prose_rewriter/runtime` → `integration.fetch_runtime`

The config endpoint is *already specified* as generic — AGENTS.md documents it as
"per-feature JSON blob" — so the generic route validating the feature id and
handing an opaque body to `controller.apply_config` is the contract, not a
compromise. Splitting it out instead would cost three things for nothing:

- two live URL patterns for one path, resolved by router registration order,
  which §5 of the first draft correctly called a hazard;
- the 404 body for `POST /api/local-ml/autocomplete/config` and
  `/api/local-ml/nope/config`, which today read
  `{"detail": "Unknown local-ML feature 'nope'"}` / `"'autocomplete' has no
  configurable variants"` and would silently become Starlette's
  `{"detail": "Not Found"}`. Two integration tests assert only the status code,
  so this regression would ship green;
- the `_download_lock`, which currently guards the model download and the
  runtime fetch as one lock and would have to be hoisted to `api/deps.py` to
  span two routers.

With config staying generic, only the runtime route moves, and it takes the lock
question with it: `api/routes/prose_rewriter.py` imports `_download_lock` from
`api/deps.py` (where the other cross-route locks live) and `local_ml.py` does
too. One lock, two routers, no shared-mutable-state-in-a-route-module.

Register `prose_rewriter.router` **before** `local_ml.router` in `ROUTERS`
anyway: the paths do not collide today, and an exact path in front of a
`{feature}` pattern is the ordering that stays correct if one ever does.

`download`, `delete`, `enabled`, `config`, `status`, `slop-score` and
`classify-emotion` stay in `local_ml.py`. `download`/`delete`/`enabled`/`config`
call the controller when the resolved feature id has one;
`slop-score`/`classify-emotion` are untouched.

The prose route body keeps its response and error strings verbatim — see §8.9.

### 7.4 Pipeline: delete `slm_rewrite.py`

Recommended deviation from §3's first draft. After `resolve_prose_rewrite` and
`prose_rewrite_step` move into the feature, the adapter has no behavior left: turn
placement is the call site in `editor_pass`, and the "editor-stage event
translation" is `editor.py`'s own `if ev["type"] == "draft_update"` branch. A
module that only re-exports two names under different spellings is a name, not a
boundary.

So: delete `backend/pipeline/passes/editor/slm_rewrite.py`, move its pre-audit
ordering docstring into the `editor_pass` prose block where the ordering is
enforced, and update three importers:

| File | Today | After |
|---|---|---|
| `pipeline/config.py:35,84` | `from .passes.editor.slm_rewrite import ProseRewrite, resolve_prose_rewrite` | `from ..features.prose_rewriter import ProseRewriteConfig, resolve_config` |
| `pipeline/state.py:29,84` | `from .passes.editor.slm_rewrite import ProseRewrite` | `from ..features.prose_rewriter import ProseRewriteConfig` |
| `passes/editor/editor.py:48,225` | `from .slm_rewrite import ProseRewrite, prose_rewrite_step` | `from ....features.prose_rewriter import ProseRewriteConfig, rewrite_events` |

If the team prefers to keep a named pipeline adapter, keep `slm_rewrite.py` with
exactly the docstring plus two aliases and leave the three importers alone; the
rest of this plan is unaffected either way. Decide before Phase 3 starts.

### 7.5 Every other call site

| File:line | Today | After |
|---|---|---|
| `api/routes/messages.py:45-49` | `from ...pipeline.passes.editor.slm_rewrite import ProseRewrite, prose_rewrite_step, resolve_prose_rewrite` | `from ...features.prose_rewriter import ProseRewriteConfig, resolve_config, rewrite_events` (from-import, so the tests keep patching module-local names) |
| `api/routes/messages.py:319,343,425` | `ProseRewrite` / `prose_rewrite_step` / `resolve_prose_rewrite` | `ProseRewriteConfig` / `rewrite_events` / `resolve_config` |
| `api/routes/stats.py:12-13` | `from ...inference.local_ml import model_dir`; `from ...inference.prose_rewriter.runtime import bin_bytes` | `from ...inference.local_models.assets import model_dir`; `from ...inference.local_models.llama_server.binary import bin_bytes` |
| `api/__init__.py:96-100` | deferred `prose_rewriter.shutdown()` | `from ..inference.local_models.llama_server import manager` (top level; stdlib + httpx only) and `await manager.shutdown_all()` |
| `api/routes/local_ml.py:17-18` | `from ...inference import local_ml, prose_rewriter` | `from ...inference import local_models`; `from ...features import prose_rewriter` |
| `features/cards/expressions.py:22` | `from ...inference.local_ml import GO_EMOTIONS` | unchanged |
| `workflows/toolkit.py:74,130` | re-exports `local_ml` | unchanged |
| `workflows/image_gen/pov.py` | `local_ml.available` / `aclassify_pov` | unchanged |

## 8. Hazards

These are the places where a mechanically correct move is still wrong.

### 8.1 `_ROOT` depth changes, and a wrong one silently re-downloads 9.6 GB

`local_ml.py` computes the repo root with three `dirname` calls;
`prose_rewriter/runtime.py` with `Path(...).parents[3]`. Both gain a directory
level:

| Module | Today | After |
|---|---|---|
| `local_ml.py` → `local_models/assets.py` | 3 × `dirname` | 4 × `dirname` |
| `prose_rewriter/runtime.py` → `local_models/llama_server/binary.py` | `parents[3]` | `parents[4]` |

A wrong depth does not raise: `model_dir()` happily creates
`backend/inference/data/models/` and reports every model as missing. Guard it
with a Phase 0 test that asserts both directories resolve under
`<repo>/backend/data/` (§9, test 3), and run that test before moving anything
else in Phase 1.

`dependencies.install_cmd` builds `<_ROOT>/requirements-ml.txt` from the same
constant and is covered by the existing `test_local_ml.py::test_install_cmd…`.

### 8.2 Monkeypatch targets follow the *callee*, not the facade

`local_ml.py` re-exporting `model_dir` creates a second binding. Patching
`local_ml.model_dir` will not affect `assets.model_dir`, which is what production
code now calls. Every test that patches a moved name must patch the module that
owns it:

| Test | Patch today | Patch after |
|---|---|---|
| `tests/integration/test_local_ml.py:56` | `local_ml.model_dir` | `assets.model_dir` |
| `…:74,78,174` | `local_ml.deps_ok` | `dependencies.deps_ok` |
| `…:80,175,181,224,238` | `local_ml.download` | `assets.download` |
| `…:37` | `ml_routes.prose_rewriter.HOST.release` | `prose_rewriter.HOST.release` (same object, new import path) |
| `tests/unit/test_prose_rewriter_catalog.py:46,68` | `local_ml.model_dir` | `assets.model_dir` |
| `tests/unit/test_local_ml.py:19` | `local_ml.resolve_path` | `assets.resolve_path` (`present` moves with it) |
| `tests/integration/test_character_expressions.py:85` | `local_ml.deps_ok` | `dependencies.deps_ok` |

Two fixtures patch by seam rather than by name and need the same treatment:

- `test_local_ml.py::_no_child_process` patches `ml_routes._prewarm` and
  `ml_routes.prose_rewriter.HOST.release`. `_prewarm` moves into
  `features/prose_rewriter/integration.py`, so the fixture patches
  `integration._prewarm`; `HOST.release` is the same object under a new import
  path. Both patches are load-bearing — without them a developer machine with a
  real `llama-server` on PATH and a fake GGUF from `_empty_model_dir` has
  everything it needs to actually start a child during the suite.
- `test_local_ml.py::_empty_model_dir` patches `local_ml.model_dir` and its
  docstring explains that `catalog.variant_path` reaches disk through it *via a
  lazy import*, "so patching the attribute covers it". After the refactor the
  import is no longer lazy and the seam is `assets.model_dir`. The patch keeps
  working — `assets.variant_path` looks `model_dir` up in its own module globals
  — but the docstring's stated reason becomes wrong and must be rewritten, not
  left as a comment that misdescribes why the test is safe.
- `test_local_ml.py` patches `pr_runtime.fetch` → `binary.fetch`.

Unaffected, because production still calls them through `local_ml`:
`tests/integration/test_autocomplete.py` (`local_ml.available`,
`local_ml.complete` — `available` is re-exported and `messages.py` calls
`local_ml.available()`), and every `aclassify_pov` patch in the image_gen tests.

**The mirror-image rule inside the moved code.** A monkeypatched name must be
read as a module attribute at call time, not bound at import. Three current
tests depend on exactly that and would go quietly dead if a move "tidied" the
import:

| Test patches | The moved code must therefore write |
|---|---|
| `S.runtime.IS_WINDOWS` | `from . import binary` … `binary.IS_WINDOWS` in `process.py` — **never** `from .binary import IS_WINDOWS` |
| `S._help_text` | `binary.supports_flag(...)` in `client.py`, as a module attribute |
| `S._free_port`, `S._can_spawn_async` | stay module-local to `client.py` / `process.py`, called unqualified |

`test_can_spawn_async_rejects_only_a_windows_loop_that_is_not_proactor` is the
one that matters most: it is the only coverage of the Windows selector-loop
fallback, and binding `IS_WINDOWS` by value makes it silently pass on the
constant instead of the branch.

### 8.3 The variant-trust check must not be lost in transit

`LlamaServer.__init__` currently proves that the `Variant` it was handed is the
registered record before putting its path on a command line. The generic client
cannot do that. `config.launch_profile_for` must perform it *before* constructing
the profile, and `tests/unit/test_prose_rewriter_child.py::test_llama_server_rejects_a_variant_outside_the_registry`
moves to the feature config test unchanged in intent. If that check is dropped
rather than moved, nothing fails — the barrier just quietly ceases to exist.

### 8.4 Import-time registration

`service.HOST` registers with the manager at import. Two consequences:

- `manager.shutdown_all()` in the lifespan does nothing if the feature was never
  imported. That is correct (nothing can have spawned a child), but a test that
  imports only the manager sees an empty registry.
- Registry state leaks between tests. Give `manager` an `unregister` and have the
  new manager tests build their own hosts and unregister in a fixture, rather
  than reaching for the module-level list.

### 8.5 `pyright` on the re-export shim

`local_ml.py` re-exporting names it does not define needs an explicit `__all__`
(or `as` re-export form) or Pyright reports them as private-and-unused on the
`standard` type-checking mode this repo runs. No `# pyright: ignore` — the repo
forbids suppressions.

### 8.6 Bandit/Ruff suppressions travel with the code

`# noqa: S603` on the two subprocess spawns, `# noqa: S202` on the archive
extraction, `# noqa: S310` on the two urllib calls, and `# noqa: PLC0415` on the
remaining deliberate deferred imports must move with the lines they annotate.
`scripts/security_check.sh` runs Bandit at `-ll -ii`; a moved-but-unannotated
`Popen` will not fail it, but the reasoning comment is the point.

### 8.7 The deferred `huggingface_hub` / `llama_cpp` imports must stay deferred

Base Orb runs without `requirements-ml.txt`. `assets.download` and
`dependencies._import_llama` keep their function-local imports. A tidy-up that
hoists them to module scope breaks every stock install at import time, and no
test in the suite installs the extras to catch it.

### 8.8 The prose feature must not import `local_ml.py`

It imports `local_models` only. `local_ml.py` keeps `llama_cpp`-flavored
in-process code; a feature that reaches for it re-creates the coupling this
refactor removes, and Phase 5's layer checker will not catch it because both are
in `inference`. Assert it with a grep in the Phase 3 exit check.

### 8.9 Strings the tests assert verbatim

These are compared literally by the existing suite (or rendered into the
Settings panel) and must survive the move byte-for-byte, wherever they end up:

| String | Today | After |
|---|---|---|
| `"batch_size must be an integer from 1 to 4"` | assembled in the config route from `MIN/MAX_BATCH_SIZE` | same, in the generic config route via the controller |
| `"Unregistered prose-rewriter variant {id!r}"` | `LlamaServer.__init__` | `config.launch_profile_for` |
| `"slots must be between 1 and 4"` | `ModelHost.__init__`/`ensure`/`use` | `config.launch_profile_for` |
| `"Unknown variant {v!r} for {f!r}"`, `"Unknown local-ML feature {f!r}"` | generic route `_require` | unchanged |
| `"{feature!r} has no configurable variants"` | config route's `if not spec.variants` | config route's `if feature not in _MANAGEMENT` — same wording, and still accurate for `autocomplete`, the feature the test posts |
| `"{label} is not downloaded — {local_name} is missing."` | `ModelHost._ensure_locked` | `config.launch_profile_for` (it is the one place that knows the label) |
| `"No llama-server binary. Fetch one from Settings → Local ML → Prose Rewriter, …"` | `runtime.find_binary` | see §8.10 |

### 8.10 Two prose strings live inside the module that becomes shared

`inference/prose_rewriter/runtime.py` — which moves wholesale into
`llama_server/binary.py` — contains two prose references that a naive move
carries into shared code, and that the Phase 2 exit grep will flag:

1. `USER_AGENT = "Orb/prose-rewriter"`, sent to the GitHub releases API.
   **Rename to `"Orb/llama-server"`.** It is outbound only, no contract in §12
   covers it, and leaving a feature name on the shared binary fetcher's UA is
   the kind of thing nobody ever comes back to fix.
2. `LlamaServerMissing`'s message names *Settings → Local ML → Prose Rewriter*.
   **Keep it**, with a comment saying why: it is user-visible panel text, the
   rewriter is the only place in the UI that offers the fetch, and a generic
   "no binary" message would send the user nowhere. Revisit when a second
   llama-server feature exists — that is the moment the message becomes wrong,
   and it is a one-line fix then.

Add both to the Phase 2 exit check's known-exception list rather than weakening
the grep.

## 9. Test plan

### 9.1 Phase 0 — write these before moving anything

| Test | File | Asserts |
|---|---|---|
| 1 | `tests/unit/test_local_models_catalog.py::test_every_downloadable_basename_is_claimed_exactly_once` | moved from `test_prose_rewriter_catalog.py:24` |
| 2 | `…::test_prune_stale_keeps_every_registered_prose_variant` | write all three variant basenames plus an unclaimed file into a tmp dir; prune; all three survive |
| 3 | `tests/unit/test_local_models_paths.py::test_model_and_binary_dirs_resolve_under_backend_data` | `assets.model_dir()` == `<repo>/backend/data/models`, `binary.bin_dir()` == `<repo>/backend/data/llama-bin` (§8.1) |
| 4 | `tests/unit/test_llama_server_manager.py::test_release_all_releases_every_registered_host` | two fake hosts, both `release`d |
| 5 | `…::test_shutdown_all_stops_every_registered_host` | two fake hosts, both `shutdown`; one raising does not skip the other |
| 6 | `tests/unit/test_llama_server_client.py::test_completion_body_carries_the_callers_stop_sequence` | `stop=("<\|im_end\|>",)` → `"stop": ["<\|im_end\|>"]`; `stop=()` → no `stop` key |
| 7 | `tests/unit/test_backend_layers.py` or `scripts/check_backend_layers.py` | `inference/local_models/**` imports no `features`/`pipeline`/`api`/`workflows` module |

Tests 1–3 and 6–7 can be written against the current tree (1 already exists, 3
and 6 fail until Phase 1/2 land — mark them `xfail(strict=True)` and flip in the
phase that makes them pass, or write them in the same commit as the move).

### 9.2 Moves and splits

| From | To | Contents |
|---|---|---|
| `tests/unit/test_prose_rewriter_catalog.py` | `tests/unit/test_local_models_catalog.py` | basename claim, default-is-a-variant, prune keep/remove |
| (same file, remainder) | `tests/unit/test_prose_rewriter_catalog.py` | unusable selection → `None`, `variant_path` under the models dir |
| `tests/unit/test_prose_rewriter_child.py` | `tests/unit/test_llama_server_child.py` | child streaming/exit/idempotent stop, `spawn` selection, `_can_spawn_async`, boot-failure log, slot argv |
| (same file) | `tests/unit/test_llama_server_host.py` | release drains an in-flight rewrite, release with no child forces the next load |
| (same file) | `tests/unit/test_prose_rewriter_config.py` | variant outside the registry rejected (§8.3), batch size outside 1..4 rejected |

Two of those are **rewrites, not moves**, because the thing they call changes
shape. Do not let a mechanical port turn them into tautologies:

- `test_batch_size_change_marks_the_loaded_host_stale` currently pokes
  `host.variant`/`host.gpu`/`host._stale` and asserts `host.slots == 2` after
  `mark_stale(variant, True, 2)`. It becomes: seed `host.profile` with a
  4-lane profile, call `mark_stale(profile_for(same variant, same gpu, 2))`,
  assert `host.profile.parallel == 2` and `host.healthy is False`. It must also
  keep the negative half implicitly covered by `mark_stale`'s early return —
  add `test_an_identical_profile_does_not_mark_the_host_stale`, which is the
  branch that stops a settings write from restarting a healthy child.
- `test_host_rejects_batch_sizes_outside_the_supported_range` tests
  `ModelHost(slots=0)`. The generic host no longer has an opinion; the same
  assertion moves onto `launch_profile_for(variant, gpu, 0)` and keeps the
  `"slots must be between 1 and 4"` match (§8.9).
| `tests/unit/test_prose_rewriter_rewrite.py:116-148` | `tests/unit/test_prose_rewriter_config.py` | the two `resolve_prose_rewrite` cases + the `select_batch_size` allowlist cases |
| `tests/unit/test_prose_rewriter_text.py` | unchanged path | import path only |

### 9.3 Fakes that need signature updates

`tests/unit/test_prose_rewriter_rewrite.py` defines `_Server` (with
`count_tokens`/`generate`/`slots`) and `_Host` (with `use(variant, gpu, slots)`).
`_Server` keeps working if it grows the `stop`/`cache_prompt` keyword defaults;
`_Host.use` becomes `use(profile)`. `test_parallel_slots_select_only_fixed_command_arguments`
keeps asserting the exact argv strings — that is the CodeQL-barrier evidence and
its assertions must not weaken.

`tests/integration/test_message_prose_rewrite.py` patches
`message_routes.prose_rewrite_step` (7 sites) and `message_routes.resolve_prose_rewrite`;
both become `rewrite_events` / `resolve_config`. Nothing else in that file changes.

### 9.4 New coverage the refactor makes cheap

- `tests/integration/test_local_ml.py::test_status_payload_keys_are_unchanged` —
  pins the §7.1 union so the generic/controller split cannot drop a key.
- `tests/unit/test_prose_rewriter_config.py::test_launch_profile_is_a_pure_function_of_the_selection`
  — two profiles built from the same triple compare equal, and differ on any
  change of variant, gpu or batch size. This is the load key (§5.4).

## 10. Implementation phases

Each phase leaves Ruff, Pyright and the full suite green, and is one commit (or
one commit per numbered step where noted). Do not keep a final
`inference/prose_rewriter` compatibility package that imports upward into
`features`; migrate those imports atomically in Phase 3.

### Phase 0 — Characterize missing boundaries

Write the tests in §9.1. The existing suite already covers progress ordering,
sibling cancellation, Windows child supervision, boot logs, slot allowlists,
drain-before-release, selection repair, on-demand abort, and persistence timing;
do not rewrite those, only move them later.

Exit: `pytest tests/unit/test_local_models_catalog.py tests/unit/test_llama_server_manager.py`
passes (against stubs where the code does not exist yet), and the layer check
runs clean on the current tree.

### Phase 1 — Extract catalog, dependencies, and assets

1. `local_models/catalog.py`: `RuntimeKind`, `ModelSpec`, `ModelVariantSpec`
   (the old `Variant`), the pinned repo constants, and the complete `MODELS`
   manifest including the prose artifact records.
2. `local_models/assets.py`: `_ROOT` (**four** dirnames), `model_dir`,
   `resolve_path`, `present`, `variant_present`, `variant_path`, `variant_spec`,
   `download`, `delete_model`, `prune_stale`, and the prune self-check.
3. `local_models/dependencies.py`: `_import_llama`, `_shell_quote`,
   `install_cmd`, `deps_ok`.
4. `local_models/__init__.py`: facade plus `available(feature)`.
5. `local_ml.py`: delete the moved code, add the §4.1 re-export list and
   `__all__`, keep every in-process inference function and its self-checks.
6. Rewrite `inference/prose_rewriter/catalog.py` to consume the generic records
   with top-level imports — the deferred `from ..local_ml import …` pair goes.
7. Rename `Variant` → `ModelVariantSpec` at every use in the same commit, with no
   transitional alias: `prose_rewriter/catalog.py` (definition and 3 uses),
   `prose_rewriter/server.py` (`from .catalog import Variant`, the `LlamaServer`
   and `ModelHost` annotations), `prose_rewriter/rewrite.py` (import +
   `arewrite`'s parameter). One type, one name — an alias kept "just for this
   phase" is how a codebase ends up with both spellings forever.
8. Update the patch targets in §8.2 for the tests that run in this phase.

Exit: `grep -rn "prose_rewriter" backend/inference/local_ml.py backend/inference/local_models/` returns
only the string key `"prose_rewriter"` in `MODELS` and prose *artifact* records —
no import. `pytest tests/unit/test_local_ml.py tests/unit/test_local_models_catalog.py tests/unit/test_local_models_paths.py tests/unit/test_prose_rewriter_catalog.py tests/integration/test_local_ml.py`.

### Phase 2 — Extract the llama-server runtime

1. `llama_server/binary.py`: all of `runtime.py` (`parents[4]`), plus
   `supports_flag` carrying `_HELP_CACHE`.
2. `llama_server/process.py`: `Child`, `_AsyncChild`, `_ThreadChild`,
   `_can_spawn_async`, `_decode`, `spawn`.
3. `llama_server/client.py`: `_free_port`, `_error_text`, `BOOT_TIMEOUT`,
   `LlamaServerClient` with the §5.3 signature and profile-driven `_argv`.
4. `llama_server/host.py`: `LaunchProfile` and `ManagedLlamaServerHost` per §5.4
   and §5.5 — slot-range guards deleted, name/idle_timeout injected.
5. `llama_server/manager.py` per §5.6, and `llama_server/__init__.py` as facade.
6. `inference/prose_rewriter/profile.py` (new, transitional) holds the prose
   slot allowlist, alias, idle timeout, and `launch_profile_for` — including the
   registry check from §8.3, which must not spend a phase not existing.
   `server.py` shrinks to a re-export shim (`HOST`, `DEFAULT_SLOTS`,
   `MIN_SLOTS`, `MAX_SLOTS`) so nothing above it moves yet, and `rewrite.py`
   builds a profile instead of passing `(variant, gpu, batch_size)`.

   This is deliberate transitional *location*, not duplicated logic: Phase 3
   moves `profile.py`'s contents into `features/prose_rewriter/config.py`
   unchanged. The alternative — merging Phases 2 and 3 — produces one commit
   that moves ~1,100 lines across two layers at once, which is not reviewable.
   What is forbidden is the other direction: creating
   `features/prose_rewriter/config.py` early and importing it from
   `inference/prose_rewriter/`, which is the exact upward edge this whole plan
   exists to delete, even for one phase.
7. Keep `ORB_LLAMA_SERVER`, `ORB_LLAMA_CPP_BUILD` and `ORB_PROSE_REWRITER_IDLE`
   working and named as they are.
8. Move the child/host tests per §9.2 and update the fakes per §9.3.

Exit: `grep -rniE "prose|im_end|4b-q8|1\.7b" backend/inference/local_models/llama_server/`
returns only the one accepted exception from §8.10 — the `LlamaServerMissing`
message that points at the Settings panel. `USER_AGENT` must already be renamed,
and any other hit is a piece of prose policy that failed to move.
`pytest tests/unit/test_llama_server_child.py tests/unit/test_llama_server_host.py tests/unit/test_llama_server_manager.py tests/unit/test_prose_rewriter_rewrite.py`.

### Phase 3 — Create the prose-rewriter feature slice

Commit per step where the step is a move; the last three steps are one atomic
commit because they cross the import boundary.

1. `features/prose_rewriter/text.py` — move first, alone. Bodies byte-identical;
   add `STOP_TOKEN`. `pytest tests/unit/test_prose_rewriter_text.py` must pass
   without touching an assertion.
2. `features/prose_rewriter/catalog.py` — `FEATURE`, `variants`, `resolve`,
   `variant_path`, `on_disk`.
3. `features/prose_rewriter/config.py` — batch-size allowlist, `SLOT_ALLOCATION`,
   `IDLE_TIMEOUT`, `ProseRewriteConfig`, `resolve_config`, `launch_profile_for`
   with the §8.3 registry check.
4. `features/prose_rewriter/rewrite.py` — `arewrite(draft, profile, *, host=None, on_progress=None)`,
   typed against the client protocol rather than a concrete class.
5. `features/prose_rewriter/service.py` — `HOST` (registered), `available`,
   `state`, `shutdown`, `rewrite_events`.
6. `features/prose_rewriter/integration.py` — `_sync_selection`, `_prewarm`,
   `_spawn`/`_BACKGROUND`, `status_extra`, `apply_config`, `on_enabled`,
   `release_host`, `fetch_runtime`, moved out of `api/routes/local_ml.py`.
7. `features/prose_rewriter/__init__.py` facade.
8. **Atomic:** update `pipeline/config.py`, `pipeline/state.py`,
   `passes/editor/editor.py` (and delete `slm_rewrite.py`, per §7.4);
   update `api/routes/messages.py`; update `api/routes/local_ml.py`'s imports;
   delete `backend/inference/prose_rewriter/`.
9. Move and rename the tests per §9.2–§9.3.

Exit: `test -d backend/inference/prose_rewriter` is false;
`grep -rn "inference.prose_rewriter\|inference import prose_rewriter" backend tests` is empty;
`grep -rn "local_ml" backend/features/prose_rewriter/` is empty (§8.8).
`./scripts/tests.sh all`.

### Phase 4 — Separate management API and lifecycle composition

1. Add `api/routes/prose_rewriter.py` with the runtime endpoint only (§7.3), and
   insert it into `ROUTERS` **ahead of** `local_ml.router`.
2. Move `_download_lock` to `api/deps.py` so both routers share the one lock.
3. Replace the prose conditionals in `api/routes/local_ml.py` with the §7.2
   controller map; the generic route keeps `_require`, download, delete, enable,
   **config**, status assembly, and the two inference endpoints. `_sync_selection`,
   `_prewarm`, `_spawn` and `_BACKGROUND` leave the module entirely.
4. `api/__init__.py` lifespan teardown → `manager.shutdown_all()`.
5. `integration.fetch_runtime` → `manager.release_all()` before touching binary
   files.
6. `api/routes/stats.py` → `local_models.assets.model_dir` and
   `local_models.llama_server.binary.bin_bytes`.
7. Add the status-payload contract test (§9.4).

Exit: `grep -rn "prose" backend/api/__init__.py backend/api/routes/stats.py` is empty.
`pytest tests/integration/test_local_ml.py tests/integration/test_message_prose_rewrite.py`.

### Phase 5 — Cleanup and architecture documentation

1. Remove the obsolete deferred imports and the compatibility comments that named
   the cycle they existed to break.
2. `scripts/check_backend_layers.py` + a line in `scripts/lint.sh`. Ranks that
   pass the tree unchanged today: `core` 0, `database` 1, `analysis` 2,
   `inference` 2, `workflows` 3, `features` 4, `pipeline` 5, `api` 6. A module may
   import its own rank or lower, plus one extra rule: a `features/<a>` module may
   not import `features/<b>`. Resolve `from .. import database as db` to the
   subpackage, not to `backend`, or the checker reports phantom root edges.
3. AGENTS.md: the `inference/` row (local models now `local_ml.py` +
   `local_models/`), the `features/` row (add `prose_rewriter`), and the layer
   stack wording from §2.8 — pipeline may import features; features never import
   pipeline; slices never import peers.
4. `docs/features/prose-rewriter.md` only where module ownership or shared runtime
   behavior is described. User behavior does not change.
5. Full validation suite.

## 11. Behavior and safety invariants

The refactor is incomplete if any of these change.

### Rewrite behavior

- The exact three-block prompt stays byte-identical.
- Sampling remains temperature 0.9 and top-p 0.9.
- Short paragraphs, over-token paragraphs, paragraph caps, and character caps
  keep their current pass-through behavior.
- Repairs run in the same order.
- Progress remains whole-draft, ordered from the top despite concurrent jobs.
- A failed paragraph cancels every sibling request.

### Turn and message behavior

- The rewriter runs before editor audit and target construction.
- It remains independent of the Agent toggle.
- Any failure preserves the Writer draft and emits one non-terminal warning.
- On-demand rewriting still prefers `writer_draft`, then falls back to saved
  content.
- Partial progress is never persisted.
- Abort, disconnect, or failure leaves the stored message unchanged.
- A successful changed rewrite stales pending World proposals.

### Runtime behavior

- The child binds only to loopback on an ephemeral port.
- The web UI remains disabled when the binary supports the flag.
- Windows selector-loop installs use the threaded process fallback.
- Boot failures retain the child's final diagnostic lines, drained before the
  error is raised.
- Changing model, GPU placement, or slots drains in-flight work.
- Deleting a mapped GGUF and replacing a running executable release hosts first.
- Idle unload frees the resident model.
- Application shutdown cannot orphan a llama-server child.

### Artifact safety

- Repository revisions remain pinned.
- Request data never becomes a path or argv token without catalog resolution.
- Every downloadable basename is claimed exactly once.
- `prune_stale` always sees the complete built-in manifest.
- A failed download keeps the previous model.
- Runtime archive path traversal protections remain intact.

## 12. Compatibility requirements

No migration is planned for:

- `settings.local_ml_enabled`;
- `settings.local_ml_config`;
- the `prose_rewriter` feature key;
- stored variant ids;
- runtime or batch-size defaults;
- API URLs or response shapes;
- SSE event names or payloads;
- environment variable names;
- model filenames or storage directories.

No database migration should be added. `database/preset_schema.py` already lists
both settings columns; nothing there moves.

## 13. Explicit non-goals

- Rewriting autocomplete, emotion, slop, or POV feature architecture.
- Moving workflow-consumed model adapters into features.
- Adding a general plugin API for local models.
- Allowing arbitrary user-supplied llama-server arguments.
- Sharing one resident child across unrelated features.
- Changing model weights, prompts, sampling, or output repairs.
- Changing the Settings UI or any frontend file.
- Supporting multiple users or tabs.

## 14. Validation

Focused, during the phases:

~~~sh
pytest tests/unit/test_local_ml.py
pytest tests/unit/test_local_models_catalog.py tests/unit/test_local_models_paths.py
pytest tests/unit/test_llama_server_child.py tests/unit/test_llama_server_host.py tests/unit/test_llama_server_manager.py
pytest tests/unit/test_prose_rewriter_catalog.py tests/unit/test_prose_rewriter_config.py
pytest tests/unit/test_prose_rewriter_text.py tests/unit/test_prose_rewriter_rewrite.py
pytest tests/integration/test_local_ml.py
pytest tests/integration/test_message_prose_rewrite.py
pytest tests/integration/test_autocomplete.py tests/integration/test_character_expressions.py
~~~

Then repository validation:

~~~sh
./scripts/lint.sh
./scripts/tests.sh all
./scripts/security_check.sh
~~~

Acceptance criteria, as checks:

1. `python -m pyright backend/` reports zero errors.
2. `scripts/check_backend_layers.py` exits 0. Do not spell this acceptance check
   as a bare grep for `api\.` under `inference/` — `binary.py` contains the
   literal `https://api.github.com/repos/...`, so that grep reports a violation
   that is not one and trains the next person to ignore it. The checker parses
   imports; use the checker.
3. No feature module imports `pipeline`, `api`, `workflows`, or another slice.
4. `backend/inference/prose_rewriter/` does not exist.
5. Existing Local ML and prose-rewrite HTTP/SSE contracts pass unchanged,
   including the status-payload key test.
6. Runtime, cancellation, selection, persistence and prune invariants each have a
   direct test, at the layer that now owns them.
7. Fresh installs and existing databases require no migration
   (`tests/integration/test_fresh_install_stamping.py` still passes untouched).

## 15. Expected outcome

After the refactor, adding another llama-server-backed feature requires:

- adding its artifacts to the complete shared manifest;
- constructing its own trusted `LaunchProfile` and `ManagedLlamaServerHost`;
- implementing feature-specific prompt/input/output behavior above inference;
- registering its host with the shared runtime manager for shutdown and binary
  replacement.

It does not require copying subprocess supervision, HTTP parsing, binary download
logic, model storage, or lifecycle code, and it does not force the new feature
into the prose rewriter's paragraph-oriented API.

## 16. Open decisions

All three should be settled before Phase 3 begins; none changes any other phase.

1. **`pipeline/passes/editor/slm_rewrite.py`** — delete it (§7.4, recommended) or
   keep it as a two-alias adapter that preserves the pipeline-side names.
2. **`ProseRewriteConfig` vs keeping `ProseRewrite`** — the rename costs four call
   sites and buys an unambiguous name outside the slice. Keeping the old name
   costs nothing and reads slightly worse at `prose_rewriter.ProseRewrite`.
3. **`local_ml.py` living next to `local_models/`** — one letter apart at a
   glance, and after this refactor they mean genuinely different things
   (in-process GGUF inference vs. artifacts + llama-server). `local_ml.py`
   cannot simply be renamed: `workflows/toolkit.py` re-exports the module under
   that name as part of the workflow author's API, and
   `features/cards/expressions.py` imports from it. Options: (a) keep both names
   and make `local_ml.py`'s first docstring line say what it is *not* — "the
   in-process half; artifacts and the llama-server child live in
   `local_models/`"; (b) name the shared package something further away, e.g.
   `inference/model_store/`, at the cost of the plan's own naming throughout.
   Recommend (a) — the confusion is real but it is a one-line-of-docs problem,
   and (b) trades it for a name that describes the assets half but not the
   runtime half.
