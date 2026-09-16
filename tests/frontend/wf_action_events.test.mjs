// `data-wf-on` now names a LIST of events, because a drop target is three
// events on one element. That makes a misspelled event a silent no-op rather
// than an error -- the delegation simply never matches -- so this pins the
// spelling against the dispatcher's own list instead of waiting for a user to
// report a control that does nothing.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const ROOT = new URL("../../frontend/", import.meta.url).pathname;

function jsFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return jsFiles(path);
    return name.endsWith(".js") ? [path] : [];
  });
}

const source = (path) => readFileSync(path, "utf8");

const WIRED = new Set(
  source(join(ROOT, "workflow_api.js"))
    .match(/const _ACTION_EVENTS = \[([^\]]*)\]/)[1]
    .match(/"([a-z]+)"/g)
    .map((quoted) => quoted.slice(1, -1)),
);

test("the dispatcher wires the events it documents", () => {
  for (const event of ["click", "change", "dragover", "dragleave", "drop"]) assert.ok(WIRED.has(event), event);
  const wiring = source(join(ROOT, "workflow_api.js"));
  assert.match(wiring, /for \(const type of _ACTION_EVENTS\) document\.addEventListener/);
});

test("every data-wf-on names events the dispatcher actually listens for", () => {
  let declarations = 0;
  for (const path of jsFiles(ROOT)) {
    for (const [, value] of source(path).matchAll(/data-wf-on="([^"$]*)"/g)) {
      declarations += 1;
      for (const event of value.trim().split(/\s+/)) {
        assert.ok(WIRED.has(event), `${path}: data-wf-on="${value}" names unwired event "${event}"`);
      }
    }
  }
  assert.ok(declarations > 10, "expected the scan to find the panels' declarations");
});
