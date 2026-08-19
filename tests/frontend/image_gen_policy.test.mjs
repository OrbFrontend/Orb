// The privacy notice fires exactly when a prompt would leave this machine.
//
// Both directions matter: a banner on every configuration is one users learn to
// click through, and a missing one on a real remote endpoint is a disclosure
// that never happened.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addableProviders,
  CLOUD_SIZES,
  COMFY_CONNECTION,
  COMFY_SIZES,
  connectionList,
  isLoopbackUrl,
  maxCloudReferences,
  providerTakesReferences,
  normalizePromptFormat,
  pendingDisclosures,
  povChoices,
  privacyDisclosure,
  promptFormatLabel,
  PROMPT_FORMATS,
  sizeChoices,
  sizeIsExact,
  styleConnectionId,
} from "../../frontend/workflows/image_gen/policy.js";

test("loopback in every form Orb can be configured with gets no notice", () => {
  for (const url of [
    "http://127.0.0.1:8188",
    "http://localhost:8188",
    "https://LOCALHOST:8188/",
    // URL.hostname keeps the brackets on an IPv6 literal, so a bare "::1"
    // comparison silently warns on a loopback server.
    "http://[::1]:8188",
    "http://[0:0:0:0:0:0:0:1]:8188",
    // An unparseable URL is replaced by the backend normalizer before it can reach
    // any server, so there is no boundary to disclose.
    "not a url",
    "",
  ]) {
    assert.equal(isLoopbackUrl(url), true, url);
  }
});

test("a remote endpoint is warned about", () => {
  for (const url of ["http://192.168.1.40:8188", "https://comfy.example.com", "http://127.0.0.2:8188"]) {
    assert.equal(isLoopbackUrl(url), false, url);
  }
});

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

// Which disclosure fires, and under which acknowledgement key. The cloud branch is
// the one this module exists for: while ComfyUI was the only source the panel could
// ask about its URL and be right, but the moment cloud is selectable, a config with
// cloud active and the ComfyUI URL still at its loopback default reads as "no
// boundary crossed" — and the warning that should have fired never does.

const comfy = (apiUrl, extra = {}) => privacyDisclosure({ source: "external_comfy", apiUrl, ...extra });
const cloud = (extra = {}) => privacyDisclosure({ source: "cloud", apiUrl: "http://127.0.0.1:8188", ...extra });

test("loopback ComfyUI still gets no notice", () => {
  assert.equal(comfy("http://127.0.0.1:8188"), null);
  assert.equal(comfy("http://localhost:8188"), null);
});

test("a remote ComfyUI is disclosed, and its reference images under their own key", () => {
  const prompts = comfy("https://comfy.example.com");
  assert.equal(prompts.key, "orb:image-gen-privacy:https://comfy.example.com");
  assert.match(prompts.message, /not on this machine/);
  assert.doesNotMatch(prompts.message, /reference image/);

  // Uploading conversation images is a materially bigger disclosure than sending
  // prompt text, so a user who accepted the prompt-only wording is asked again.
  const images = comfy("https://comfy.example.com", { sendsImages: true });
  assert.equal(images.key, "orb:image-gen-privacy-images-v2:https://comfy.example.com");
  assert.match(images.message, /Reference images are turned on/);
});

test("the reference disclosure names every likeness a row can reach", () => {
  // A cast row uploads the *other* members who spoke in the exchange, not only the
  // character the picture is of. Saying "your character reference photo" and stopping
  // describes less than what leaves the machine.
  for (const notice of [
    comfy("https://comfy.example.com", { sendsImages: true }),
    cloud({ providerId: "xai", providerLabel: "xAI (Grok)", sendsImages: true }),
  ]) {
    assert.match(notice.message, /character reference photo or card art/);
    assert.match(notice.message, /other characters who spoke in that exchange/);
    // And the key moved with the wording, so consent given to the narrower sentence
    // does not silently cover the wider one.
    assert.match(notice.key, /-images-v2/);
  }
});

