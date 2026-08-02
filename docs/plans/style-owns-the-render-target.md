# Plan — the Style owns the render target, the Connection owns connectivity

> Status: **draft for review**. Nothing implemented yet.
>
> Two phases. **Phase 0** teaches ComfyUI graphs an optional resolution slot;
> **Phase 1** moves the cloud render settings from the Connection to the Style.
> Phase 0 is sequenced first because it is what lets Phase 1 land at full scope
> instead of deferring half of it — see [Sequencing](#sequencing).

## The principle

A **connection** is how Orb reaches a backend: an address and a credential. A
**style** is what an image looks like: the prompt text, the format, the model
that draws it, and the shape it comes out.

Today the cloud half violates that. `model`, `width`/`height`, `quality` and
`reference_source` all live on the connection, so a connection is a render preset
that happens to hold a key.

The ComfyUI half already gets it right for the fields it has — a style carries
`checkpoint` and `workflow`, and
[`ExternalComfyAdapter.resolve_target`](../../backend/workflows/image_gen/engine/adapters/external_comfy.py#L126-L128)
reads them straight off the style. This plan makes cloud symmetric with it, and
gives ComfyUI the one field it is missing.

## What is true today

Verified by reading, not assumed:

1. **The current cloud design is deliberate and documented.**
   [config.py:141-143](../../backend/workflows/image_gen/config.py#L141-L143) says
   *"There is deliberately no per-style cloud model: the model belongs to the
   connection, and two models on one provider is the case for a second
   connection."*

2. **The escape hatch that justifies it does not exist.**
   `cloud.providers` is keyed by provider id, and both
   [`addableProviders`](../../frontend/workflows/image_gen/policy.js#L168-L171)
   and [`addRowHtml`](../../frontend/workflows/image_gen/config_panel.js#L641-L646)
   hard-limit the panel to one connection per provider. "FLUX.1-kontext for
   realistic, SDXL for anime, both on Together AI" is unreachable.

3. **The workaround is to make styles into connections.** The shipped styles are
   *Realistic* and *Anime*. This install's settings panel currently shows styles
   named *Together AI*, *OpenRouter*, *OpenAI*, *NanoGPT* — all `(Prose)` —
   because the provider is the only thing a style can vary. The user-facing docs
   state the limitation out loud
   ([image-generation.md](../features/image-generation.md), the Style-fields
   table): *"A cloud provider has one model for every style, set in the
   **Connection** section."*

4. **The cloud adapter is already handed the style and ignores it.**
   [`resolve_target(self, style, replay)`](../../backend/workflows/image_gen/engine/adapters/openai_image.py#L130)
   never reads `style`; it reaches into `self._entry` instead. Meanwhile
   `ExternalComfyAdapter.readiness()` resolves `active_style(config)` while
   [the cloud `readiness()`](../../backend/workflows/image_gen/engine/adapters/openai_image.py#L98)
   takes a bare `model` string. Two adapters, two different ideas of what a
   render target is.

5. **Routing is global, and that is a latent bug this change would turn fatal.**
   `normalize_config` derives `source` from the **default style's** connection
   ([config.py:428-433](../../backend/workflows/image_gen/config.py#L428-L433)),
   and [`get_adapter(config)`](../../backend/workflows/image_gen/engine/router.py#L45-L53)
   routes on that `source` — not on the style being rendered.

   `/reroll-gen` gets away with it because the widget overwrites `style_id` with
   `cfg.default_style` on every reroll
   ([widget.js:73](../../frontend/workflows/image_gen/widget.js#L73)).
   **`/rehydrate` does not**: it calls the hook with the attachment's *stored*
   params ([routes/workflows.py:607-613](../../backend/api/routes/workflows.py#L607-L613)),
   and `generation_metadata` carries `style_id`
   ([hooks.py:152](../../backend/workflows/image_gen/hooks.py#L152)), so it is
   whatever the image was originally made with. Rehydrating an image made under a
   ComfyUI-linked style while the default style is cloud-linked routes a ComfyUI
   style into the cloud adapter today. It survives only because the cloud adapter
   ignores the style entirely — the exact coupling this plan removes.

   **So the routing fix is a prerequisite, not a nice-to-have.** Without it, the
   first rehydrate across a mismatched style fails with *"Choose a model for
   OpenAI"* on a style that has a perfectly good checkpoint.

6. **Orb already reads dimensions off a ComfyUI graph; it just never writes
   them.** [`describe_render_params`](../../backend/workflows/image_gen/engine/graph.py#L68-L79)
   scans every node for a widget pair named `width`/`height` and records what it
   finds on the attachment. The detection half of Phase 0 is already written and
   in production.

---

# Phase 0 — an optional resolution slot for ComfyUI graphs

## Why this is the right shape

The concern that prompted it: an img2img graph whose output size comes from
`grounding_px` or an aspect-ratio node, where there is no `width`/`height` pair
to patch and patching an `EmptyLatentImage` would not control the output anyway.

**That resolves itself if the slots are optional per graph**, which is the
pattern the slot map already uses twice. `negative` is optional because a
one-encoder prose graph has no negative conditioning; `checkpoint` is optional
because a self-contained graph carries its own model. Both are handled by the
same three mechanisms — a `"role" in slots` guard in
[`patch_graph`](../../backend/workflows/image_gen/engine/graph.py#L125-L133), a
matching guard in `validate_graph_structure`, and a `None — …` option in the
importer.

So:

| Graph | Maps size slots? | Result |
|---|---|---|
| Basic t2i with `EmptyLatentImage` | yes | resolution picker applies |
| img2img driven by `grounding_px` / aspect ratio | no | picker inert, exactly as today |
| Multi-stage with an upscale node | user picks which node | explicit, like the text encoder choice |

Nothing regresses: a graph that maps nothing behaves precisely as it does now.

## Design

**Two independent slots, `width` and `height` — not one `size` slot.** The
existing [`_slot`](../../backend/workflows/image_gen/config.py#L173-L182) shape is
`[node, field]`, so two ordinary slots reuse every validator, the `patch_graph`
role loop, and `_input_slot` unchanged. It also handles the case where the two
live on different nodes (a resize chain), which a single `[node, w, h]` triple
could not express.

**Default to unmapped at import.** The importer defaults the model slot to
*mapped* (`candidates.checkpoint.length ? 0 : -1` at
[config_panel.js:1032](../../frontend/workflows/image_gen/config_panel.js#L1032))
because a PNG pins another machine's filename. Dimensions are the opposite case:
the graph author picked a size their checkpoint renders well at, and many SD1.5 /
SDXL checkpoints degrade badly off their native resolution. Orb should not hand
over that knob by default. Default `-1` — *"None — the workflow decides"*.

## Work items

**`backend/workflows/image_gen/config.py`**
- `_user_graph`: add `"width"`, `"height"` to the optional-slot loop
  ([line 227](../../backend/workflows/image_gen/config.py#L227)). Genuinely a
  one-line change — they parse through the existing `_slot` and stay absent when
  unmapped, exactly like `negative`.

**`backend/workflows/image_gen/engine/graph.py`**
- `patch_graph`: accept `width`/`height`, patch each when its slot exists, using
  the same `"role" in slots` guard `negative` uses.
- `validate_graph_structure`: validate the two when present.
- `describe_render_params`: **prefer the mapped slots over the positional scan.**
  Today it takes the first node in sorted order carrying a `width`/`height` pair,
  which may not be the node Orb patched — an upscale node can sort first. Already
  imprecise; becomes wrong in a new way once Orb writes to one of them.

**`backend/workflows/image_gen/engine/adapters/external_comfy.py`**
- `node_roles`: add `dimension_inputs`, alongside the existing `seed_inputs`
  rule ([line 241](../../backend/workflows/image_gen/engine/adapters/external_comfy.py#L241)) —
  `[name for name in _typed_inputs(entry, "INT") if name.lower() in ("width", "height")]`.
  Same shape, same one-liner.
- `resolve_target`: `supports_dimensions` becomes
  `"width" in slots and "height" in slots`, replacing the hardcoded `False` and
  its now-obsolete comment
  ([lines 152-153](../../backend/workflows/image_gen/engine/adapters/external_comfy.py#L152-L153)).
  Populate `width`/`height` from the style.
- `generate`: pass them to `patch_graph`. Add a note when a graph has no size
  slots and a non-default resolution was chosen, mirroring the existing
  "this workflow has no negative prompt input" disclosure
  ([line 257-258](../../backend/workflows/image_gen/engine/adapters/external_comfy.py#L257-L258)).

**`frontend/workflows/image_gen/graph_import.js`**
- `slotCandidates`: a `dimension` bucket fed by `typing.dimension_inputs`, with a
  fallback name list for when the server is unreachable. Keep it to inputs
  literally named `width`/`height` — a bare INT like `grounding_px` must not be
  offered, because a wrong guess patches something that is not a size.
- `missingRoles`: unchanged. Dimensions are never required.

**`frontend/workflows/image_gen/config_panel.js`**
- Import picker: two selects, both defaulting to *"None — the workflow decides"*.
- `addPendingGraph`: read them into `slots.width` / `slots.height`.

**Tests**
- `tests/unit/workflows/image_gen/test_graph.py` — patch/validate with and
  without the slots; the `describe_render_params` precedence fix.
- `tests/unit/workflows/image_gen/test_external_adapter.py` — per-graph
  `supports_dimensions`, and the unmapped-graph disclosure note.
- `node --test` over `graph_import.js` — the dimension bucket and its fallback.

**Estimate:** half a day. No config migration — an existing graph simply has no
`width`/`height` key in its slot map, which is already how "unmapped" is encoded.

---

# Phase 1 — the Style owns the render target

## Target shape

```jsonc
{
  "styles": [{
    "id": "realistic",
    "label": "Realistic",
    "prompt_format": "hybrid",
    "prompt": "...",
    "negative_prompt": "...",
    "extra_instructions": "",
    "connection": "togetherai",

    "checkpoint": "",            // ComfyUI half — unchanged
    "workflow": "",              // ComfyUI half — unchanged

    "model": "black-forest-labs/FLUX.1-kontext-pro",  // NEW — cloud half
    "width": 1024,                                     // NEW — both halves, after Phase 0
    "height": 1536,                                    // NEW — both halves, after Phase 0
    "quality": "",                                     // NEW — cloud half
    "reference_source": ""                             // NEW — cloud half
  }],
  "cloud": {
    "provider": "togetherai",    // kept: legacy fallback for unlinked styles
    "providers": {
      "togetherai": { "api_key": "...", "base_url": "" }   // connectivity only
    }
  }
}
```

Both halves are always present on a style, whichever connection it links to —
the rule `checkpoint`/`workflow` already follow, so relinking cloud → ComfyUI →
cloud loses neither pin. `width`/`height` are the first fields **both** backends
read, which is what Phase 0 buys.

`cloud.width`/`height`/`quality`/`reference_source` (the mirrored block) and the
same four keys on each provider entry are **read on load and not written back**,
which is how they migrate.

## Sequencing

Phase 0 first, and the reason is that it removes a deferral rather than adding
work. Without it, `width`/`height` have no ComfyUI meaning, so they stay on the
connection and the connection stays a partial render preset — then moving them
later costs a *second* normalize-time hoist (entry → style) plus a second round
of settings-panel churn. Doing Phase 0 first makes Phase 1 land at full scope in
one pass, and leaves the connection as `{api_key, base_url}` — exactly as wide as
the ComfyUI connection's `{api_url, api_key}`.

The two phases are otherwise independent: Phase 0 touches graph/slot machinery,
Phase 1 touches config normalization and routing. They meet only at
`ExternalComfyAdapter.resolve_target`.

## Why all four fields move, not just the model

`reference_source` is gated by a *model-level* allowlist —
[`modelTakesReferences`](../../frontend/workflows/image_gen/policy.js#L191) and
its backend twin `takes_references`. Split it from the model and the panel's
"this model does not accept reference images" warning
([config_panel.js:584-589](../../frontend/workflows/image_gen/config_panel.js#L584-L589))
becomes a cross-object check: references on at the connection, style A on Kontext
(works), style B on schnell (silently drops the reference and still bills).

`quality` carries the same one-per-provider limitation the model does — a "quick
draft" style and a "final" style on one OpenAI connection cannot differ in
quality while it lives on the connection.

`width`/`height` become genuinely shared once Phase 0 lands.

## Migration

**No DB migration, no `schema.py` change, no `preset_schema.py` change.**
`workflow_config` is a JSON blob, and the declared secret path
`("image_gen", "cloud", "providers", "*", "api_key")` still holds — `api_key` is
the one field that stays.

`normalize_config` runs on every read and every write, so it hoists on first read
and persists hoisted on first write. This is the pattern the styles hoist already
uses ([config.py:402-410](../../backend/workflows/image_gen/config.py#L402-L410))
and the pattern `_cloud_provider_entry`'s `legacy` argument already uses
([config.py:300-329](../../backend/workflows/image_gen/config.py#L300-L329)).

Rule: **a style with no render settings of its own inherits them from the entry
its `connection` names**, falling back to the legacy `cloud.*` top-level keys,
then to the preset default.

| Stored today | After first read |
|---|---|
| 3 styles linked to `togetherai`, entry model `FLUX.1-schnell` | all 3 styles get `model: "FLUX.1-schnell"` |
| entry with a model, no style linked to it | model is dropped; the entry keeps its key. Linking a style later seeds from `preset.default_model` |
| style with `connection: ""` (predates linking) | inherits from `cloud.provider`'s entry, matching what it renders on today |
| ComfyUI-linked style | gets `width`/`height` defaults it only uses once its graph maps size slots |

Nothing re-routes and nothing changes what the next render produces.

### Two implementation traps in the hoist

**The hoist must read the *raw* cloud block, not the normalized one.**
`normalize_config` parses styles first, derives `provider_override` from the
default style's connection, and only then calls `_cloud(raw["cloud"], override)`.
So a `_style()` that wanted normalized cloud entries would close a cycle. Reading
the raw block is also the correct semantics — the values being read are legacy
ones on their way out.

**`_style` needs a second argument, and `_unique_by_id` does not pass one.**
[`_unique_by_id(candidates, parse, limit)`](../../backend/workflows/image_gen/config.py#L379-L390)
takes a one-argument `parse`. Bind the raw cloud block with a closure or
`functools.partial` at the call site rather than widening the helper — it is
shared with `_user_graph`.

## Work items

### Backend

**`backend/workflows/image_gen/config.py`**
- `_style()`: add `model` (`_text`, 256), `width`/`height` (`_edge`), `quality`
  (`CLOUD_QUALITIES`), `reference_source` (`REFERENCE_SOURCES`). Update the
  docstring — it currently argues the opposite of this design.
- `CONFIG_DEFAULTS["styles"]`: add the five keys to both shipped styles.
- `normalize_config`: pass the raw cloud block into `_style` parsing so each
  style can inherit from its connection's entry (the hoist).
- `_cloud_provider_entry`: reduce to `{api_key, base_url}`. Keep reading the old
  keys off `raw`/`legacy` long enough to feed the hoist.
- `_cloud`: stop mirroring the four render settings to `cloud.*`. Keep `provider`
  (legacy fallback + the frontend's `styleConnectionId`).
- **New**: `style_source(config, style) -> tuple[str, str]` returning
  `(source, provider_id)` — comfy → `("external_comfy", "")`, a cloud id →
  `("cloud", id)`, `""` → the stored global `source`/`cloud.provider`. The single
  place the derivation lives; `normalize_config` calls it for the default style
  to keep `config["source"]` meaningful for `_status`.

**`backend/workflows/image_gen/engine/router.py`**
- `get_adapter(config, style)` — **style required, positional**, so no render
  path can silently fall back to the default style. Routes on
  `style_source(config, style)`, not on `config["source"]`.
- `comfy_adapter(config)` unchanged. Its only caller is `_node_types`, which
  calls `node_roles()` — pure network, no style, no readiness.

**`backend/workflows/image_gen/engine/adapters/base.py`**
- Constructor takes `(config, style=None)`; the bound style is what `readiness()`
  and `resolve_target()` answer about. `None` falls back to `active_style(config)`
  so `comfy_adapter` and any diagnostic construction stay valid. The asymmetry is
  deliberate: optional at the constructor, required at the router, which is the
  seam where forgetting it would be a bug.
- `readiness()` keeps its `model` override argument (a replay judged on its
  recorded model — see
  [openai_image.py:99-104](../../backend/workflows/image_gen/engine/adapters/openai_image.py#L99-L104)).

**`backend/workflows/image_gen/engine/adapters/openai_image.py`**
- `_entry` resolves via the bound style's connection, not `cloud["provider"]`.
- `_model()` → `style["model"] or preset.default_model`.
- `resolve_target`: `width`/`height`/`reference_source` off the style. Replay
  override logic unchanged — it still pins the recorded size so a rehydrate
  cannot pick up today's setting.
- `_build`: `quality` off the style.
- `readiness()`: unchanged in shape; now inherently style-scoped.

**`backend/workflows/image_gen/engine/adapters/external_comfy.py`**
- `readiness()` drops its `active_style(config)` call and uses the bound style —
  which also fixes the case the docstring apologises for
  ([lines 65-69](../../backend/workflows/image_gen/engine/adapters/external_comfy.py#L65-L69)).

**`backend/workflows/image_gen/hooks.py`**
- `_generate_fresh`: `get_adapter(config, selected_style)`.
- `reroll_gen`: `get_adapter(config, style)` — **after** the `style_changed`
  pops, same as today. This is the rehydrate fix.

**`backend/workflows/image_gen/queries.py`**
- No signature change. `_status` keeps answering about the default style. The
  frontend's `configForConnection()` trick (point the default style at the
  connection being probed) still works unchanged for `test` / `models`.

### Frontend

**`frontend/workflows/image_gen/policy.js`**
- `hasContent()`: drop `.model`, keep `api_key`/`base_url`.
- `connectionList()`: `detail` is no longer the model. Use the provider label /
  host, or a linked-style count.
- `readiness()`: drop the `"No model"` clause — that is now a style-row problem,
  reported on the style row.
- `pendingDisclosures()`: `sendsImages` becomes "any style linked to this
  connection has `reference_source` set" instead of reading `entry.reference_source`.
- `modelTakesReferences()`: unchanged, new call site.

**`frontend/workflows/image_gen/config_panel.js`**
- `backendFields()` cloud branch: replace the "comes from this connection" note
  with Model / Resolution / Quality / Reference images, gated on
  `preset.supports_*` exactly as `cloudFields` does today. Move
  `referenceModelNote` and the aspect-ratio note here — the former is the whole
  point, since both its operands now sit in one row.
- `backendFields()` ComfyUI branch: Resolution too, once Phase 0 lands, shown
  only when the pinned graph maps size slots.
- `cloudFields()`: reduce to API key + base URL + Test + docs + capability line.
- `captureStyles()`: capture the five new fields, each falling back to the stored
  value (not `""`) so a control the preset does not render cannot blank a saved
  setting — the rule `captureConnections` already documents.
- `captureConnections()`: drop the five.
- `addStyle()`: seed the new fields from the previous style, as it already does
  for `checkpoint`/`workflow`.
- `relinkStyle()`: probe the *new* connection's models, not only ComfyUI
  ([line 439](../../frontend/workflows/image_gen/config_panel.js#L439) is
  ComfyUI-only today).
- `openSettings()`: probe every connection some style links to and that holds a
  key. `modelsByConnection` is already keyed by connection id, so N styles on one
  provider still cost one probe.
- Style summary: consider showing the model next to the connection badge, since
  it is now the thing that differs between two styles on one provider.

### Tests

- `tests/unit/workflows/image_gen/test_config.py` — the `_cloud()` helper and the
  render-settings/hoist tests (lines ~314-340) move to style-level; add a test for
  the connection→style hoist and for the "entry with no linked style" case.
- `tests/unit/workflows/image_gen/test_cloud_adapter.py` — `_config()` helper puts
  the model on the style; `_adapter()` binds a style.
- `tests/unit/workflows/image_gen/test_replay.py` — add the rehydrate-with-a-
  non-default-style case, which is the regression this plan is fixing.
- `tests/unit/workflows/image_gen/test_router.py` — routing by style, including
  two styles on two sources in one config.
- `tests/integration/workflows/image_gen/test_hooks.py` — `CLOUD_CONFIG`
  (lines 49-58) puts `width`/`height` at the cloud level and its styles come from
  `CONFIG_DEFAULTS` with `connection: ""`, so **this fixture is the legacy
  unlinked path**. Worth keeping one test on that shape rather than relinking it,
  since it is the only coverage of the `cloud.provider` fallback.
- `tests/integration/workflows/image_gen/test_routes.py` — fixture shape.
- **`tests/integration/test_preset_schema_coverage.py:800`** — asserts
  `cloud.providers.xai.model` survives a preset export while `api_key` is blanked.
  `preset_schema.py` itself needs no change (the declared secret path still
  holds), but this assertion moves to a style-level field. Easy to miss: it lives
  outside the image_gen test directory.
- `node --test` over `policy.js` — disclosure and readiness changes.

### Docs

- `docs/features/image-generation.md` — the Style-fields table, the Connection
  section walkthrough, the sentence documenting the limitation being removed, and
  the new ComfyUI resolution slots in the workflow-import steps.
- `docs/features/cloud-image-setup.md` — step 6 ("Select a Model") moves from the
  connection walkthrough to the style walkthrough.
- `AGENTS.md` — the image-generation API bullet mentions routing through
  `get_adapter(config)` on `config["source"]`; update to per-style routing.

## Risks and open questions for review

1. **Per-graph `supports_dimensions` makes the resolution picker live or dead
   depending on which workflow a style pins.** New per-style variability in the
   panel. Same shape as the existing checkpoint and negative-prompt handling, and
   `backendFields()` already swaps per connection, but it is one more conditional
   control.

2. **Orb gains a knob that can make ComfyUI output worse.** Off-native
   resolutions degrade many SD1.5/SDXL checkpoints, and today the graph author's
   chosen size always wins. Mitigated by defaulting the import to unmapped, so
   nothing changes until the user opts in.

3. **New reachable state: a style naming a model its connection does not have.**
   Today model and provider cannot disagree.
   [`modelField`](../../frontend/workflows/image_gen/config_panel.js#L259) already
   renders `"(not detected)"` for this, so it degrades rather than breaks, but it
   is a state that cannot currently occur.

4. **The connection row loses its most informative summary line.** The collapsed
   Together AI row currently reads `black-forest-labs/FLUX.1-kontext-pro`. It
   needs a new answer — see the `connectionList` item above.

5. **Adapter constructor change is the widest blast radius.** `ImageAdapter(config)`
   → `ImageAdapter(config, style=None)` touches both adapters, the router, both
   query paths and every test that constructs an adapter directly. The
   optional-constructor / required-router split above contains it.

6. ~~Should `cloud.provider` survive?~~ **Decided: yes, keep it.** It is the
   legacy fallback for styles with `connection: ""`, and that path is live — the
   `CLOUD_CONFIG` integration fixture runs entirely on it, because `CONFIG` ships
   no `styles` key and the defaults link nothing. Dropping it would mean
   force-linking every pre-linking style on upgrade, which changes what an
   existing install renders.

## Estimate

**Phase 0:** half a day. Reuses the optional-slot machinery end to end; the
detection half already exists.

**Phase 1:** a day of source work, plus test churn across seven files. ~10 source
files, no DB migration, no `preset_schema.py` change, no new HTTP routes, no
`workflow_api.js` ABI change. The routing fix is about a third of it and carries
the rehydrate regression fix with it.

## Revision log

**Draft 3 — ComfyUI resolution promoted to Phase 0.** The Draft 2 decision to
leave resolution on the connection was reversed: rather than defer, teach ComfyUI
graphs an optional size slot first, which lets Phase 1 land at full scope. Cheaper
end to end than deferring, because deferring costs a second hoist and a second
round of panel churn later. Verified before proposing it:
`describe_render_params` already locates a `width`/`height` pair in a graph;
`_user_graph`'s optional-slot loop, `patch_graph`'s `"role" in slots` guard and
`node_roles`' typed-input rule all extend by one line each.

**Draft 2 corrections**, found while re-checking Draft 1 against the code:

- **Attribution.** "Users work around it by naming styles after providers" was
  stated as a general fact; it is an observation of this install's settings panel.
  Reworded.
- **`preset_schema.py` untouched was right; "no preset test changes" was not.**
  `test_preset_schema_coverage.py:800` explicitly asserts the cloud entry's
  `model` survives an export. Added to the test list.
- **The hoist has an ordering cycle.** `_cloud()` runs *after* styles are parsed
  because it needs `provider_override` from the default style, so `_style()`
  cannot read normalized cloud entries. Documented, with the `_unique_by_id`
  signature trap alongside it.
- **`cloud.provider` question resolved** against the `CLOUD_CONFIG` fixture rather
  than left open.
- **Router API tightened** from `get_adapter(config, style=None)` to a required
  style, with the fallback pushed down to the constructor where `comfy_adapter`
  needs it.

Re-verified and unchanged across drafts: the rehydrate routing mismatch, the
one-connection-per-provider limit, and the four cloud-level render settings having
exactly one reader each in `openai_image.py`.
