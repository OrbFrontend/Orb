# Plan: Managed Image Generation Secondary Workflow

## Decision

Orb will ship image generation as an `image_gen` secondary workflow with three deliberately unequal execution modes:

| Mode | Generation | Model discovery | Orb model installation | Intended user |
|---|---|---|---|---|
| `managed_local` | Orb-managed, headless ComfyUI sidecar | Yes | Yes, from Orb's curated catalog | Default local experience |
| `external_comfy` | User-supplied ComfyUI HTTP endpoint | Yes | No | Advanced users with an existing engine |
| `openai_images` | Configured cloud Images API | Provider-dependent | Not applicable | Users preferring a hosted API |

The managed sidecar is the only mode allowed to promise one-click setup, curated model downloads, or automatic model selection. External ComfyUI is generation-only unless a future authenticated Orb companion service is installed on the remote host. Cloud styles are prompt additions; Orb does not pretend to install or switch provider-owned models.

This asymmetry is intentional and represented as capabilities rather than hidden behind a falsely uniform adapter:

```python
class ImageBackendCapabilities(TypedDict):
    can_generate: bool
    can_list_models: bool
    can_install_curated_models: bool
    managed_runtime: bool
```

The documented core ComfyUI server routes can submit workflows, inspect history, fetch outputs, and enumerate model folders, but do not define a model-install route. Model installation therefore cannot be assumed for a generic remote server. See [ComfyUI server routes](https://docs.comfy.org/development/comfyui-server/comms_routes). ComfyUI-Manager is not part of the v1 contract: remote install permissions vary by its security mode and enabling permissive remote operations expands the attack surface. See [ComfyUI-Manager security modes](https://github.com/Comfy-Org/ComfyUI-Manager/blob/main/docs/en/v3.38-userdata-security-migration.md).

“Built in” means **installed and supervised by Orb as an optional separate process**. ComfyUI, PyTorch, and model weights are never imported into Orb's Python process and are not part of the base installation.

## Non-negotiable product behavior

- The normal generation UI is a style dropdown and a Generate button. Initial stable style ids are `realistic`, `anime`, `pixel_art`, `scenery`, and `line_art`; catalog updates may add styles but never silently repurpose an existing id.
- The user explicitly opts into every runtime or model download. No multi-gigabyte download starts because a conversation was opened or a generation button was pressed.
- In managed-local mode, selecting a style selects its entire curated recipe. The normal UI never exposes checkpoints, VAEs, graph nodes, samplers, CFG, steps, or LoRAs.
- If the selected local recipe is not installed, the UI reports the exact download size and source links and offers one Download action. Generation stays disabled until every artifact verifies successfully.
- In cloud and external modes, style prompt text is applied, but the configured provider/remote model remains in control.
- `auto_generate` exists but defaults to `false`. It never auto-installs a runtime or model.
- The LLM only describes the visual scene through one fixed standalone forced call. It never chooses the backend, model, workflow, sampler, dimensions, or style.

## Facts that constrain the implementation

- ComfyUI model files are commonly multiple gigabytes and may consist of a checkpoint plus encoders, VAE, LoRAs, or other companion weights. A request-long `hf_hub_download` call is not an adequate product download manager. See [ComfyUI model documentation](https://docs.comfy.org/development/core-concepts/models).
- ComfyUI officially supports model directories outside its installation through `extra_model_paths.yaml`. The managed sidecar will read Orb's model store this way; Orb will not copy weights into the runtime tree. See [extra model paths](https://docs.comfy.org/development/core-concepts/models#adding-extra-model-paths).
- ComfyUI needs an isolated environment because its dependencies may conflict with the host application. GPU/PyTorch installation differs by platform and accelerator. See [manual installation](https://docs.comfy.org/installation/manual_install) and [system requirements](https://docs.comfy.org/installation/system_requirements).
- There is no official universal ComfyUI Docker image, so a container is not the default sidecar mechanism. The managed installer is platform-manifest driven and must show “unsupported” when no tested runtime variant matches.
- “Best model for a style” is a curatorial decision that changes over time. Code consumes a versioned catalog; it must never hard-code marketing claims or infer quality from a filename. A bundle becomes recommended only after its exact revision, hashes, resource requirements, workflow, and output quality have been reviewed.

## Scope

### v1

- Managed local ComfyUI install/start/stop/health/log lifecycle.
- Curated, opt-in model-bundle download/remove/repair with progress, cancellation, resume, disk checks, and SHA-256 verification.
- Five simple styles: realistic, anime, pixel art, scenery, and line art.
- First-party ComfyUI nodes only; shipped API-format workflows only.
- Text-to-image, one image per request.
- Per-message Visualize action and optional blocking post-pipeline generation.
- External ComfyUI generation against a user-configured checkpoint.
- One explicit cloud adapter for an OpenAI-style Images endpoint; provider-specific adapters can be added later rather than assuming all “compatible” APIs behave identically.
- Existing `workflow_attachments` storage, sibling reroll/regenerate behavior, and default `image/*` renderer.

### Not v1

- ComfyUI-Manager, arbitrary custom nodes, arbitrary workflow upload, arbitrary model URLs, or a model marketplace.
- Remote model installation. A future remote-management option requires an authenticated companion daemon with an allowlisted catalog and filesystem sandbox; it is not implemented through undocumented Manager routes.
- Forge/A1111 adapters. They add another lifecycle/model-selection contract without improving the managed default.
- Live remote catalog updates. The catalog ships with Orb releases; signed catalog updates can be designed later.
- img2img, inpainting, ControlNet, IPAdapter/FaceID, character LoRAs, expression-pack generation, batches, or live previews. These reappear as the building blocks of the planned v2 character-identity feature; v1 ships none of them but reserves its extension points (see "Forward compatibility: character identity consistency (v2)").
- Automatic runtime/model updates. Upgrades are explicit and compatibility-checked.
- Universal hardware support. Only runtime variants exercised by the release matrix are offered.
- Byte-identical replay across GPU, driver, PyTorch, ComfyUI, model, or workflow changes.

## Forward compatibility: character identity consistency (v2)

v1 deliberately ships no character-identity mechanism: a generic prompt (“blue eyes, brown hair, red lips”) yields a different face every generation, so the user feels like they are talking to a new character each turn. A v2 **character identity** feature will address this without per-user asset wrangling: a canonical **reference portrait** is generated once from the character's appearance (locked seed, portrait framing), stored as character state, and used to condition every subsequent generation. The default identity mechanism is **IPAdapter-plus-face** (CLIP-vision, style-agnostic so it works on both anime and realistic recipes), with **InstantID** (InsightFace) as a realistic-only upgrade and a trained **character LoRA** as the opt-in gold standard. An optional face-region detailer inpaint applies the identity at high fidelity. Because Orb is a non-commercial, local-only, open-source tool, InsightFace's non-commercial model license and curated non-first-party nodes are acceptable here.

None of this is implemented in v1. v1's only obligation is to not foreclose it. These are the explicit compatibility decisions that keep v2 additive rather than a rewrite:

- **Additive catalog only.** Identity recipes and bundles are new ids/versions; the five stable style ids and their v1 recipes are never repurposed, so stored v1 attachments keep resolving. Identity simply exercises the existing catalog-versioning rule.
- **Model-store kinds are a data-driven, extensible set.** `BundleSpec.kind` maps to a model-store subdirectory. v2 adds `ipadapter/`, `clip_vision/`, and (for InstantID) `controlnet/`/`instantid/` by extending the validated kind set and the store layout. v1 rejects unknown kinds on purpose; new kinds arrive only with new reviewed catalog versions.
- **The graph node allowlist is a reviewed data set, not a first-party assumption.** Graph validation admits nodes from a reviewed allowlist; v2 extends it with vetted identity nodes (IPAdapter, an anime/real face detector, InstantID). Nothing below the allowlist hard-codes “first-party only.”
- **Curated custom nodes reuse existing machinery.** v2 delivers a pinned, hash-verified, reviewed node set as **node bundles** through the same download/verify/atomic-install primitive and job registry as model bundles (pinned git commit plus pinned pip requirements installed into the isolated environment; no ComfyUI-Manager). v1's runtime installer and supervisor must not assume an empty `custom_nodes` directory.
- **Recipes may be multi-stage.** The slot map patches arbitrary declared nodes, so a base → detect → identity-inpaint → composite graph needs no contract change — only new allowlisted node types.
- **Character state reserves identity keys.** `workflow_character_state` carries `reference_image` and `face_seed` now, `null` and unused in v1. The character-state normalizer preserves them across round-trips, and a v1 test pins this so the reservation cannot silently rot.
- **Replay metadata is open and tolerant.** Identity fields (`identity_method`, `reference_image_ref`, identity-artifact SHA-256s) are additive to `generation_metadata`. v1 replay ignores fields it does not know, and v2 replay of a v1 row tolerates their absence.
- **The forced-call scene schema is unchanged.** v2 adds a non-text reference-image conditioning path around the composer, not a new field in `compose_image_prompt`, so the LLM contract stays stable.

Still explicitly not v1: reference-portrait generation, any identity conditioning, custom nodes actually shipped, LoRA training, and the identity model bundles themselves.

## Architectural placement

Image generation is owned entirely by its secondary-workflow package, mirroring TTS. Runtime management has two consumers—workflow hooks and dedicated API routes—but both belong to the same image-generation feature; multiple consumers do not make it generic LLM inference infrastructure.

The engine subpackage remains independently importable and has no dependency on the workflow registry or hook contracts. `hooks.py` is the integration boundary: it imports the engine through the engine's public facade and imports Orb services through `workflows.toolkit`. The higher `api/` layer may import `workflows.image_gen.engine` for runtime/model-management routes and lifespan shutdown, which follows Orb's `api/ → workflows/` dependency direction. Nothing in `engine/` imports `api/`, `pipeline/`, `features/`, another workflow, or `database/`.

```text
backend/workflows/image_gen/
  __init__.py                 # Workflow declaration only; no registration side effects
  config.py                   # strict config/profile normalization
  composer.py                 # forced-call schema and prompt assembly
  hooks.py                    # post/on-demand/regenerate/reroll integration
  engine/
    __init__.py               # narrow public facade for hooks, API routes, and lifespan
    contracts.py              # StyleSpec, RecipeSpec, BundleSpec, request/result/capabilities
    catalog.py                # load + validate immutable shipped catalog
    paths.py                  # data-root resolution and path-containment checks
    jobs.py                   # bounded background install jobs and progress snapshots
    downloads.py              # HTTPS download/resume/hash/atomic-install primitive
    runtime.py                # runtime variant detection, install/repair/status
    supervisor.py             # one managed ComfyUI child process
    comfy_client.py           # documented ComfyUI HTTP execution/discovery client
    render.py                 # source/style resolution and adapter routing
    adapters/
      base.py
      managed_comfy.py
      external_comfy.py
      openai_images.py
    resources/
      catalog.v1.json         # styles, recipes, bundles, exact artifacts
      runtimes.v1.json        # tested runtime variants and pinned sources/hashes
      workflows/
        realistic.v1.json
        anime.v1.json
        pixel_art.v1.json
        scenery.v1.json
        line_art.v1.json

backend/api/routes/image_gen.py        # thin HTTP facade over the workflow engine
frontend/workflows/image_gen/
  index.js
  widget.js
  config_panel.js
  image_gen.css
```

This deliberately follows `backend/workflows/tts/engine/` rather than placing a feature-specific engine in `backend/inference/`. `backend/inference/local_ml.py` remains separate legacy precedent for optional local assets and is not reused as the image-model manager: its single blocking download and one-file-per-feature contract lacks bundle manifests, progress, cancellation, resume, disk reporting, and cryptographic verification. A download primitive should move to shared lower-level infrastructure only after a second real consumer requires the same contract.

## Data roots and ownership

`ORB_IMAGEGEN_DATA_DIR` overrides the root. The source-install default is `backend/data/imagegen/`, matching existing Orb local-model storage. Every filesystem operation resolves the final path and proves it remains below this root; catalog ids and filenames are never treated as paths supplied by the frontend.

```text
imagegen/
  runtime.lock                # exclusive managed-local owner; OS releases it on process exit
  runtime/
    <runtime-version>/        # isolated ComfyUI source/archive + Python environment
    active.json               # selected healthy runtime version; atomic replacement
  models/
    checkpoints/
    vae/
    clip/
    diffusion_models/
    loras/
  downloads/                  # resumable *.part files, never visible to ComfyUI
  logs/
    comfyui.log
```

Installed state is derived from the catalog plus verified files on disk, not stored as a second truth in SQLite. In-memory job records are disposable; after restart the status endpoint reconstructs installed/missing/corrupt state and reusable `.part` downloads remain resumable.

The sidecar receives a generated `extra_model_paths.yaml` pointing to the Orb-owned `models/` tree. Runtime upgrades replace only `runtime/<version>` and cannot delete models. Bundle removal deletes only catalog-owned artifacts no other installed bundle references; unknown/manual files are never pruned.

## Catalog: styles decide differently by backend

The catalog is parsed into strict immutable records at import. Duplicate ids, unknown references, unsafe relative paths, invalid hashes, missing workflow resources, and unsupported node slots fail tests and make the affected entry unavailable. They do not crash normal Orb boot.

### `StyleSpec`

```json
{
  "id": "anime",
  "label": "Anime",
  "description": "Clean anime illustration",
  "managed_recipe_id": "anime-v1",
  "prompt": "anime illustration, clean line art",
  "negative_prompt": "photorealistic"
}
```

Each style supplies its own prompt fragments, e.g.:

```json
{
  "id": "scenery",
  "label": "Scenery",
  "description": "Wide environment and landscape art",
  "managed_recipe_id": "scenery-v1",
  "prompt": "expansive scenery, detailed environment, atmospheric lighting, wide establishing shot",
  "negative_prompt": "close-up portrait, cropped subject"
}
```

```json
{
  "id": "line_art",
  "label": "Line art",
  "description": "Clean monochrome ink line art",
  "managed_recipe_id": "line_art-v1",
  "prompt": "clean black and white line art, bold ink linework, monochrome, flat, no shading",
  "negative_prompt": "color, painterly shading, grayscale gradient, photorealistic"
}
```

The five initial ids are stable UI concepts, not model names:

- `realistic`: photographic or cinematic realism.
- `anime`: drawn anime/manga character illustration.
- `pixel_art`: deliberately pixelated game-art rendering with recipe-appropriate dimensions and scaling.
- `scenery`: environment/landscape rendering that centers the setting over any single subject.
- `line_art`: monochrome ink line drawing with clean outlines and minimal or no shading.

`scenery` and `line_art` are full styles: each carries its own positive/negative prompt and, in `managed_local`, its own recipe, graph, and params. A recipe may reuse an already-installed curated bundle via a shared `bundle_id` where the curator judges an existing checkpoint suitable, avoiding a second multi-gigabyte download; the shared-artifact removal rule already keeps such bundles safe. Whether a new style reuses a bundle or ships its own remains a curatorial decision, never inferred from a model name.

For `managed_local`, the style chooses the recipe and therefore the model bundle, graph, prompt mode, resolution, sampler, scheduler, steps, CFG, and style-specific positive/negative fragments. This is the deep decision layer the normal UI hides.

For `external_comfy` and `openai_images`, only `prompt` and `negative_prompt` participate. External mode uses the one checkpoint and shipped compatible graph selected in Advanced settings; cloud mode uses the configured provider model. Orb must not claim those backends reproduce the curated local look.

### `RecipeSpec`

```json
{
  "id": "anime-v1",
  "bundle_id": "curated-anime-v1",
  "workflow": "workflows/anime.v1.json",
  "workflow_sha256": "<64 lowercase hex characters>",
  "slots": {
    "positive": ["6", "text"],
    "negative": ["7", "text"],
    "seed": ["3", "seed"],
    "checkpoint": ["4", "ckpt_name"]
  },
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 28,
    "cfg": 5.0,
    "sampler": "euler",
    "scheduler": "normal"
  }
}
```

Slot maps use exact node ids and input names in a shipped API-format graph. There is no heuristic “first KSampler” fallback. Catalog validation loads each graph, checks all declared nodes/inputs, permits only nodes in the reviewed node allowlist (first-party in v1; a data-driven set the v2 identity feature extends with vetted nodes, never a hard-coded first-party assumption), and confirms one declared output node. Runtime compatibility is tied to the pinned ComfyUI version.

### `BundleSpec`

```json
{
  "id": "curated-anime-v1",
  "label": "Curated Anime v1",
  "requirements": {
    "download_bytes": 0,
    "disk_bytes": 0,
    "minimum_vram_mb": 0
  },
  "artifacts": [
    {
      "url": "https://source.example/exact-revision/model.safetensors",
      "sha256": "<64 lowercase hex characters>",
      "bytes": 0,
      "kind": "checkpoints",
      "filename": "orb-curated-anime-v1.safetensors"
    }
  ]
}
```

The example deliberately contains no claimed “best” model. Before release, the curator replaces it with exact reviewed artifacts and nonzero resource metadata. Only HTTPS, immutable/revision-pinned URLs and `safetensors` model artifacts are accepted automatically. Gated models, click-through licenses, credentials embedded in URLs, mutable “latest” URLs, and formats capable of loading pickled code are manual-install-only and cannot be marked one-click recommended.

Catalog updates must preserve old recipe and bundle ids while stored attachments reference them. A replacement gets a new id/version. Removing an obsolete entry requires a migration policy for replay metadata, not an in-place semantic change.

## Managed runtime

### Installation

**Decision: a version-pinned `comfy-cli` is the primary managed installer, not a hand-maintained per-platform PyTorch matrix.** The dominant cost and risk of managed local is cross-platform runtime install: accelerator-specific PyTorch wheels, Python compatibility, and ComfyUI's own dependency set all differ per `(os, architecture, accelerator)`. Re-deriving that logic per variant — pinned torch index URLs, per-platform argv — duplicates exactly what [`comfy-cli`](https://github.com/Comfy-Org/comfy-cli) (Comfy-Org, pip-installable) already does and maintains. v1 therefore drives installation through a version-pinned `comfy-cli` invoked with explicit non-interactive argv, and `runtimes.v1.json` shrinks to *pinning and verification* rather than *install logic*.

Trade-off, taken deliberately: this adds `comfy-cli` and its transitive dependencies as a supply-chain surface. It is installed into the sidecar's isolated environment, never Orb's interpreter, and its version is pinned and verified before use. This is accepted because the alternative — Orb owning platform torch-install logic — is a larger, less-tested surface the ComfyUI project does not expect downstreams to reimplement. Installation goes through `comfy-cli`; process supervision does not — the supervisor launches the installed ComfyUI entrypoint with explicit argv directly (not `comfy-cli launch`) so Orb owns the real child PID for the "terminate only the child it started" contract (see Supervision). `comfy-cli`'s remote/registry features are unused.

`runtimes.v1.json` contains one entry per tested `(os, architecture, accelerator)` variant with:

- pinned `comfy-cli` version plus the exact non-interactive argv used to install;
- pinned ComfyUI release/commit `comfy-cli` must resolve to (and archive SHA-256 for any variant that pins an archive instead of a VCS ref);
- required Python range;
- accelerator selector handed to `comfy-cli` (cpu / cuda / rocm / mps …), with a pinned PyTorch source only where a variant must override `comfy-cli`'s default;
- expected health/version information for post-install verification;

The installer performs preflight before mutation: supported variant, `comfy-cli` present at the pinned version (or installed into the isolated environment first), Python/runtime prerequisites, writable data root, free disk, and absence of another install job. It installs into a temporary version directory, verifies the result, starts it once, checks `/system_stats` plus a catalog smoke workflow, then atomically writes `active.json`. A failed install leaves the prior active runtime untouched and retains a bounded diagnostic log.

Do not use `shell=True`, install into Orb's interpreter, run arbitrary catalog commands, or modify a user-owned ComfyUI installation. Every `comfy-cli` invocation uses explicit pinned argv; it is an implementation helper, not a remote-management API or public contract.

### Supervision

Managed-local v1 has a single-Orb-process deployment contract. On the first managed runtime or bundle mutation, Orb takes an exclusive OS file lock at `runtime.lock` and retains it for the process lifetime. If another Orb process already owns the same imagegen data root, this process reports managed local as unavailable instead of starting a second sidecar or mutating shared files. External and cloud generation remain available. Inside the owner process, the supervisor is guarded by one async lifecycle lock:

1. Resolve the installed active runtime.
2. Choose an unused loopback port from a bounded range and retry if startup loses a port race.
3. Launch explicit argv with loopback-only listen, browser auto-launch disabled, no Manager flag, and a sanitized environment.
4. Poll `/system_stats` until healthy or the startup deadline expires.
5. Keep the child handle, resolved base URL, runtime version, and a bounded log tail in memory.
6. On Orb lifespan shutdown, terminate, wait, then kill only that recorded child if the graceful deadline expires.

Orb never proxies the ComfyUI UI to the LAN and never starts a sidecar on normal boot. The first managed generation may lazily start an already-installed runtime; installation always requires its own explicit user action. Sidecar startup failure degrades only image generation and cannot fail Orb startup.

The managed adapter serializes executions so timeout cancellation may safely call ComfyUI's process-wide `/interrupt`. The external adapter does not call `/interrupt` on timeout because a remote server may be shared with other clients.

## Download and install jobs

Runtime and bundle installs return `202` with a random job id. The frontend polls status; no new SSE contract is required.

Job states are `queued | preflight | downloading | verifying | installing | complete | cancelled | failed`. Snapshots include totals, completed bytes, current artifact label, and a sanitized error code/message. URLs containing credentials and raw subprocess output are never returned.

Rules:

- One mutating imagegen install/remove job runs at a time. Generation may continue only when it does not depend on files being changed.
- Check free disk against remaining download bytes, unpack/install overhead, and a safety margin before starting.
- Stream into `downloads/<sha256>.part`; cap bytes at the manifest size plus a small protocol tolerance.
- Resume with HTTP Range only when the server confirms the requested range; otherwise restart the part file.
- Hash while streaming and verify exact byte count and SHA-256 before moving.
- Move verified files atomically on the same filesystem into the catalog-derived model path.
- On cancellation or network failure, keep a valid bounded part for resume. On hash mismatch or oversize, quarantine/delete the corrupt part and fail closed.
- Refuse redirects to non-HTTPS or unapproved hosts, local/private addresses, and destinations outside the data root. The frontend sends only a runtime/bundle id, never a URL or path.
- Repair re-verifies every artifact and downloads only missing/corrupt files.

This subsystem is independent of the existing local-ML download route; migrating those smaller models onto it is a separate cleanup.

## Image-generation API

Add `backend/api/routes/image_gen.py` and register it in `backend/api/routes/__init__.py`. The router is a thin HTTP facade: it validates requests, calls only the public `workflows.image_gen.engine` facade, and translates domain errors into HTTP responses. It owns no catalog, download, process, or generation logic.

These operations cannot use the generic ON_DEMAND trigger. Runtime/model setup must work without an active conversation, must not construct an LLM client, and must not take conversation or character workflow locks. Keeping the FastAPI router in `api/` also prevents HTTP concerns from leaking down into the workflow engine.

All image-generation-specific global routes live below the workflow namespace:

```text
GET    /api/workflows/image_gen/status
GET    /api/workflows/image_gen/styles
POST   /api/workflows/image_gen/runtime/install             -> 202 job
POST   /api/workflows/image_gen/runtime/repair              -> 202 job
POST   /api/workflows/image_gen/runtime/start
POST   /api/workflows/image_gen/runtime/stop
DELETE /api/workflows/image_gen/runtime                     -> 202 job; runtime only, models retained
GET    /api/workflows/image_gen/bundles/{bundle_id}
POST   /api/workflows/image_gen/bundles/{bundle_id}/install -> 202 job
POST   /api/workflows/image_gen/bundles/{bundle_id}/repair  -> 202 job
DELETE /api/workflows/image_gen/bundles/{bundle_id}         -> 202 job
GET    /api/workflows/image_gen/jobs/{job_id}
DELETE /api/workflows/image_gen/jobs/{job_id}               # request cancellation
POST   /api/workflows/image_gen/connections/test
GET    /api/workflows/image_gen/external/models
```

`GET /status` returns runtime support/install/run/health state, detected device summary, backend capabilities, installed/corrupt/missing bundle status, active jobs, and only sanitized diagnostics. It never returns local absolute paths, environment values, API keys, or complete process command lines.

`POST /connections/test` validates either the saved source configuration or bounded unsaved overrides from the Advanced form without persisting them. `GET /external/models` uses the saved external-Comfy configuration and returns only sanitized model filenames from documented discovery routes; it never installs or uploads anything.

The existing generic framework routes remain authoritative for the workflow manifest, enablement, and global config, while generation/profile actions remain conversation-scoped:

```text
GET  /api/workflows
GET  /api/workflows/image_gen/config
PUT  /api/workflows/image_gen/config
POST /api/workflows/image_gen/enabled
POST /api/conversations/{cid}/workflows/image_gen/trigger
```

Frontend workflow modules call paths such as `api.get("/workflows/image_gen/status")`; `frontend/api.js` adds the `/api` prefix.

## Generation contracts

```python
@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    negative_prompt: str
    seed: int              # resolved 64-bit diffusion seed; see "Seed handling" below
    style_id: str
    recipe_id: str | None
    width: int | None
    height: int | None
    timeout_seconds: float

@dataclass(frozen=True)
class ImageResult:
    image_bytes: bytes
    mime: str
    backend_info: Mapping[str, Any]
```

`render.resolve_and_generate(config, request)` resolves the backend and returns both the bytes and a normalized replay record. Adapters expose capabilities honestly; optional behavior is never inferred from a model-name substring.

**Seed handling across the framework boundary.** `ImageRequest.seed` is the resolved diffusion seed actually submitted to the backend — a bounded integer in `[0, 2**64)`, ComfyUI's KSampler seed range. The framework, however, hands the reroll/rehydrate hooks a seed from `_generated_seed()` = `secrets.token_hex(16)`, i.e. a 32-char / 128-bit hex *string* (secondary-workflow.md §8.2). The hook folds that string into an int deterministically (`int(seed_hex, 16) % (2**64)`) before building the `ImageRequest`, and applies the same fold to a freshly generated random seed on the initial generate, so rerolled and fresh seeds occupy one integer space.

The initial generate (post-pipeline, on-demand) MUST write the diffusion seed it used into the attachment's dedicated `seed` column, as text. Rehydrate reads `att["seed"]` and re-folds it; without this write the row is silently unrehydratable. The seed lives only in that column — it is never duplicated into `generation_metadata`.

### Managed ComfyUI

- Require a healthy managed runtime and a fully verified style bundle.
- Load the immutable recipe graph, deep-copy it, and patch only declared slots.
- Submit `POST /prompt` with a random `client_id`, poll `GET /history/{prompt_id}`, then fetch only the declared output through `GET /view`.
- Treat prompt validation errors, `node_errors`, missing declared output, MIME mismatch, oversized response, and timeout as failures.
- Validate the returned bytes by signature and cap response size before persistence.
- The initial implementation may poll once per second. WebSocket progress and previews are deferred.

### External ComfyUI

- Test `/system_stats`, `/object_info`, and `/models/{folder}`.
- Advertise `can_install_curated_models=false` unconditionally.
- Use one shipped core-node graph selected in Advanced settings and one checkpoint filename selected from the server's discovered list.
- Apply the chosen style's prompt fragments only. Do not silently substitute a catalog checkpoint, upload a model, write remote userdata, or invoke Manager routes.
- Poll by `prompt_id`; on timeout stop polling without issuing the global `/interrupt`.
- An external server with incompatible/missing first-party nodes or graph inputs fails connection validation with an actionable message.

### Cloud Images API

- Keep provider base URL, API key, and model in advanced workflow config.
- Append the selected `StyleSpec.prompt` to the composed positive prompt and, only when supported by that adapter, its negative prompt.
- Implement the exact documented request/response contract for the selected provider. The first adapter may target the OpenAI Images shape, but “OpenAI-compatible” is not treated as a guarantee that sizes, response encoding, seed, negative prompt, or model discovery behave identically.
- Reroll is a fresh provider generation when seed control is unavailable. Rehydrate is best effort, never described as deterministic.

## Workflow config and profile

No schema migration is required. Global settings live in `settings.workflow_config.image_gen`; machine/runtime/model installation state stays on disk.

```json
{
  "source": "managed_local",
  "default_style": "realistic",
  "auto_generate": false,
  "timeout_seconds": 180,
  "external_comfy": {
    "api_url": "http://127.0.0.1:8188",
    "api_key": "",
    "checkpoint": "",
    "workflow": "external_core_v1"
  },
  "openai_images": {
    "api_url": "https://api.openai.com",
    "api_key": "",
    "model": ""
  }
}
```

Every hook calls `normalize_config` because workflow `config_schema` is UI metadata, not enforcement. Validation includes the source/style enums, bounded strings, HTTP(S) URLs, timeout range, and source-specific required fields. The normalizer drops unknown keys and returns a new canonical dict.

Secrets remain only in live workflow config and are read at call time. They never enter attachment metadata, job snapshots, logs, subprocess argv, or catalog files.

**Preset export needs a new nested-JSON scrubber; the existing secret protections do not reach this case.** Orb's preset secret machinery is column-granular and column-*name*-driven: `_scrub_configs` blanks whole columns listed in `SECRET_COLUMNS` (`backend/features/presets/engine.py`), and the `SENSITIVE_*` tripwire that forces coverage suffix-matches column *names* (`backend/database/preset_schema.py`). Both API keys live nested inside the `settings.workflow_config` JSON column, whose name matches no secret suffix — so the tripwire never flags them and no existing scrubber touches them. Therefore:

- Add a JSON-path scrub of `workflow_config.image_gen.external_comfy.api_key` and `.openai_images.api_key` to the configs export path (`_scrub_configs`), keyed off the workflow-config shape rather than a column name.
- The `SECRET_COLUMNS` coverage test (`tests/integration/test_preset_schema_coverage.py`) is no backstop here; correctness rests entirely on dedicated canary tests.
- Precedent confirms the blind spot: TTS already stores `api_key` inside `character_cards.workflow_state` (`backend/workflows/tts/synth.py`), uncovered by the same mechanism. Do not assume the framework scrubs workflow JSON secrets — it does not.

Tests seed unique canaries into both keys and assert they disappear when `configs` is omitted and when `strip_keys=true`, and that a deliberate `strip_keys=false` export retains them.

Per-character `workflow_character_state` is deliberately small:

```json
{
  "appearance_prompt": "",
  "negative_prompt": "",
  "reference_image": null,
  "face_seed": null
}
```

Style is not character state. `reference_image` and `face_seed` are reserved for the v2 character-identity feature (see "Forward compatibility: character identity consistency (v2)"): they are `null` and unused in v1, but the normalizer preserves them so a future write survives a v1 round-trip. Character-specific LoRAs and reference-portrait generation are deferred.

## Prompt composition

Declare one standalone `ToolSpec`; it remains outside the Director/Writer/Editor tool union and therefore does not change their shared tool-schema prefix.

```python
COMPOSE_TOOL_SCHEMA = {"type": "function", "function": {
    "name": "compose_image_prompt",
    "description": "Describe the current visible moment without choosing an art style.",
    "parameters": {"type": "object", "properties": {
        "scene": {
            "type": "string",
            "description": "Concise concrete visual description: subjects, setting, lighting, pose, expression, clothing, and framing. No art-style or quality terms."
        },
        "avoid": {
            "type": ["string", "null"],
            "description": "Optional visible elements that must not appear."
        }
    }, "required": ["scene", "avoid"], "additionalProperties": false}
}}
```

The forced call uses the writer lane exposed by the hook contexts. POST_PIPELINE passes the existing prefix and cache tracker but does not promise byte-identical Writer prefix reuse because the standalone tool and tail differ. On-demand/regenerate build a short standalone transcript ending at the anchor assistant message.

Composition is deterministic after the forced call:

```text
managed positive = recipe/style positive + character appearance + scene
managed negative = recipe/style negative + character negative + avoid

external/cloud positive = character appearance + scene + selected style prompt
external/cloud negative = character negative + avoid + selected style negative
```

Segments are whitespace-normalized, individually length-bounded, and joined without attempting semantic de-duplication. The style is always supplied by the trusted catalog, never by the LLM. If the forced call fails, use a bounded plain-text excerpt of the anchor assistant reply as `scene`; if both are empty, fail without spending inference resources.

## Hooks and artifact behavior

Declare `image_gen_workflow` with `produces_artifacts=True` and bind POST_PIPELINE, ON_DEMAND, REGENERATE, and REROLL_GEN in `backend/workflows/__init__.py` before `finalize_registry()`.

### On demand

`POST .../workflows/image_gen/trigger` action `generate` accepts `{message_id, style_id}`. Validate that `message_id` is an integer but not a boolean and that `style_id` is a live catalog id. Compose from the message, resolve the selected style/source, generate, and insert one workflow attachment.

The only other ON_DEMAND actions are `get_profile` and `set_profile`, because they need the active conversation's character context. Global readiness, connection tests, model discovery, and runtime/model mutation use the dedicated `/api/workflows/image_gen/...` routes and never take the conversation lock held by the workflow trigger.

### Post pipeline

When `auto_generate=false`, do nothing. When enabled, use `default_style`; never install missing dependencies. Yield phase events for prompt composition and generation. Ordinary failure logs a sanitized warning and ends the workflow phase without aborting or replacing the assistant response; cancellation propagates.

This remains a blocking post-pipeline hook: assistant persistence and `done` wait for generation while workflow locks are held. The default is off, the UI labels the latency tradeoff, and `timeout_seconds` bounds it. True post-persistence jobs require a framework message-id handoff and remain deferred.

### Regenerate, reroll, and rehydrate

- Regenerate recomposes the scene from the anchor message under the currently selected style and source, creating a sibling artifact.
- Reroll uses stored resolved prompt/recipe/model metadata with a fresh seed — the route-supplied `_generated_seed()` hex string, folded to a 64-bit int (see "Seed handling").
- Rehydrate uses the stored metadata and the stored `seed` column value, re-folded to the same int, when the backend supports seeding.
- Managed replay requires the recorded recipe and exact bundle artifacts. If they are unavailable, fail with a sanitized actionable error; never silently switch to a newer recommended model.
- Cloud/external replay is best effort. A seed and recipe are evidence of the request, not a guarantee of identical bytes.

Store in `generation_metadata`:

```text
source, style_id, catalog_version, recipe_id, workflow_sha256,
bundle_id, artifact_sha256s, runtime_version, backend_model,
prompt, negative_prompt, width, height, steps, cfg, sampler, scheduler
```

Only fields applicable to the resolved backend are populated. Never store API keys, managed/external URLs containing credentials, local paths, or raw provider responses. `consumption_metadata` contains the display-safe style label, prompt, negative prompt, and source.

## Frontend

All files under `frontend/workflows/image_gen/` import only `/static/workflow_api.js` plus their own relative modules.

### Normal flow

The assistant-message Visualize button opens a minimal modal:

1. Style dropdown (`Realistic`, `Anime`, `Pixel art`, `Scenery`, `Line art`).
2. One primary action: Generate.

In managed-local mode, if setup is incomplete, the same modal replaces Generate with exactly one relevant action:

- `Set up local image generation` when the runtime is absent;
- `Download <style> model (<size>)` when its bundle is absent;
- `Repair installation` when files are corrupt;
- a clear unsupported-device message when no runtime variant matches.

After a job starts, show determinate byte progress when total size is known, Cancel, and a retryable failure message. Poll job status and restore Generate only after status re-verifies readiness. Do not show raw logs, paths, graph/model controls, or a misleading Generate button while prerequisites are missing.

### Tools panel / advanced settings

The normal card shows source, default style, readiness, disk usage, and a Settings button. Advanced settings contain:

- source selector (`Managed local`, `External ComfyUI`, `Cloud Images API`);
- auto-generate toggle and timeout;
- installed bundles with size/source/remove actions;
- managed runtime status/start/stop/repair/remove and sanitized log tail;
- external URL/key/checkpoint and connection test;
- cloud URL/key/model and connection test;
- per-character appearance/negative prompts.

Raw sampler/CFG/steps/dimensions and model overrides are not exposed for managed recipes. Catalog curation happens in version-controlled resources and tests, not through end-user settings.

The attachment renderer extends `ctx.defaultHtml` so the framework keeps image display and sibling controls. Every prompt/style/provider string passes through `esc()` or `escAttr()` before interpolation.

## Security boundaries

- The managed server listens only on loopback and is not reverse-proxied through Orb.
- No ComfyUI-Manager and no community custom nodes in v1.
- No frontend-supplied download URL, filename, destination, command, graph, or node id.
- Catalog files are trusted release inputs but are still schema/path/hash validated.
- Subprocesses use explicit argv, sanitized environment, dedicated cwd, deadlines, and captured bounded logs.
- HTTP downloads reject local/private targets and unsafe redirects; model files are verified before ComfyUI can see them.
- External endpoints are explicit advanced user configuration. External API keys are sent only to that configured origin and redacted from logs.
- Generated images have signature/MIME and byte-size checks before being stored.
- Runtime/model removal requires explicit confirmation in the UI and targets only resolved catalog-owned paths under the imagegen data root. Removing the runtime retains the model store.

## Test plan

### Unit

- Catalog schema, referential integrity, stable ids, URL policy, exact sizes/hashes, path containment, node allowlist, graph slots, and workflow hash.
- Runtime variant selection and unsupported hardware behavior.
- Download resume/restart semantics, progress math, cancellation, oversize, hash mismatch, unsafe redirect, disk-space failure, and atomic install.
- Bundle shared-artifact removal and protection of unknown/manual files.
- Supervisor argv/env, loopback binding, health deadline, crash state, port retry, log redaction, and shutdown escalation against a fake child.
- Comfy queue/history/declared-output/view sequence, malformed/error payloads, timeout, output size/signature, and managed versus external interrupt behavior.
- Config/profile normalization and nested-secret exclusion.
- Character-state normalization preserves the reserved `reference_image`/`face_seed` keys and their defaults (v2 identity forward-compat guard).
- Style resolution proves managed style selects its recipe/bundle/params while external/cloud styles change prompt only.
- Composer schema/choice parity, segment order/bounds, style isolation, and anchor-text fallback.
- Seed fold from the framework hex string to a bounded 64-bit int is deterministic; the initial generate persists the `seed` column and rehydrate re-folds the stored value.
- Installer builds pinned `comfy-cli` argv (never shell text) and verifies the pinned `comfy-cli` version before invoking it.

### Integration

- Base Orb boots and all non-image features work without ComfyUI, PyTorch, model files, or optional installer dependencies.
- Runtime/model install routes return jobs without holding request connections; restart reconstructs verified status.
- No install starts from boot, status, conversation open, Visualize, or auto-generate.
- Managed generation refuses missing/corrupt prerequisites before invoking ComfyUI.
- Auto-off yields nothing; auto-on phases and attachment persistence work; failures do not lose the assistant response; cancellation propagates.
- On-demand guards, profile round-trip, regenerate sibling shape, reroll seed change, and rehydrate request replay.
- Exact stored recipe remains required after catalog recommendation changes.
- External discovery/generation never calls install, userdata-write, Manager, or interrupt endpoints.
- Cloud adapter receives the selected style prompt and never receives a local recipe/model path.
- Preset canaries prove the bespoke JSON-path scrubber removes both nested API keys when configs are omitted or `strip_keys=true`, and retains them under a deliberate `strip_keys=false` export (the `SECRET_COLUMNS` coverage test does not reach this path).
- App lifespan terminates only the sidecar process it started.

### Frontend

- The normal modal has only style plus the correct primary action for every readiness state.
- Runtime/bundle progress, cancellation, failure, repair, and unsupported states remain usable after rerender.
- Styles are catalog-driven but preserve stable labels/order.
- No advanced engine fields leak into managed normal flow.
- Prompt/style/error/log payloads containing HTML, quotes, and handler-shaped strings are escaped.
- Existing default image renderer and regenerate/reroll controls remain intact.

### Manual release matrix

For every runtime variant advertised by `runtimes.v1.json`:

1. Test clean install, cancellation, resume, repair, start, generation for all five styles, stop, restart, explicit runtime upgrade/removal, and bundle removal.
2. Record runtime/model download size, disk peak, startup time, first-generation latency, steady latency, and peak RAM/VRAM.
3. Verify no service listens beyond loopback and no Manager/custom-node route is enabled.
4. Review output quality with fixed prompts/seeds before marking a bundle recommended.
5. Complete source/notices review for ComfyUI, runtime dependencies, and every distributed/recommended model artifact.

An untested platform/accelerator combination is reported as unsupported; it is never inferred to work from a nearby variant.

## Implementation sequence

1. **Contracts and release inputs**: add catalog/runtime schemas, strict loader, five style ids, placeholder test bundles, graph-slot validator, path policy, and catalog tests. Production catalog entries remain unavailable until real artifact metadata and review are complete.
2. **Jobs and downloads**: implement background job registry, status/cancel API, disk preflight, secure resumable downloader, verified atomic installs, repair/remove, and tests.
3. **Managed runtime**: implement platform variant resolver, isolated `comfy-cli`-driven installer (pinned version + argv), generated extra-model paths, supervisor, lifespan shutdown, fake-process tests, then certify the first real runtime variant.
4. **Comfy generation core**: implement documented Comfy client, strict graph patching, managed/external adapters, output validation, metadata, and fake-server tests.
5. **Workflow integration**: implement config/profile/composer/hooks, register the workflow, add nested preset-secret scrubbing, and complete integration tests. At this point managed generation works through existing default image artifacts.
6. **Simple frontend**: implement style/generate modal, readiness-driven setup/download flow, tools panel, attachment caption, progress polling, and escaping/state tests.
7. **Cloud adapter**: implement one exact provider contract, prompt-only style injection, credential handling, and provider-shaped tests. Do not generalize until a second provider demonstrates the actual common surface.
8. **External ComfyUI**: add advanced endpoint/model discovery and generation-only flow, with capability/UI tests proving install is unavailable.
9. **Release hardening**: certify runtime variants and curated bundles, update `AGENTS.md` and `docs/architecture/secondary-workflow.md`, run `./scripts/lint.sh` and `./scripts/tests.sh all`, then execute the manual release matrix.

## Release blockers

The feature is not ready to advertise as fool-proof managed local generation until all are true:

- At least one runtime variant has a pinned, reproducible, clean-machine install and full manual matrix result.
- Each visible managed style points to a real reviewed bundle with immutable sources, exact byte sizes, SHA-256 hashes, hardware guidance, and a passing shipped workflow.
- Runtime and model downloads are cancellable, resumable, verified, and recover safely across process restart.
- Managed ComfyUI is loopback-only, uses no Manager/custom nodes, and cannot mutate paths outside its data root.
- GPL/runtime/model licensing review is complete for the actual distribution method and catalog artifacts.
- The normal UI contains no expert engine parameter and never offers an action Orb cannot fulfill.
