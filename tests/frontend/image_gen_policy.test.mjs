// The privacy notice fires exactly when a prompt would leave this machine.
//
// Both directions matter: a banner on every configuration is one users learn to
// click through, and a missing one on a real remote endpoint is a disclosure
// that never happened.
import assert from "node:assert/strict";
import { test } from "node:test";

import { isLoopbackUrl, povChoices } from "../../frontend/workflows/image_gen/policy.js";

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
