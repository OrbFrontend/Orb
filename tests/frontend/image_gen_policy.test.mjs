// The rules the settings panel derives from a stored config: which connections
// exist, what a style renders on, and what each target can actually be asked for.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addableProviders,
  CLOUD_SIZES,
  COMFY_CONNECTION,
  COMFY_SIZES,
  connectionList,
  providerTakesReferences,
  normalizePromptFormat,
  povChoices,
  promptFormatLabel,
  PROMPT_FORMATS,
  sizeChoices,
  sizeIsExact,
  styleConnectionId,
} from "../../frontend/workflows/image_gen/policy.js";

// Auto is only a real choice while the classifier can answer it; otherwise it draws
// the fallback camera and the picker would offer the same shot twice.
test("the camera picker offers Auto only when the classifier can answer it", () => {
  const ids = ({ modes }) => modes.map(([id]) => id);

  const withClassifier = povChoices({ classifier: true, mode: "auto", fallback: "third" });
  assert.deepEqual(ids(withClassifier), ["auto", "first", "third"]);
  assert.equal(withClassifier.selected, "auto");

  const without = povChoices({ classifier: false, mode: "auto", fallback: "third" });
  assert.deepEqual(ids(without), ["first", "third"]);
  assert.equal(without.selected, "third");

  // A hand-pinned camera survives the classifier going away.
  assert.equal(povChoices({ classifier: false, mode: "first", fallback: "third" }).selected, "first");
});

// Both style pickers name the prompt format beside the style, so the label must be
// the format the render path will actually use — and anything unknown or unset
// reads as the default the backend substitutes.
test("every stored format has a label, and everything else reads as the default", () => {
  for (const [id, label] of PROMPT_FORMATS) {
    assert.equal(normalizePromptFormat(id), id);
    assert.equal(promptFormatLabel(id), label);
  }
  for (const value of [undefined, "", "booru", null]) {
    assert.equal(normalizePromptFormat(value), "hybrid");
    assert.equal(promptFormatLabel(value), "Hybrid");
  }
});

// ── connections ──────────────────────────────────────────────────────────────
//
// The connection list is derived from the credentials rather than stored beside
// them, so the interesting cases are all about *which* stored rows count as a
// connection the user made — and what a style pointing at one resolves to.

// `supports_references` rides along because the panel offers the reference control
// exactly where the adapter would send one: a provider with no reference field in
// its dialect uploads nothing, whatever a style relinked from elsewhere still stores.
const PROVIDERS = [
  { id: "xai", label: "xAI (Grok)", needs_base_url: false, default_model: "grok-imagine-image", supports_references: true },
  { id: "openai", label: "OpenAI", needs_base_url: false, default_model: "gpt-image-1", supports_references: true },
  { id: "custom", label: "Custom (OpenAI-compatible)", needs_base_url: true, default_model: "", supports_references: true },
];

const config = (over = {}) => ({
  source: "external_comfy",
  styles: [],
  external_comfy: { api_url: "http://127.0.0.1:8188", user_graphs: [] },
  cloud: { provider: "xai", providers: {} },
  ...over,
});

test("ComfyUI is always the first connection and is never removable", () => {
  const [comfy, ...rest] = connectionList(config(), PROVIDERS);
  assert.equal(comfy.id, COMFY_CONNECTION);
  assert.equal(comfy.removable, false);
  assert.equal(comfy.ready, true);
  assert.equal(comfy.detail, "127.0.0.1:8188");
  assert.deepEqual(rest, []);
});

test("the inert shipped provider row is not a connection the user made", () => {
  // The defaults carry one empty `xai` entry so the preset-schema walker can see
  // the api_key leaf. Listing it would put a connection in the panel that nobody
  // added and that renders nothing.
  const list = connectionList(config({ cloud: { providers: { xai: { api_key: "", base_url: "" } } } }), PROVIDERS);
  assert.deepEqual(list.map((c) => c.id), [COMFY_CONNECTION]);
});

test("a just-added, still-empty connection is listed while it is pending", () => {
  // The panel tracks those ids in a Set, so asking it the membership question in
  // list form threw instead of answering — and took the settings modal with it,
  // on the shipped defaults, which carry exactly the empty entry this filters.
  const args = [config({ cloud: { providers: { xai: { api_key: "", base_url: "" } } } }), PROVIDERS];
  for (const pending of [["xai"], new Set(["xai"])]) {
    assert.deepEqual(
      connectionList(...args, pending).map((c) => c.id),
      [COMFY_CONNECTION, "xai"],
    );
  }
  // And an unrelated pending id leaves the empty entry filtered out as before.
  assert.deepEqual(
    connectionList(...args, new Set(["openai"])).map((c) => c.id),
    [COMFY_CONNECTION],
  );
});

