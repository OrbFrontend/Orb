// The privacy notice fires exactly when a prompt would leave this machine.
//
// Both directions matter: a banner on every configuration is one users learn to
// click through, and a missing one on a real remote endpoint is a disclosure
// that never happened.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isLoopbackUrl,
  normalizePromptFormat,
  povChoices,
  privacyDisclosure,
  promptFormatLabel,
  PROMPT_FORMATS,
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