test("cloud always discloses, even with the ComfyUI URL left at loopback", () => {
  // The exact configuration that swallows the warning if the gate stays on
  // `external_comfy` — which is what makes this the regression worth pinning.
  const notice = cloud({ providerId: "xai", providerLabel: "xAI (Grok)" });
  assert.notEqual(notice, null);
  assert.match(notice.message, /xAI \(Grok\)/);
  assert.match(notice.message, /third-party/);
  // Cloud says more than ComfyUI does: this one bills, and the provider may keep it.
  assert.match(notice.message, /billed/);
  assert.match(notice.message, /retain/);
});

test("every acknowledgement key is its own, per provider and per boundary", () => {
  const xai = cloud({ providerId: "xai", providerLabel: "xAI (Grok)" });
  const xaiImages = cloud({ providerId: "xai", providerLabel: "xAI (Grok)", sendsImages: true });
  assert.equal(xai.key, "orb:image-gen-privacy-cloud:xai");
  assert.equal(xaiImages.key, "orb:image-gen-privacy-cloud-images-v2:xai");
  assert.match(xaiImages.message, /character reference/);
  assert.doesNotMatch(xai.message, /character reference/);

  // Acknowledging one provider does not silently cover a switch to another, and no
  // cloud key ever collides with a ComfyUI one.
  const keys = new Set([
    xai.key,
    xaiImages.key,
    cloud({ providerId: "openai", providerLabel: "OpenAI" }).key,
    comfy("https://comfy.example.com").key,
    comfy("https://comfy.example.com", { sendsImages: true }).key,
  ]);
  assert.equal(keys.size, 5);
});

// ── connections ──────────────────────────────────────────────────────────────
//
// The connection list is derived from the credentials rather than stored beside
// them, so the interesting cases are all about *which* stored rows count as a
// connection the user made — and what a style pointing at one resolves to.