test("an entry holding anything, or linked by a style, is a connection", () => {
  const withKey = connectionList(config({ cloud: { providers: { xai: { api_key: "k" } } } }), PROVIDERS);
  assert.deepEqual(withKey.map((c) => c.id), [COMFY_CONNECTION, "xai"]);

  // A style pointing at an entry the user has not credentialed yet still has to
  // see it, or the row it names is unreachable in the panel.
  const linked = connectionList(
    config({ styles: [{ id: "s", connection: "openai" }], cloud: { providers: { openai: {} } } }),
    PROVIDERS,
  );
  assert.deepEqual(linked.map((c) => c.id), [COMFY_CONNECTION, "openai"]);
  assert.equal(linked[1].ready, false);
});

test("a cloud connection is unready until it has every prerequisite", () => {
  const only = (providers, styles = []) => connectionList(config({ styles, cloud: { providers } }), PROVIDERS).at(-1);
  assert.equal(only({ xai: { base_url: "https://proxy.example.com/v1" } }).detail, "No API key");
  assert.equal(only({ custom: { api_key: "k" } }).detail, "No API base URL");
  // A key is now the whole prerequisite. The model used to be checked here and is a
  // *style* problem: a connection with a key can render, and which model it renders
  // is a question the connection has no longer any business answering.
  assert.equal(only({ xai: { api_key: "k" } }).ready, true);

  // A provider Orb no longer knows is still listed: the backend retains such rows so
  // a rename does not erase a key, and hiding it would make that key unreachable.
  const renamed = only({ renamed_in_v2: { api_key: "k" } });
  assert.equal(renamed.label, "renamed_in_v2");
  assert.equal(renamed.preset, null);
  assert.equal(renamed.ready, false);
});

test("a connection a style renders on is listed even with nothing in it", () => {
  // The model used to make an entry "held", so a keyless row stayed visible through
  // it. With the model gone, only credentials count — and a connection a style
  // resolves to must still be reachable, or "Paste an API key for xAI" names a row
  // the panel does not show and the one thing to fix is the one thing you cannot
  // reach. Resolved, not raw: this is the legacy fallback path, where the style
  // names no connection at all.
  const unlinked = config({
    source: "cloud",
    styles: [{ id: "a", connection: "" }],
    cloud: { provider: "xai", providers: { xai: { api_key: "", base_url: "" } } },
  });
  const [, row] = connectionList(unlinked, PROVIDERS);
  assert.equal(row.id, "xai");
  assert.equal(row.ready, false);
  assert.equal(row.detail, "No API key");
});

test("a ready cloud row says how many styles reach it, since the model no longer can", () => {
  // Two styles on one provider is the state this whole change exists to allow, so
  // "which model" stopped being a connection-level fact. What is still worth seeing
  // collapsed is whether anything renders here at all -- a credentialed connection
  // nothing points at is a real state, and one that explains a setting doing nothing.
  const only = (styles) =>
    connectionList(config({ styles, cloud: { providers: { xai: { api_key: "k" } } } }), PROVIDERS).at(-1);
  assert.equal(only([]).detail, "No styles");
  assert.equal(only([{ id: "a", connection: "xai" }]).detail, "1 style");
  assert.equal(only([{ id: "a", connection: "xai" }, { id: "b", connection: "xai" }]).detail, "2 styles");
  // A style resolving there only through the legacy fallback counts too: it is the
  // connection that style renders on.
  const unlinked = connectionList(
    { ...config({ source: "cloud", styles: [{ id: "a", connection: "" }] }), cloud: { provider: "xai", providers: { xai: { api_key: "k" } } } },
    PROVIDERS,
  ).at(-1);
  assert.equal(unlinked.detail, "1 style");
});

test("Add offers each provider once", () => {
  const list = connectionList(config({ cloud: { providers: { xai: { api_key: "k" } } } }), PROVIDERS);
  assert.deepEqual(addableProviders(list, PROVIDERS).map((p) => p.id), ["openai", "custom"]);
});

// A style that predates connection linking has to keep rendering where it did,
// which is the whole reason "" is a legal value rather than a defaulted one.
test("an unlinked style resolves to whatever the old global source said", () => {
  assert.equal(styleConnectionId({}, config()), COMFY_CONNECTION);
  assert.equal(styleConnectionId({ connection: "" }, config({ source: "cloud" })), "xai");
  assert.equal(styleConnectionId({ connection: "openai" }, config({ source: "cloud" })), "openai");
});

