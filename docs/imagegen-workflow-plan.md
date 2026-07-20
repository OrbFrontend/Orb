# Plan: Managed Image Generation Secondary Workflow

## Decision

Orb will ship image generation as an `image_gen` secondary workflow, **generated on demand only**, with two deliberately unequal execution modes:

| Mode | Generation | Model discovery | Orb model installation | Intended user |
|---|---|---|---|---|
| `managed_local` | Orb-managed, headless ComfyUI sidecar | Yes | Yes, from Orb's curated catalog | Default local experience |
| `external_comfy` | User-supplied ComfyUI HTTP endpoint | Yes | No | Advanced users with an existing engine |

The managed sidecar is the only mode allowed to promise one-click setup, curated model downloads, or automatic model selection. External ComfyUI is generation-only unless a future authenticated Orb companion service is installed on the remote host.

**No hosted-provider adapter.** A cloud Images API is a poor fit for this product: character-driven scene generation runs headlong into provider moderation for a large share of Orb's actual use, so the adapter would fail in exactly the cases users want it. It also adds a credential surface, a per-provider response contract, and a second "your prompt left this machine" story to a feature that is otherwise entirely local. If demand appears later, the adapter boundary below is where it attaches.

**Every image is produced by an explicit user action.** There is no automatic per-turn generation. A blocking post-pipeline hook would make assistant persistence wait tens of seconds on a render while workflow locks are held, and the per-message Visualize action produces the same image with none of that cost. Automatic generation becomes reconsiderable only once the framework supports post-persistence work with a message-id handoff; until then it is not worth the latency it imposes on every turn.

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

- The normal generation UI is a style dropdown and a Generate button. Initial stable style ids are `realistic` and `anime`; catalog updates may add styles but never silently repurpose an existing id.
- Generation happens only when the user asks for it, per message. No image is produced as a side effect of a turn completing.
- The user explicitly opts into every runtime or model download. No multi-gigabyte download starts because a conversation was opened or a generation button was pressed.
- In managed-local mode, selecting a style selects its entire curated recipe. The normal UI never exposes checkpoints, VAEs, graph nodes, samplers, CFG, steps, or LoRAs.
- If the selected local recipe is not installed, the UI reports the exact download size and source links and offers one Download action. Generation stays disabled until every artifact verifies successfully.
- In external mode, style prompt text is applied, but the configured remote model remains in control.
- The LLM only describes the visual scene through standalone forced calls. It never chooses the backend, model, workflow, sampler, dimensions, or style.

## Facts that constrain the implementation