const PROVIDERS = [
  { id: "xai", label: "xAI (Grok)", needs_base_url: false, default_model: "grok-imagine-image" },
  { id: "openai", label: "OpenAI", needs_base_url: false, default_model: "gpt-image-1" },
  { id: "custom", label: "Custom (OpenAI-compatible)", needs_base_url: true, default_model: "" },
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

// One disclosure per connection a style can reach. The old panel asked about the
// active source alone, which becomes a hole the moment a save can light up a
// second remote backend without it ever being active.
test("every linked remote connection is disclosed, and only those", () => {
  const next = config({
    source: "external_comfy",
    styles: [{ id: "a", connection: COMFY_CONNECTION }, { id: "b", connection: "xai" }],
    external_comfy: { api_url: "https://comfy.example.com", user_graphs: [] },
    cloud: { provider: "xai", providers: { xai: { api_key: "k" }, openai: { api_key: "k" } } },
  });
  const keys = pendingDisclosures(next, connectionList(next, PROVIDERS)).map((d) => d.key);
  // OpenAI is configured but nothing points at it, so nothing crosses its boundary.
  assert.deepEqual(keys, ["orb:image-gen-privacy:https://comfy.example.com", "orb:image-gen-privacy-cloud:xai"]);
});

test("a loopback ComfyUI style adds no question to a cloud save", () => {
  const next = config({
    styles: [
      { id: "a", connection: COMFY_CONNECTION },
      { id: "b", connection: "xai", reference_sources: ["previous"] },
    ],
    cloud: { provider: "xai", providers: { xai: { api_key: "k" } } },
  });
  const keys = pendingDisclosures(next, connectionList(next, PROVIDERS)).map((d) => d.key);
  // And references being on for that one connection picks the bigger wording.
  assert.deepEqual(keys, ["orb:image-gen-privacy-cloud-images-v2:xai"]);
});

test("one style with references on is enough to ask the larger cloud question", () => {
  // Reference images are a style setting now, so asking the *connection* would miss
  // the case that matters: a provider carrying a text-only style and an edit style
  // still receives conversation images, and the prompt-only wording would not say so.
  const next = config({
    styles: [
      { id: "a", connection: "xai" },
      { id: "b", connection: "xai", reference_sources: ["character"] },
    ],
    cloud: { provider: "xai", providers: { xai: { api_key: "k" } } },
  });
  const keys = pendingDisclosures(next, connectionList(next, PROVIDERS)).map((d) => d.key);
  assert.deepEqual(keys, ["orb:image-gen-privacy-cloud-images-v2:xai"]);

  // And with every style on it prompt-only, the smaller question is the honest one.
  const off = config({
    styles: [{ id: "a", connection: "xai" }],
    cloud: { provider: "xai", providers: { xai: { api_key: "k" } } },
  });
  assert.deepEqual(
    pendingDisclosures(off, connectionList(off, PROVIDERS)).map((d) => d.key),
    ["orb:image-gen-privacy-cloud:xai"],
  );
});

test("a remote ComfyUI is asked the image question by its styles, not by its imports", () => {
  // It used to be asked whether *any* imported graph mapped a slot. That over-asked
  // for a server no style pointed a reference at, and went on asking after every
  // style had switched them off — a graph is global, so one import spoke for all.
  const graphs = [{ id: "g", slots: { references: [{ slot: ["11", "image"], label: "Load Image (#11)" }] } }];
  const external = { api_url: "https://comfy.example.com", user_graphs: graphs };
  const off = config({
    styles: [{ id: "a", connection: COMFY_CONNECTION, workflow: "g" }],
    external_comfy: external,
  });
  assert.deepEqual(
    pendingDisclosures(off, connectionList(off, PROVIDERS)).map((d) => d.key),
    ["orb:image-gen-privacy:https://comfy.example.com"],
  );

  const on = config({
    styles: [
      { id: "a", connection: COMFY_CONNECTION, workflow: "g" },
      { id: "b", connection: COMFY_CONNECTION, workflow: "g", reference_sources: ["character"] },
    ],
    external_comfy: external,
  });
  assert.deepEqual(
    pendingDisclosures(on, connectionList(on, PROVIDERS)).map((d) => d.key),
    ["orb:image-gen-privacy-images-v2:https://comfy.example.com"],
  );
});

test("a provider's reference capacity is whatever the backend derived, floored at one", () => {
  // The count is derived server-side from the reference encoding
  // (`providers.reference_capacity`) rather than hand-measured, so there is no table
  // here to fall behind. A provider the panel has never heard of reads as one, which is
  // a safe floor rather than a claim.
  assert.equal(maxCloudReferences(null), 1);
  assert.equal(maxCloudReferences({ id: "xai" }), 1);
  assert.equal(maxCloudReferences({ id: "xai", max_references: 3 }), 3);
  // Bounded by the same cap the style's stored list is, and never below one.
  assert.equal(maxCloudReferences({ max_references: 99 }), 4);
  assert.equal(maxCloudReferences({ max_references: 0 }), 1);
});

test("a source stored for a slot the render target does not have is not an upload", () => {
  // A style keeps both backends' answers across a relink, so its stored list outlives
  // the target that shaped it. Reading it raw asks the user to approve an upload the
  // panel shows as Off and no adapter makes — the disclosure has to match the render.
  const cloud = config({
    // Slot 0 off, slot 1 on: a two-`LoadImage` ComfyUI style, since relinked to xAI,
    // which declares one slot and so reads position 0 alone.
    styles: [{ id: "a", connection: "xai", reference_sources: ["", "character"] }],
    cloud: { provider: "xai", providers: { xai: { api_key: "k" } } },
  });
  assert.deepEqual(
    pendingDisclosures(cloud, connectionList(cloud, PROVIDERS)).map((d) => d.key),
    ["orb:image-gen-privacy-cloud:xai"],
  );

  // A provider probed for a second slot reads position 1, so the same stored list now
  // is an upload — the capacity is what decides, never the stored length.
  const roomy = connectionList(cloud, [{ ...PROVIDERS[0], max_references: 2 }]);
  assert.deepEqual(
    pendingDisclosures(cloud, roomy).map((d) => d.key),
    ["orb:image-gen-privacy-cloud-images-v2:xai"],
  );

  // The same in the other direction: a workflow that loads no image at all declares no
  // slot for the answer left over from the one before it.
  const comfy = config({
    styles: [{ id: "a", connection: COMFY_CONNECTION, workflow: "t2i", reference_sources: ["character"] }],
    external_comfy: { api_url: "https://comfy.example.com", user_graphs: [{ id: "t2i", slots: {} }] },
  });
  assert.deepEqual(
    pendingDisclosures(comfy, connectionList(comfy, PROVIDERS)).map((d) => d.key),
    ["orb:image-gen-privacy:https://comfy.example.com"],
  );
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
  // A model that will not take a reference refuses at render time, for free, and the
  // render degrades one rung and says so.
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
