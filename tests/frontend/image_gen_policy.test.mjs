// The privacy notice fires exactly when a prompt would leave this machine.
//
// Both directions matter: a banner on every configuration is one users learn to
// click through, and a missing one on a real remote endpoint is a disclosure
// that never happened.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addableProviders,
  COMFY_CONNECTION,
  connectionList,
  isLoopbackUrl,
  normalizePromptFormat,
  pendingDisclosures,
  povChoices,
  privacyDisclosure,
  promptFormatLabel,
  PROMPT_FORMATS,
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
  ]) {
    assert.equal(isLoopbackUrl(url), true, url);
  }
});

test("a remote endpoint is warned about", () => {
  for (const url of ["http://192.168.1.40:8188", "https://comfy.example.com", "http://127.0.0.2:8188"]) {
    assert.equal(isLoopbackUrl(url), false, url);
  }
});

test("an unparseable URL is treated as loopback", () => {
  // The backend normalizer replaces it with the loopback default before it can
  // reach any server, so there is no boundary to disclose.
  assert.equal(isLoopbackUrl("not a url"), true);
  assert.equal(isLoopbackUrl(""), true);
});

// The camera picker with and without the classifier. Auto is only a real choice
// while the classifier can answer it; otherwise it draws the fallback camera and
// the picker would be offering the same shot twice.
const ids = ({ modes }) => modes.map(([id]) => id);

test("the classifier makes Auto a real choice", () => {
  const choices = povChoices({ classifier: true, mode: "auto", fallback: "third" });
  assert.deepEqual(ids(choices), ["auto", "first", "third"]);
  assert.equal(choices.selected, "auto");
});

test("without the classifier Auto is dropped and the fallback shown in its place", () => {
  const choices = povChoices({ classifier: false, mode: "auto", fallback: "third" });
  assert.deepEqual(ids(choices), ["first", "third"]);
  assert.equal(choices.selected, "third");
});

test("a hand-pinned camera survives the classifier going away", () => {
  const choices = povChoices({ classifier: false, mode: "first", fallback: "third" });
  assert.equal(choices.selected, "first");
});

// Both style pickers name the prompt format beside the style, so the label must
// be the format the render path will actually use.
test("every stored format has a label", () => {
  for (const [id, label] of PROMPT_FORMATS) {
    assert.equal(normalizePromptFormat(id), id);
    assert.equal(promptFormatLabel(id), label);
  }
});

test("an unset or unknown format reads as the default the backend substitutes", () => {
  for (const value of [undefined, "", "booru", null]) {
    assert.equal(normalizePromptFormat(value), "hybrid");
    assert.equal(promptFormatLabel(value), "Hybrid");
  }
});

// Which disclosure fires, and under which acknowledgement key. The cloud branch
// is the one this module exists for: while ComfyUI was the only source, the panel
// could ask about its URL and be right. The moment cloud is selectable, a config
// with cloud active and the ComfyUI URL still at its loopback default reads as
// "no boundary crossed" — and the warning that should have fired never does.

const comfy = (apiUrl, extra = {}) => privacyDisclosure({ source: "external_comfy", apiUrl, ...extra });
const cloud = (extra = {}) => privacyDisclosure({ source: "cloud", apiUrl: "http://127.0.0.1:8188", ...extra });

test("loopback ComfyUI still gets no notice", () => {
  assert.equal(comfy("http://127.0.0.1:8188"), null);
  assert.equal(comfy("http://localhost:8188"), null);
});

test("a remote ComfyUI keeps today's message and key", () => {
  const notice = comfy("https://comfy.example.com");
  assert.equal(notice.key, "orb:image-gen-privacy:https://comfy.example.com");
  assert.match(notice.message, /not on this machine/);
  assert.doesNotMatch(notice.message, /reference image/);
});

