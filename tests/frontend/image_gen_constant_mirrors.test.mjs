// The image_gen panel hand-mirrors a dozen limits and enums from the Python
// normalizer, because the picker has to gate a file *before* any query resolves —
// there is no payload to read them from at that moment.
//
// The backend values are read straight out of the .py sources rather than duplicated
// here: a test that restates the number it is guarding proves only that someone typed
// it twice.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// policy.js is the one panel module free of the plugin facade, so it is the only one
// this test can import; everything else is read as source. That is not a workaround —
// a constant the picker uses before any fetch resolves is a *literal*, and reading
// the literal is what proves the two literals match.
import {
  DEFAULT_PROMPT_FORMAT,
  MAX_REFERENCE_SLOTS,
  POV_MODES,
  PROMPT_FORMATS,
} from "../../frontend/workflows/image_gen/policy.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (p) => readFileSync(join(root, p), "utf8");

const config = read("backend/workflows/image_gen/config.py");
const pov = read("backend/workflows/image_gen/pov.py");
const panel = read("frontend/workflows/image_gen/config_panel.js");
const graphImport = read("frontend/workflows/image_gen/graph_import.js");
const profile = read("frontend/workflows/image_gen/character_profile.js");

/** One `NAME = <int>` from a Python source, underscores stripped. */
function pyInt(source, name) {
  const m = source.match(new RegExp(`^${name}\\s*=\\s*([\\d_]+)`, "m"));
  assert.ok(m, `backend constant ${name} not found — did it get renamed?`);
  return Number(m[1].replaceAll("_", ""));
}

/** One `NAME = ("a", "b")` tuple of string literals from a Python source. */
function pyStrTuple(source, name) {
  const m = source.match(new RegExp(`^${name}\\s*=\\s*\\(([^)]*)\\)`, "m"));
  assert.ok(m, `backend constant ${name} not found — did it get renamed?`);
  return [...m[1].matchAll(/"([^"]*)"/g)].map((x) => x[1]);
}

/** One `NAME = {"a": ..., "b": ...}` mapping's keys. */
function pyDictKeys(source, name) {
  const m = source.match(new RegExp(`^${name}\\s*=\\s*\\{([^}]*)\\}`, "m"));
  assert.ok(m, `backend constant ${name} not found — did it get renamed?`);
  return [...m[1].matchAll(/"([^"]+)"\s*:/g)].map((x) => x[1]);
}

/** One `const NAME = <int>;` from a JS source. */
function jsInt(source, name) {
  const m = source.match(new RegExp(`const ${name}\\s*=\\s*([\\d_]+)`));
  assert.ok(m, `frontend constant ${name} not found — did it get renamed?`);
  return Number(m[1].replaceAll("_", ""));
}

test("graph size cap agrees, or the importer and the normalizer refuse different files", () => {
  assert.equal(jsInt(graphImport, "MAX_GRAPH_BYTES"), pyInt(config, "MAX_GRAPH_BYTES"));
});

test("collection caps agree, or the panel lets the user build what the server drops", () => {
  assert.equal(MAX_REFERENCE_SLOTS, pyInt(config, "MAX_REFERENCE_SLOTS"));
  assert.equal(jsInt(panel, "MAX_USER_GRAPHS"), pyInt(config, "MAX_USER_GRAPHS"));
});

test("the default cloud edge agrees, so an unsized entry previews what it renders", () => {
  assert.equal(jsInt(panel, "DEFAULT_EDGE"), pyInt(config, "DEFAULT_CLOUD_EDGE"));
});

test("the reference image budget agrees with the base64 cap it is derived from", () => {
  // The panel gates raw bytes; the normalizer stores base64, which inflates by 4/3.
  const b64Cap = pyInt(config, "MAX_REFERENCE_IMAGE_B64");
  const rawCap = jsInt(profile, "MAX_REFERENCE_IMAGE_BYTES");
  assert.equal(rawCap, Math.floor((b64Cap * 3) / 4 / 1_000_000) * 1_000_000);
});

test("accepted reference mimes agree, or the picker offers a file the server drops", () => {
  const offered = [...profile.match(/const REFERENCE_IMAGE_MIMES = \[([^\]]*)\]/)[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  assert.deepEqual(offered.sort(), pyDictKeys(config, "MIME_EXTENSIONS").sort());
});

test("prompt formats agree with the normalizer's enum and default", () => {
  assert.deepEqual(
    PROMPT_FORMATS.map(([id]) => id),
    pyStrTuple(config, "PROMPT_FORMATS"),
  );
  const m = config.match(/^DEFAULT_PROMPT_FORMAT\s*=\s*"([^"]+)"/m);
  assert.ok(m, "backend DEFAULT_PROMPT_FORMAT not found");
  assert.equal(DEFAULT_PROMPT_FORMAT, m[1]);
});

test("camera modes agree, so the picker cannot offer a mode pov.resolve rejects", () => {
  assert.deepEqual(
    POV_MODES.map(([id]) => id),
    pyStrTuple(pov, "POV_MODES"),
  );
});

test("reference sources agree with the normalizer's resolution table", () => {
  // The panel's menu excludes nothing: every key the backend resolves is offerable.
  const backend = [...config.matchAll(/^\s{4}"(previous|character|previous_or_character|character_and_previous)":/gm)].map((x) => x[1]);
  const offered = [...panel.matchAll(/\["(previous|character|previous_or_character|character_and_previous)",/g)].map((x) => x[1]);
  assert.deepEqual(offered.sort(), backend.sort());
});

test("cloud qualities agree, or the panel offers one the normalizer blanks", () => {
  const offered = [...panel.matchAll(/^\s{2}\["(low|medium|high)",/gm)].map((x) => x[1]);
  assert.deepEqual(offered, pyStrTuple(config, "CLOUD_QUALITIES"));
});