- ComfyUI model files are commonly multiple gigabytes and may consist of a checkpoint plus encoders, VAE, LoRAs, or other companion weights. A request-long `hf_hub_download` call is not an adequate product download manager. See [ComfyUI model documentation](https://docs.comfy.org/development/core-concepts/models).
- ComfyUI officially supports model directories outside its installation through `extra_model_paths.yaml`. The managed sidecar will read Orb's model store this way; Orb will not copy weights into the runtime tree. See [extra model paths](https://docs.comfy.org/development/core-concepts/models#adding-extra-model-paths).
- ComfyUI needs an isolated environment because its dependencies may conflict with the host application. GPU/PyTorch installation differs by platform and accelerator. See [manual installation](https://docs.comfy.org/installation/manual_install) and [system requirements](https://docs.comfy.org/installation/system_requirements).
- There is no official universal ComfyUI Docker image, so a container is not the default sidecar mechanism. The managed installer is platform-manifest driven and must show “unsupported” when no tested runtime variant matches.
- “Best model for a style” is a curatorial decision that changes over time. Code consumes a versioned catalog; it must never hard-code marketing claims or infer quality from a filename. A bundle becomes recommended only after its exact revision, hashes, resource requirements, workflow, and output quality have been reviewed.

## Feasibility probe (2026-07-20)

Measured against a live loopback ComfyUI install using a saved `orb-test` project. Every claim below was executed, not read from docs.

**Probe environment.** ComfyUI 0.22.0, frontend 1.44.19, Python 3.12.3, PyTorch 2.6.0+cu124, RTX 3090 (24 GB), launched `main.py --listen 127.0.0.1 --port 8188 --enable-manager`. 20 custom-node packs installed; `/object_info` reports **1953 node types**. This is a realistic "advanced user's existing engine", i.e. exactly the `external_comfy` target.

### The HTTP contract works as specified

`POST /prompt {prompt, client_id}` → `200 {prompt_id, number, node_errors}`; poll `GET /history/{prompt_id}` until `status.completed`; fetch the declared output node's image via `GET /view?filename=&subfolder=&type=output`. Returned bytes carried a correct `image/png` `Content-Type` and PNG signature. The plan's queue/history/view/discovery sequence needs no change.

`GET /models` returns 47 folder kinds; `GET /models/{folder}` returns filenames. **Folder names are not guessable:** `/models/clip` was empty while the project's `CLIPLoader` loads from `text_encoders`. External model discovery must enumerate the folder the selected graph's loader actually reads, never a hard-coded `clip`/`checkpoints` assumption.

### Saved projects are UI format; there is no server-side conversion

`orb-test.json` is a **UI graph** (`nodes`/`links`/`groups`/`extra`), not the API format `/prompt` accepts. UI→API conversion lives in the frontend JavaScript and is exposed by **no server route**. Consequences:

- Orb cannot ingest a user's saved ComfyUI project. Shipped API-format graphs, as already planned, are the only workable input — this is now a verified constraint rather than a preference.
- **Every ComfyUI-generated PNG embeds the API-format graph in a `prompt` tEXt chunk** (plus the UI graph in `workflow`). This is the practical authoring route: generate once in the UI, read the API graph straight out of the output PNG. Hand-converting `widgets_values` positionally against `/object_info` also works but is fragile and unnecessary. Verified by replaying an extracted graph and reproducing its source image **pixel-for-pixel**.
- The real project contained a **dead node** (`CLIPSetLastLayer`, fed by the checkpoint's CLIP, output consumed by nothing — the text encoders read from a separate `CLIPLoader`). Hand-authored graphs carry cruft; catalog validation should reject or prune unreachable nodes rather than assume clean input.

The converted graph ran unmodified, so the conversion is mechanical — just not automatable against a live server.

### Validation is structured, pre-execution, and leaks paths

All malformed graphs were rejected with **HTTP 400 before any GPU work**, carrying a usable taxonomy in `error.type`:

| Sent | `error.type` |
|---|---|
| Nonexistent checkpoint filename | `prompt_outputs_failed_validation` + node error `value_not_in_list` |
| `steps: 99999` | `value_bigger_than_max` (max 10000) |
| Unknown sampler enum | `value_not_in_list` |
| Link to a missing node id | `exception_during_inner_validation` |
| Unknown `class_type` | `missing_node_type` |
| Graph with no output node | `prompt_no_outputs` |

This is a cheap, exact preflight — a catalog graph can be validated against a specific server before spending a render.

**`exception_during_inner_validation` embeds a Python traceback containing absolute server paths** (`<comfyui-install-dir>/execution.py` and similar). The security rule against returning local paths is therefore load-bearing on this exact path: `node_errors` must never be relayed to the frontend verbatim. Extract `type` and `input_name`; drop `traceback`, `exception_message`, and `input_config`.

### Generated PNGs carry the full prompt as metadata — strip it

The same `prompt`/`workflow` tEXt chunks that make authoring easy are a **privacy leak in the stored artifact**. If Orb persists ComfyUI's PNG bytes verbatim into `workflow_attachments`, every stored image embeds the complete prompt, negative prompt, model filenames, and graph. A user who exports or shares an image ships their character's full prompt text with it — including anything NSFW — with no indication that it is there.

Orb must re-encode or strip metadata before persisting. This composes well with the size finding above: the re-encode that drops the chunks is the same pass that converts to WebP.

### The composer's output format is a real design risk

The measured quality gap in this probe was caused entirely by prompt *form*, not by the graph. Substituting a 13-tag generic prompt for the project's 45-tag one, on an identical graph and comparable seed, produced visibly washed-out, low-contrast, mushy output; the original prompt on the same pipeline produced crisp, high-contrast, coherent results.

This matters because `compose_image_prompt` currently asks the LLM for a *"concise concrete visual description"* — natural-language prose. Local checkpoints are tag-trained and respond to **comma-separated tags**, with quality scaling on tag density and on explicit quality tags (`best quality`, `very aesthetic`, `high contrast`, score tags). Feeding them prose reproduces exactly the mediocre output measured here, and no amount of recipe-side catalog curation compensates for it.

**The composer therefore emits tags, always.** Prose only works on cloud models, and the plan already rules out a hosted-provider adapter — so prose has no consumer in Orb and no format switch is needed. `compose_image_prompt` asks for comma-separated tags, the recipe supplies its quality-tag prefix, and the segment joiner is already comma-based. If a hosted adapter ever appears, it arrives with its own composer concern; adding a `prompt_format` enum now would be a config value with exactly one reachable branch.

The tool description below is written accordingly. `analyze_scene` renders to tags for the same reason.

### Reproducibility, and the cache trap it hides

Re-submitting the identical graph with identical seeds produced a **byte-identical PNG** (same SHA-256). Good for the replay contract — but the second run completed in **2.0 s**, was fully cache-hit, wrote **no new file**, and returned the **same `filename` as the first run**. So:

- Output filenames are not per-submission identities; Orb must key on `prompt_id`, never on filename.
- A reroll that fails to actually change the seed **silently returns the previous image in ~2 s** instead of erroring. The seed-fold round-trip test in the test plan is guarding a real, silent failure mode — worth asserting that a reroll's bytes differ from its parent's.

### Latency is ~55 s per image, not "tens of seconds"

Same 3090, the project's two-stage graph (960×1536, 8 steps → 4× UltraSharp upscale → downscale 1472×2304 → 10 steps at 0.3 denoise):

- cold (loaders uncached): **50.1 s**
- warm, fresh seed: **55.1 s**

Model loading is not the cost; sampling is. A simpler single-stage recipe will be faster, but this is the order of magnitude a quality recipe lands at on good consumer hardware. **This decisively confirms the no-`POST_PIPELINE` decision** — a blocking per-turn hook would add ~a minute to every turn while holding workflow locks.

### Stored size is ~3× the plan's estimate at this resolution

The 1472×2304 output was **4,690,975 bytes** of PNG → **6,254,636 bytes base64**. The plan estimates ~2 MB per stored row at 1024×1024; at a quality recipe's real resolution it is over 6 MB. A hundred images is then a ~600 MB conversation database, on the file every conversation read touches.

Recipe dimensions are therefore not merely "chosen with stored size as a constraint" — they are the dominant term. Two mitigations are worth deciding before the catalog is fixed: cap recipe output resolution nearer 1024², and consider storing WebP/JPEG rather than PNG for the attachment (the render stays PNG; only the stored copy is re-encoded). Neither needs new infrastructure.

### Interrupt is indistinguishable from failure

`POST /interrupt` returned `200` with an **empty body**, and the running job landed in history as `status_str: "error"`, `completed: false`, `outputs: {}` — identical in shape to a genuine execution failure. The single-error-funnel decision is validated, but the corollary is that **Orb must remember locally that it issued the interrupt**; the server will not tell it apart from a crash.

### Two external-mode facts the plan should absorb

- **`GET /queue` returns the full prompt graph of running and pending jobs to any client**, including all prompt text. On a shared external server, character appearance and scene descriptions are readable by anyone who can reach it. This belongs in the external-mode privacy warning, alongside "your prompt left this machine".
- **Outputs accumulate on the server's disk** (33 files in `output/` here) and there is no cleanup route. External mode permanently leaves generated images on the remote host. Worth stating in the UI; not something Orb can fix.

`GET /view` **is** correctly path-contained — `../../main.py`, its URL-encoded form, and a `subfolder=../..` traversal returned 400/400/403. No sanitization needed on Orb's side there.

### Node allowlist: validate the graph, not the server

With 1953 node types available from 20 installed packs, an external server is a strict **superset** of what any Orb graph needs. Allowlist validation must therefore be "this graph references only allowlisted nodes", never "this server offers only allowlisted nodes" — the latter fails against every real installation.

Incidentally, this install already carries `comfyui-impact-pack`, `comfyui-impact-subpack`, and `comfyui_controlnet_aux` — precisely the detector/detailer stack the v2 identity section describes. Advanced users' servers will commonly already have it, which strengthens the case for v2 identity being additive rather than a rewrite.

### Net assessment

Nothing in the probe invalidates the design. Six items change, the last being the only one that touches a contract the plan wants frozen:

1. Sanitize `node_errors` — it leaks absolute paths today.
2. Re-examine recipe resolution and stored-image encoding; 6 MB/row is the real number.
3. Strip PNG metadata before persisting — ComfyUI embeds the full prompt and graph in every output.
4. Track interrupt state locally; the server reports it as an error.
5. Document that external mode exposes prompts via `/queue` and leaves files on the remote disk.
6. **The composer emits comma-separated tags, not prose.** Tag density was the single largest observed quality factor. No format switch — prose only suits cloud models and the plan ships no hosted adapter.

## Scope

### v1

- Managed local ComfyUI install/start/stop/health/log lifecycle.
- Curated, opt-in model-bundle download/remove/repair with progress, cancellation, resume, disk checks, and SHA-256 verification.
- Two styles: realistic and anime. Each is a full curatorial commitment — reviewed bundle, exact hashes, hardware guidance, passing shipped workflow, fixed-seed quality review — so the count is set by how many of those Orb can actually stand behind at release, not by how many sound good in a dropdown. Further styles are additive catalog versions.
- First-party ComfyUI nodes only; shipped API-format workflows only.
- Text-to-image, one image per request.
- Per-message Visualize action. On demand only.
- External ComfyUI generation against a user-configured checkpoint.
- Existing `workflow_attachments` storage, sibling reroll/regenerate behavior, and default `image/*` renderer.

### Not v1

- ComfyUI-Manager, arbitrary custom nodes, arbitrary workflow upload, arbitrary model URLs, or a model marketplace.
- Remote model installation. A future remote-management option requires an authenticated companion daemon with an allowlisted catalog and filesystem sandbox; it is not implemented through undocumented Manager routes.
- Forge/A1111 adapters. They add another lifecycle/model-selection contract without improving the managed default.
- Live remote catalog updates. The catalog ships with Orb releases; signed catalog updates can be designed later.
- img2img, inpainting, ControlNet, IPAdapter/FaceID, character LoRAs, expression-pack generation, batches, or live previews. These reappear as the building blocks of the planned v2 character-identity feature; v1 ships none of them but reserves its extension points (see "Forward compatibility: character identity consistency (v2)").
- Automatic per-turn generation. Deferred until the framework can run work after assistant persistence rather than inside the turn.
- A hosted-provider (cloud Images API) adapter.
- Additional styles beyond realistic and anime — pixel art, scenery, line art and the rest are catalog additions, each gated on its own bundle review.
- Automatic runtime/model updates. Upgrades are explicit and compatibility-checked.
- Universal hardware support. Only runtime variants exercised by the release matrix are offered.
- Byte-identical replay across GPU, driver, PyTorch, ComfyUI, model, or workflow changes.

## Forward compatibility: character identity consistency (v2)

v1 deliberately ships no character-identity mechanism: a generic prompt (“blue eyes, brown hair, red lips”) yields a different face every generation, so the user feels like they are talking to a new character each turn. A v2 **character identity** feature will address this without per-user asset wrangling: a canonical **reference portrait** is generated once from the character's appearance (locked seed, portrait framing), stored as character state, and used to condition every subsequent generation. The default identity mechanism is **IPAdapter-plus-face** (CLIP-vision, style-agnostic so it works on both anime and realistic recipes), with **InstantID** (InsightFace) as a realistic-only upgrade and a trained **character LoRA** as the opt-in gold standard. Because Orb is a non-commercial, local-only, open-source tool, InsightFace's non-commercial model license and curated non-first-party nodes are acceptable here.

### The face-region detailer is load-bearing, not cosmetic

Identity conditioning applied only at the base pass is weak for the framing this feature actually produces. At recipe resolutions a mid-shot or wider composition gives the face a few hundred pixels at most, and both IPAdapter and InstantID have correspondingly little to bind to; the identity washes out precisely in the shots where a conversation partner is usually depicted. The fix is to re-diffuse the face region at full resolution **and attach the identity conditioning to that pass**, which is what makes a face-region detailer a prerequisite for identity rather than a quality nicety.

Three distinct jobs are involved and must not be conflated in the catalog or the node allowlist:

| Stage | Component | Role |
|---|---|---|
| Locate | a bbox detector provider plus a face detection model | Finds the face in the **generated** image. Contributes nothing to identity. |
| Re-render | a face-detailer node (crop → upscale → partial denoise → masked composite) | Produces a full-resolution face region. Improves fidelity; does **not** by itself make the face the same face, and its extra sampling pass adds its own variance. |
| Identify | IPAdapter-plus-face / InstantID / character LoRA | Supplies *whose* face, conditioned on the stored reference portrait. |

Only the third stage carries identity. The first two exist so the third operates at a resolution where it is effective.

InstantID additionally needs InsightFace to embed the face from the **reference portrait** and to supply keypoints for its ControlNet. That is a different model at a different stage from the generated-image bbox detector, and the two are not interchangeable — the InstantID path needs both.

Consequences for v1's reserved extension points, all of which the existing forward-compatibility decisions already cover:

- Detector and identity weights are new model-store kinds and new reviewed catalog bundles. A face bbox model is on the order of tens of megabytes, negligible beside a checkpoint, so the download cost of the locate stage is not a design constraint.
- Detector, detailer, and identity nodes are curated non-first-party nodes, delivered as pinned node bundles through the same download/verify/atomic-install primitive as model bundles, and admitted by extending the reviewed node allowlist.
- The graph becomes multi-stage (base → detect → identity-inpaint → composite), which the slot map already supports because it patches arbitrary declared nodes.

**Licensing must be resolved before any of this is advertised.** The common face-detection models are YOLO-architecture and the dominant implementation is AGPL-3.0; the widely used detailer node packs are GPL-3.0. Both run inside the sidecar process rather than Orb's, which affects the analysis but does not settle it, and neither belongs in a recommended bundle before review completes. A non-AGPL face-mesh detector is the fallback if that review goes badly. This is an explicit input to the existing licensing release blocker.

**v1 consequence, stated up front rather than discovered in the release matrix:** because v1 ships first-party nodes only, it has no detailer stage, so managed-local faces at non-portrait framing will be visibly softer than what a user gets from a hand-built ComfyUI graph. This is an accepted v1 limitation, not a bug to triage.

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
  composer.py                 # forced-call schemas and prompt assembly
  hooks.py                    # on-demand/regenerate/reroll integration
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
    resources/
      catalog.v1.json         # styles, recipes, bundles, exact artifacts
      runtimes.v1.json        # tested runtime variants and pinned sources/hashes
      workflows/
        realistic.v1.json
        anime.v1.json

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

### Generated images grow the conversation database

`workflow_attachments.data_b64` is a `TEXT` column: attachment bytes live base64-encoded inside the main SQLite database, not on disk. Base64 adds a third again on top of the encoded image, and reroll and regenerate retain siblings rather than replacing them, so a single message can hold several. A 1024×1024 illustration PNG lands around 1.5 MB, so roughly 2 MB per stored image; a 1536×1152 one is about double that. A few hundred images is therefore a multi-hundred-megabyte conversation database, and it is the same file every conversation read touches. **Measured: a 1472×2304 PNG from a real recipe was 4.7 MB → 6.25 MB base64, roughly 3× the estimate above (see "Feasibility probe").**

v1 accepts this rather than building a blob store, because the existing attachment storage, sibling tree, and default `image/*` renderer are the entire reason no schema migration is needed. Three things keep it from becoming pathological:

- Recipe default dimensions are chosen with stored size as a real constraint, not only for output quality.
- The response-size cap that already guards the fetched bytes doubles as the per-row bound.
- Growth is surfaced in the tools panel next to model disk usage, so it is visible before it is a problem.

Moving attachment bytes to disk under the imagegen data root, with the row carrying a reference, is the upgrade path. It is a framework-wide change affecting TTS equally, so it belongs to whichever feature first measures real pain — not to this one speculatively.

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

The two initial ids are stable UI concepts, not model names:

- `realistic`: photographic or cinematic realism.
- `anime`: drawn anime/manga character illustration.

These two are shipped because they cover the overwhelming majority of character-conversation imagery and because two is the number of bundles that can realistically clear full curatorial review — exact revision, hashes, resource requirements, shipped workflow, and fixed-seed output review — for a first release. Adding a style is not a dropdown entry; it is another bundle carrying all of that.

Later styles (pixel art, scenery, line art, and anything else) arrive as additive catalog versions with new ids, which the versioning rule already supports without touching stored attachments. A new recipe may reuse an already-installed curated bundle via a shared `bundle_id` where the curator judges an existing checkpoint suitable, avoiding a second multi-gigabyte download; the shared-artifact removal rule already keeps such bundles safe. Whether a new style reuses a bundle or ships its own remains a curatorial decision, never inferred from a model name.

For `managed_local`, the style chooses the recipe and therefore the model bundle, graph, prompt mode, resolution, sampler, scheduler, steps, CFG, and style-specific positive/negative fragments. This is the deep decision layer the normal UI hides.

For `external_comfy`, only `prompt` and `negative_prompt` participate: the mode uses the one checkpoint and shipped compatible graph selected in Advanced settings. Orb must not claim that backend reproduces the curated local look.

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

Managed-local v1 has a single-Orb-process deployment contract. On the first managed runtime or bundle mutation, Orb takes an exclusive OS file lock at `runtime.lock` and retains it for the process lifetime. If another Orb process already owns the same imagegen data root, this process reports managed local as unavailable instead of starting a second sidecar or mutating shared files. External generation remains available. Inside the owner process, the supervisor is guarded by one async lifecycle lock:

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

The initial generate MUST write the diffusion seed it used into the attachment's dedicated `seed` column, as text. Rehydrate reads `att["seed"]` and re-folds it; without this write the row is silently unrehydratable. The seed lives only in that column — it is never duplicated into `generation_metadata`.

### Managed ComfyUI

- Require a healthy managed runtime and a fully verified style bundle.
- Load the immutable recipe graph, deep-copy it, and patch only declared slots.
- Submit `POST /prompt` with a random `client_id`, poll `GET /history/{prompt_id}`, then fetch only the declared output through `GET /view`.
- Treat prompt validation errors, `node_errors`, missing declared output, MIME mismatch, oversized response, and timeout as failures.
- Funnel every render failure — unreachable host, connect error, HTTP status, execution error reported in history, malformed body, missing or empty output, timeout — into a single client exception type. Callers degrade identically on all of them, so distinguishing them at the call site buys nothing and multiplies the paths each caller must handle.
- Validate the returned bytes by signature and cap response size before persistence.
- The initial implementation may poll once per second. WebSocket progress and previews are deferred.

### External ComfyUI

- Test `/system_stats`, `/object_info`, and `/models/{folder}`.
- Advertise `can_install_curated_models=false` unconditionally.
- Use one shipped core-node graph selected in Advanced settings and one checkpoint filename selected from the server's discovered list.
- Apply the chosen style's prompt fragments only. Do not silently substitute a catalog checkpoint, upload a model, write remote userdata, or invoke Manager routes.
- Poll by `prompt_id`; on timeout stop polling without issuing the global `/interrupt`.
- An external server with incompatible/missing first-party nodes or graph inputs fails connection validation with an actionable message.

## Workflow config and profile

No schema migration is required. Global settings live in `settings.workflow_config.image_gen`; machine/runtime/model installation state stays on disk.

```json
{
  "source": "managed_local",
  "default_style": "realistic",
  "scene_analysis": false,
  "timeout_seconds": 180,
  "external_comfy": {
    "api_url": "http://127.0.0.1:8188",
    "api_key": "",
    "checkpoint": "",
    "workflow": "external_core_v1"
  }
}
```

Every hook calls `normalize_config` because workflow `config_schema` is UI metadata, not enforcement. Validation includes the source/style enums, bounded strings, HTTP(S) URLs, timeout range, and source-specific required fields. The normalizer drops unknown keys, coerces numerics that round-tripped through JSON as strings, and returns a new canonical dict merged over the defaults, so a partial or empty persisted slot still resolves every key and downstream code reads typed values without rechecking.

**Overridable defaults are stored empty and shown as placeholders.** Any field with a shipped default — prompt fragments in particular — persists as an empty string and renders as ghost text sourced from the manifest's `config_schema` default, with the backend substituting the baked value whenever the field is empty. Editing is then an explicit override, and a curated default can change between releases without migrating stored config or silently overwriting a user's edit.

Secrets remain only in live workflow config and are read at call time. They never enter attachment metadata, job snapshots, logs, subprocess argv, or catalog files.

**Preset export needs a new nested-JSON scrubber; the existing secret protections do not reach this case.** Orb's preset secret machinery is column-granular and column-*name*-driven: `_scrub_configs` blanks whole columns listed in `SECRET_COLUMNS` (`backend/features/presets/engine.py`), and the `SENSITIVE_*` tripwire that forces coverage suffix-matches column *names* (`backend/database/preset_schema.py`). The external endpoint's API key lives nested inside the `settings.workflow_config` JSON column, whose name matches no secret suffix — so the tripwire never flags it and no existing scrubber touches it. Therefore:

- Add a JSON-path scrub of `workflow_config.image_gen.external_comfy.api_key` to the configs export path (`_scrub_configs`), keyed off the workflow-config shape rather than a column name.
- The `SECRET_COLUMNS` coverage test (`tests/integration/test_preset_schema_coverage.py`) is no backstop here; correctness rests entirely on dedicated canary tests.
- Precedent confirms the blind spot: TTS already stores `api_key` inside `character_cards.workflow_state` (`backend/workflows/tts/synth.py`), uncovered by the same mechanism. Do not assume the framework scrubs workflow JSON secrets — it does not.

Dropping the hosted-provider adapter removes one of two keys, not the requirement: a single nested secret is exactly as invisible to the existing machinery as two. Tests seed a unique canary into the key and assert it disappears when `configs` is omitted and when `strip_keys=true`, and that a deliberate `strip_keys=false` export retains it.

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

**Two composition modes, selected by config.** A free-text `scene` field invites the model to fill unestablished details from genre convention — inventing an outfit or a pose the transcript never established, which reads as the character changing clothes between turns. Two mitigations exist and they trade cost against rigor, so `scene_analysis` (default `false`) picks between them:

- **Off — one forced call.** `compose_image_prompt` alone, relying on instruction discipline: use only what the history directly evidences, take the most recent explicit statement for every attribute, and fall back to the character's default rather than guess when the text establishes nothing. One inference per image.
- **On — an analysis call first.** `analyze_scene` returns a *structured* scene — characters present, each one's outfit as a **delta from their default** (articles added or substituted, default articles now absent), spatial anchors, positions relative to anchors and to each other, poses, and actions — which is rendered to compact text and appended as the final message of the composition call, so the scene conclusions sit where attention is strongest. The components are enforced by the schema instead of by prompt wording. Two inferences per image.

The outfit delta is the reason the second mode exists: it is the only representation that distinguishes "the transcript established a change" from "the model would like there to be a change", and it is what keeps a character visually stable across turns.

Default off. The extra call doubles composition latency on a path that already blocks, and instruction discipline is sufficient for the common single-character case. Turn it on for multi-character scenes and long conversations where drift compounds.

Both `ToolSpec`s are declared unconditionally — registry membership is fixed at `finalize_registry()` and cannot follow a runtime config value. The toggle selects which path executes, not what is registered. Failures degrade one rung at a time: analysis failure falls through to single-call composition, composition failure falls back to a bounded plain-text excerpt of the anchor assistant reply, and only an empty excerpt fails the generation.

```python
COMPOSE_TOOL_SCHEMA = {"type": "function", "function": {
    "name": "compose_image_prompt",
    "description": "Tag the current visible moment without choosing an art style.",
    "parameters": {"type": "object", "properties": {
        "scene": {
            "type": "string",
            "description": (
                "Comma-separated visual tags, not sentences. Cover subjects and count, "
                "setting, lighting, pose, expression, clothing, and framing. "
                "Prefer many short specific tags over few broad ones. "
                "No art-style or quality terms. "
                "Example: 1girl, solo, long white hair, twin braids, blue jacket, "
                "open clothes, sitting, windowsill, night, city lights, upper body, "
                "looking away, melancholic"
            )
        },
        "avoid": {
            "type": ["string", "null"],
            "description": "Optional comma-separated tags for visible elements that must not appear."
        }
    }, "required": ["scene", "avoid"], "additionalProperties": false}
}}
```

The forced calls use the writer lane exposed by the hook contexts, always off-turn: there is no in-turn caller, so no prefix or cache tracker is inherited and no Writer-prefix-reuse promise is made or needed.

**Off-turn paths need the full prefix, not a bare transcript.** On-demand and regenerate run with no turn in flight, so they have no pipeline prefix to reuse. Since generation is on-demand only, *every* composition path is off-turn — there is no in-turn variant to fall back on. A plain role/content list of history omits the system prompt and character framing, and a composer that cannot see who the character is describes a generic person. These paths must rebuild the prefix a pipeline pass would receive: effective system prompt, character persona and scenario, example messages, macros, resolved persona description, and history up to the anchor message only. Pre-pipeline system blocks have no off-turn analogue and are omitted; KV reuse is forgone and the cold prompt is paid, which is acceptable precisely because the user asked for this image and is already waiting on a render.

Reconstructing that prefix requires character-context and persona resolution that currently live in `database` and `pipeline`, neither of which a workflow may import. Add one `build_offturn_prefix` helper to `workflows/toolkit.py` rather than copying those two resolvers into the workflow. TTS faces the same boundary, so the helper has two consumers on arrival, which is the bar this plan already sets for promoting shared code; copies flagged as keep-in-sync are the failure mode this avoids.

Composition is deterministic after the forced call:

```text
managed positive = recipe/style positive + character appearance + scene
managed negative = recipe/style negative + character negative + avoid

external positive = character appearance + scene + selected style prompt
external negative = character negative + avoid + selected style negative
```

Segments are whitespace-normalized, individually length-bounded, and joined without attempting semantic de-duplication. The style is always supplied by the trusted catalog, never by the LLM. If the forced call fails, use a bounded plain-text excerpt of the anchor assistant reply as `scene`; if both are empty, fail without spending inference resources. That excerpt is prose fed to a tag model, so it renders worse than a composed prompt — accepted, because the alternative on this path is no image at all, and it is not worth a sentence-to-tag converter to improve a fallback.

## Hooks and artifact behavior

Declare `image_gen_workflow` with `produces_artifacts=True` and bind ON_DEMAND, REGENERATE, and REROLL_GEN in `backend/workflows/__init__.py` before `finalize_registry()`. **No POST_PIPELINE binding**: nothing in this workflow runs inside a turn, so it cannot delay assistant persistence, cannot hold pipeline locks, and cannot fail a turn. This is the largest single simplification in the design, and several consequences follow from it — every composition path is off-turn, so there is exactly one prefix-construction path rather than an in-turn one and an off-turn one that must produce comparable results; there is no turn-cancellation propagation to handle; and no KV-cache or Writer-prefix-reuse question arises.

### On demand

`POST .../workflows/image_gen/trigger` action `generate` accepts `{message_id, style_id}`. Validate that `message_id` is an integer but not a boolean, that the target message exists in this conversation and is an assistant message, and that `style_id` is a live catalog id. Compose from the message, resolve the selected style/source, generate, and insert one workflow attachment.

**Progress streams from the hook return; no new SSE contract is added.** The generic trigger route relays a hook's `StreamingResponse` verbatim, so the action returns one and frames its own phase and reasoning events the way the orchestrator frames the in-turn pipeline's. Install jobs poll (long-running, must survive restart); generation streams (short, in-request). Two rules make the stream safe to consume:

- The body is guarded in full. An uncaught exception inside a streaming response aborts the chunked transfer without its terminating chunk, leaving the client's reader waiting on a stream that never closes; degrading to a null result keeps the UI unblocked.
- The stream ends on an explicit terminal event carrying the new attachment id, or null when generation produced nothing. Clients finish on that event rather than on stream close, which can stall.

**The stream body runs outside the trigger route's locks.** `api_trigger_workflow` holds the conversation and character-state locks only for the duration of the hook *call*; returning a `StreamingResponse` returns immediately and its body is consumed after the route function exits, so every lock is released before the first byte is produced. Anything depending on that serialization — reading conversation state, character state, or config, and validating the target message — must complete before the response object is constructed, and its results captured into the generator. Generation and attachment insertion then run unlocked, which is safe here only because this path appends a new attachment rather than read-modify-writing workflow state. A future action that does read-modify-write cannot use the streaming return without its own lock. Pin this with a test: two concurrent triggers on one message must not interleave into a corrupt sibling tree.

The only other ON_DEMAND actions are `get_profile` and `set_profile`, because they need the active conversation's character context. Global readiness, connection tests, model discovery, and runtime/model mutation use the dedicated `/api/workflows/image_gen/...` routes and never take the conversation lock held by the workflow trigger.

### Regenerate, reroll, and rehydrate

- Regenerate recomposes the scene from the anchor message under the currently selected style and source, creating a sibling artifact.
- Reroll uses stored resolved prompt/recipe/model metadata with a fresh seed — the route-supplied `_generated_seed()` hex string, folded to a 64-bit int (see "Seed handling").
- Rehydrate uses the stored metadata and the stored `seed` column value, re-folded to the same int, when the backend supports seeding.
- Managed replay requires the recorded recipe and exact bundle artifacts. If they are unavailable, fail with a sanitized actionable error; never silently switch to a newer recommended model.
- External replay is best effort. A seed and recipe are evidence of the request, not a guarantee of identical bytes — the remote server's model, nodes, and versions are outside Orb's control and may have changed.

Store in `generation_metadata`:

```text
source, style_id, catalog_version, recipe_id, workflow_sha256,
bundle_id, artifact_sha256s, runtime_version, backend_model,
composer_mode, prompt, negative_prompt, width, height, steps, cfg,
sampler, scheduler
```

`composer_mode` records which composition path produced the prompt. Replay never re-composes — reroll and rehydrate render the stored prompt — so this is diagnostic only, and it is what makes "why did this one get the outfit wrong" answerable after the config has since been toggled.

Only fields applicable to the resolved backend are populated. Never store API keys, managed/external URLs containing credentials, local paths, or raw backend responses. `consumption_metadata` contains the display-safe style label, prompt, negative prompt, and source.

## Frontend

All files under `frontend/workflows/image_gen/` import only `/static/workflow_api.js` plus their own relative modules.

### Normal flow

The Visualize button is registered as a workflow message button, shown only on assistant messages (the image depicts the reply) and only while that message has no `image_gen` attachment yet. It is disabled when other tabs are open, matching the existing edit/regenerate controls: a single writer prevents two tabs racing duplicate artifact roots onto one message.

It opens a minimal modal:

1. Style dropdown (`Realistic`, `Anime`).
2. One primary action: Generate.

In managed-local mode, if setup is incomplete, the same modal replaces Generate with exactly one relevant action:

- `Set up local image generation` when the runtime is absent;
- `Download <style> model (<size>)` when its bundle is absent;
- `Repair installation` when files are corrupt;
- a clear unsupported-device message when no runtime variant matches.

After a job starts, show determinate byte progress when total size is known, Cancel, and a retryable failure message. Poll job status and restore Generate only after status re-verifies readiness. Do not show raw logs, paths, graph/model controls, or a misleading Generate button while prerequisites are missing.

### Tools panel / advanced settings

The normal card shows source, default style, readiness, disk usage, and a Settings button. Advanced settings contain:

- source selector (`Managed local`, `External ComfyUI`);
- render timeout;
- scene-analysis toggle, labelled with its cost ("more accurate outfits and positions in multi-character scenes; one extra model call per image");
- installed bundles with size/source/remove actions;
- managed runtime status/start/stop/repair/remove and sanitized log tail;
- external URL/key/checkpoint and connection test;
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
- Style resolution proves managed style selects its recipe/bundle/params while external styles change prompt only.
- Composer schema/choice parity, segment order/bounds, style isolation, and anchor-text fallback.
- `scene_analysis` off runs one forced call and on runs two; the analysis result renders to text as the composition call's final message; a failed analysis degrades to single-call composition rather than failing the generation; both tools stay registered under either setting.
- Scene rendering tolerates a partial or malformed analysis result — every section is independently droppable — so a model that omits a field still yields usable text.
- Seed fold from the framework hex string to a bounded 64-bit int is deterministic; the initial generate persists the `seed` column and rehydrate re-folds the stored value. The generate-side reduction and the rehydrate-side decode share one pinned modulus — a mismatch silently produces a different image rather than erroring, so the round-trip is asserted directly.
- Registering standalone workflow tools widens the global registry, so the tool-registry baselines assert `BUILTIN_TOOL_NAMES == frozenset(TOOLS) - STANDALONE_TOOLS` and that the built-ins are disjoint from the standalone set — the property that actually holds built-ins in the pipeline union — rather than equality against all registered tools and an empty standalone set.
- Installer builds pinned `comfy-cli` argv (never shell text) and verifies the pinned `comfy-cli` version before invoking it.

### Integration

- Base Orb boots and all non-image features work without ComfyUI, PyTorch, model files, or optional installer dependencies.
- Runtime/model install routes return jobs without holding request connections; restart reconstructs verified status.
- No install starts from boot, status, conversation open, or Visualize.
- Managed generation refuses missing/corrupt prerequisites before invoking ComfyUI.
- Completing a turn produces no image and no image-generation inference of any kind: the workflow has no POST_PIPELINE subscription and a full pipeline run leaves the attachment table untouched. This is the guard on the on-demand-only contract, so it is asserted directly rather than inferred from the absence of a binding.
- On-demand guards, streamed progress with its terminal event, profile round-trip, regenerate sibling shape, reroll seed change, and rehydrate request replay.
- The streamed generate action validates its target and reads conversation/character state before returning the response object; two concurrent triggers on one message do not interleave into a corrupt sibling tree.
- Exact stored recipe remains required after catalog recommendation changes.
- External discovery/generation never calls install, userdata-write, Manager, or interrupt endpoints.
- Preset canaries prove the bespoke JSON-path scrubber removes the nested external API key when configs are omitted or `strip_keys=true`, and retains it under a deliberate `strip_keys=false` export (the `SECRET_COLUMNS` coverage test does not reach this path).
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

1. Test clean install, cancellation, resume, repair, start, generation for both styles, stop, restart, explicit runtime upgrade/removal, and bundle removal.
2. Record runtime/model download size, disk peak, startup time, first-generation latency, steady latency, and peak RAM/VRAM.
3. Verify no service listens beyond loopback and no Manager/custom-node route is enabled.
4. Review output quality with fixed prompts/seeds before marking a bundle recommended.
5. Complete source/notices review for ComfyUI, runtime dependencies, and every distributed/recommended model artifact.

An untested platform/accelerator combination is reported as unsupported; it is never inferred to work from a nearby variant.

## Implementation sequence

Ordered so a working feature exists as early as possible. External ComfyUI needs none of the catalog, download, or runtime machinery, so it comes first and makes every later phase testable against something real — and it settles what prompt composition actually needs *before* two model bundles are committed to curatorial review, which is the expensive thing to change later.

1. **Contracts and generation core**: add the engine skeleton, request/result/capabilities contracts, the documented Comfy client with its single error funnel, strict graph patching against a declared slot map, output signature/size validation, and fake-server tests.
2. **Workflow integration**: implement config normalization, character profile, the composer (single-call path first), and the on-demand/regenerate/reroll hooks. Add the `build_offturn_prefix` toolkit helper, register the workflow, widen the two tool-registry baseline assertions that standalone registration breaks, add nested preset-secret scrubbing, and complete integration tests.
3. **External ComfyUI**: endpoint/model discovery, generation against a user-selected checkpoint, connection validation with actionable failures, and capability tests proving install is unavailable. **Ships a usable feature.** Everything after this improves it rather than enabling it.
4. **Frontend**: Visualize message button, style/generate modal, streamed progress with its terminal event, tools panel, and escaping/state tests.
5. **Scene analysis**: add the second forced call behind `scene_analysis`, its rendering to text, and the degrade-to-single-call path. Deliberately after real images exist, so the toggle's value can be judged against actual output.
6. **Contracts and release inputs for managed local**: catalog/runtime schemas, strict loader, two style ids, placeholder test bundles, graph-slot validator, path policy, and catalog tests. Production catalog entries remain unavailable until real artifact metadata and review are complete.
7. **Jobs and downloads**: background job registry, status/cancel API, disk preflight, secure resumable downloader, verified atomic installs, repair/remove, and tests.
8. **Managed runtime**: platform variant resolver, isolated `comfy-cli`-driven installer (pinned version + argv), generated extra-model paths, supervisor, lifespan shutdown, fake-process tests, then certify the first real runtime variant. The readiness-driven setup/download UI lands with it.
9. **Release hardening**: certify runtime variants and curated bundles, update `AGENTS.md` and `docs/architecture/secondary-workflow.md`, run `./scripts/lint.sh` and `./scripts/tests.sh all`, then execute the manual release matrix.

Phases 6–8 carry nearly all the risk and all the release blockers. Keeping them behind a shipped external-mode feature means managed local can slip without the feature slipping.

## Release blockers

These gate the *managed local* mode only. External-ComfyUI mode ships when its own phase is complete and is not held behind them. The feature is not ready to advertise as fool-proof managed local generation until all are true:

- At least one runtime variant has a pinned, reproducible, clean-machine install and full manual matrix result.
- Each visible managed style points to a real reviewed bundle with immutable sources, exact byte sizes, SHA-256 hashes, hardware guidance, and a passing shipped workflow.
- Runtime and model downloads are cancellable, resumable, verified, and recover safely across process restart.
- Managed ComfyUI is loopback-only, uses no Manager/custom nodes, and cannot mutate paths outside its data root.
- GPL/runtime/model licensing review is complete for the actual distribution method and catalog artifacts.
- The normal UI contains no expert engine parameter and never offers an action Orb cannot fulfill.