test("a connection just added is listed before it holds anything", () => {
  // A fresh connection is genuinely empty — its model lives on a style now, and
  // dropping the row between the click and the first keystroke would read as the Add
  // button doing nothing.
  const empty = config({ cloud: { providers: { openai: { api_key: "", base_url: "" } } } });
  assert.deepEqual(
    connectionList(empty, PROVIDERS).map((c) => c.id),
    [COMFY_CONNECTION],
  );
  assert.deepEqual(
    connectionList(empty, PROVIDERS, ["openai"]).map((c) => c.id),
    [COMFY_CONNECTION, "openai"],
  );
});

test("reference support is a provider fact and is never asked of the model", () => {
  // The per-model allowlist is gone, and its absence is the point: it was a hand-kept
  // table over catalogues of hundreds of models, so it was always behind, and being
  // behind hid the control entirely — the user never learned the capability existed.
  // A model that will not take a reference says so in the remote message; the user
  // can then turn the existing reference control off.
  assert.equal(providerTakesReferences({ supports_references: true }), true);
  assert.equal(providerTakesReferences({ supports_references: true, default_model: "flux-schnell" }), true);

  // Provider-level is still a real answer: OpenRouter has no reference field on this
  // path at all, measured across three spellings, so there is nothing to send.
  assert.equal(providerTakesReferences({ supports_references: false }), false);
  assert.equal(providerTakesReferences(null), false);
});

// ── resolution ───────────────────────────────────────────────────────────────
// The picker's job is to offer only what the target will actually render. Anything
// else is a label that lies at the moment the user is choosing what to pay for --
// the backend does snap it, but it says so afterwards, on an image already billed.

test("a provider that names its own sizes is offered exactly those", () => {
  // OpenAI names them in its own rejection: "Supported sizes are 1024x1024,
  // 1024x1536, 1536x1024, and auto." Orb's wider menu was snapped to these anyway.
  const openai = { dimension_mode: "size", sizes: ["1024x1024", "1024x1536", "1536x1024"] };
  assert.deepEqual(sizeChoices(openai, false), openai.sizes);
  assert.equal(sizeIsExact(openai, false, "1024x1536"), true);
  assert.equal(sizeIsExact(openai, false, "1024x1820"), false);
});

test("a size provider that declares no menu keeps the full list", () => {
  // NanoGPT and OpenRouter deliberately publish none: each model has its own
  // vocabulary, and snapping to a menu the next model does not share is a worse
  // answer than the one the provider itself picks.
  for (const preset of [{ dimension_mode: "size" }, { dimension_mode: "size", sizes: [] }]) {
    assert.deepEqual(sizeChoices(preset, false), CLOUD_SIZES);
    assert.equal(sizeIsExact(preset, false, "1024x1820"), true);
  }
});

test("a pixel-grid provider is offered only what lands on its grid", () => {
  // Together 400s on a non-multiple of 16 and tops out at 1792, so the two 16:9-ish
  // rows are not sizes it can render -- they are scaled down and re-snapped.
  const together = { dimension_mode: "width_height", min_dimension: 64, max_dimension: 1792, dimension_step: 16 };
  const offered = sizeChoices(together, false);
  assert.deepEqual(offered, ["1024x1024", "1024x1536", "1536x1024"]);
  assert.equal(offered.includes("1820x1024"), false);
  assert.equal(sizeIsExact(together, false, "1820x1024"), false);
  // Off the grid rather than out of bounds -- 1000 is under the ceiling and still
  // not a multiple of 16.
  assert.equal(sizeIsExact(together, false, "1000x1000"), false);
  assert.equal(sizeIsExact(together, false, "1024x1024"), true);
});

test("an aspect-ratio provider takes any pair, since only the ratio is ever sent", () => {
  const xai = { dimension_mode: "aspect_ratio", aspect_ratios: ["1:1", "16:9"] };
  assert.deepEqual(sizeChoices(xai, false), CLOUD_SIZES);
  assert.equal(sizeIsExact(xai, false, "1024x1820"), true);
});

test("an unknown provider is not narrowed by a preset Orb does not have", () => {
  assert.deepEqual(sizeChoices(null, false), CLOUD_SIZES);
  assert.equal(sizeIsExact(null, false, "832x1216"), true);
});

test("ComfyUI gets its own menu, and every option is a size a latent can hold", () => {
  assert.deepEqual(sizeChoices(null, true), COMFY_SIZES);
  // A latent is the request divided by eight, so an odd edge is silently truncated
  // by the sampler -- which is why 1820 sits in the cloud list and not this one.
  for (const value of COMFY_SIZES) {
    for (const edge of value.split("x").map(Number)) assert.equal(edge % 64, 0, value);
  }
  assert.equal(COMFY_SIZES.some((value) => CLOUD_SIZES.includes(value)), true);
  // Nothing is off-menu for ComfyUI: the backend clamps to 64..4096 and otherwise
  // renders what it is handed, so a size stored from elsewhere is kept as-is.
  assert.equal(sizeIsExact(null, true, "704x1408"), true);
});