test("a remote ComfyUI with reference-mapping graphs is asked again under its own key", () => {
  const notice = comfy("https://comfy.example.com", { sendsImages: true });
  assert.equal(notice.key, "orb:image-gen-privacy-images:https://comfy.example.com");
  assert.match(notice.message, /reference image/);
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

test("acknowledging one provider does not silently cover a switch to another", () => {
  const xai = cloud({ providerId: "xai", providerLabel: "xAI (Grok)" });
  const openai = cloud({ providerId: "openai", providerLabel: "OpenAI" });
  assert.notEqual(xai.key, openai.key);
  assert.equal(xai.key, "orb:image-gen-privacy-cloud:xai");
});

test("turning cloud reference images on is its own acknowledgement", () => {
  const prompts = cloud({ providerId: "xai", providerLabel: "xAI (Grok)" });
  const images = cloud({ providerId: "xai", providerLabel: "xAI (Grok)", sendsImages: true });
  assert.notEqual(prompts.key, images.key);
  assert.equal(images.key, "orb:image-gen-privacy-cloud-images:xai");
  assert.match(images.message, /character reference/);
  assert.doesNotMatch(prompts.message, /character reference/);
});

test("a cloud disclosure never collides with a ComfyUI one", () => {
  const keys = new Set([
    comfy("https://comfy.example.com").key,
    comfy("https://comfy.example.com", { sendsImages: true }).key,
    cloud({ providerId: "xai" }).key,
    cloud({ providerId: "xai", sendsImages: true }).key,
  ]);
  assert.equal(keys.size, 4);
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
  const list = connectionList(config({ cloud: { providers: { xai: { api_key: "", model: "", base_url: "" } } } }), PROVIDERS);
  assert.deepEqual(list.map((c) => c.id), [COMFY_CONNECTION]);
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
  const only = (providers) => connectionList(config({ cloud: { providers } }), PROVIDERS).at(-1);
  assert.equal(only({ xai: { model: "m" } }).detail, "No API key");
  assert.equal(only({ custom: { api_key: "k", model: "m" } }).detail, "No API base URL");
  // The preset's default model is what a fresh connection is seeded with, so a
  // connection with a key and no explicit model is still ready.
  assert.equal(only({ xai: { api_key: "k" } }).ready, true);
  assert.equal(only({ xai: { api_key: "k" } }).detail, "grok-imagine-image");
});

test("a stored entry whose provider Orb no longer knows is still listed", () => {
  // The backend retains such rows precisely so a rename does not erase a key on
  // the next save; hiding the row here would make that credential unreachable.
  const [, renamed] = connectionList(config({ cloud: { providers: { renamed_in_v2: { api_key: "k" } } } }), PROVIDERS);
  assert.equal(renamed.label, "renamed_in_v2");
  assert.equal(renamed.preset, null);
  assert.equal(renamed.ready, false);
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
    styles: [{ id: "a", connection: COMFY_CONNECTION }, { id: "b", connection: "xai" }],
    cloud: { provider: "xai", providers: { xai: { api_key: "k", reference_source: "previous" } } },
  });
  const keys = pendingDisclosures(next, connectionList(next, PROVIDERS)).map((d) => d.key);
  // And references being on for that one connection picks the bigger wording.
  assert.deepEqual(keys, ["orb:image-gen-privacy-cloud-images:xai"]);
});

test("a connection just added is listed before it holds anything", () => {
  // Several presets ship no default model, so a fresh connection is genuinely
  // empty — and dropping it between the click and the first keystroke would read
  // as the Add button doing nothing.
  const empty = config({ cloud: { providers: { openai: { api_key: "", model: "", base_url: "" } } } });
  assert.deepEqual(
    connectionList(empty, PROVIDERS).map((c) => c.id),
    [COMFY_CONNECTION],
  );
  assert.deepEqual(
    connectionList(empty, PROVIDERS, ["openai"]).map((c) => c.id),
    [COMFY_CONNECTION, "openai"],
  );
});
