// The one writer of S.settings; every change is announced on the "settings" topic with the keys it touched.
import { api } from "./api.js";
import { toast } from "./notify.js";
import { notify, S } from "./state.js";

const unsaved = new Set(); // patches applied locally whose save has not answered yet
let queue = Promise.resolve();
let requests = 0;
let savedRow;
let localEdits = {};

// Settings endpoints return full rows, so dispatch them in order while keeping edits optimistic.
function enqueue(operation) {
  if (!requests++) savedRow = S.settings;
  const result = queue.then(operation).finally(() => {
    if (!--requests) {
      savedRow = undefined;
      localEdits = {};
    }
  });
  queue = result.catch(() => {});
  return result;
}

function acceptRow(row) {
  savedRow = { ...row, ...localEdits };
  S.settings = Object.assign({}, savedRow, ...unsaved, localEdits);
}

export async function loadSettingsRow() {
  return enqueue(async () => {
    acceptRow(await api.get("/settings"));
    notify("settings", S.settings);
  });
}

/**
 * Apply *patch* now and save it. A failed save restores the keys it changed and rejects. *wire* is what is sent when it
 * differs from what is shown: a whole map shown locally, with only its changed key sent so the server keeps the others.
 */
export async function saveSettings(patch, wire = patch) {
  const saving = enqueue(async () => {
    try {
      const row = await api.put("/settings", wire);
      unsaved.delete(patch);
      acceptRow(row);
    } catch (e) {
      const before = S.settings;
      unsaved.delete(patch);
      S.settings = Object.assign({}, savedRow, ...unsaved, localEdits);
      const restored = Object.fromEntries(
        Object.keys(patch)
          .filter((key) => before[key] !== S.settings[key])
          .map((key) => [key, S.settings[key]]),
      );
      if (Object.keys(restored).length) notify("settings", restored);
      throw e;
    }
  });
  for (const key of Object.keys(patch)) delete localEdits[key];
  unsaved.add(patch);
  S.settings = { ...S.settings, ...patch };
  notify("settings", patch);
  return saving;
}

/** saveSettings with a toast for a failure; *repaint* runs now and again if the save restores the old value. */
export async function persistSettings(patch, repaint = () => {}, wire = patch) {
  const saving = saveSettings(patch, wire);
  repaint();
  try {
    await saving;
  } catch (_e) {
    toast("Failed to save setting", true);
    repaint();
  }
}

/** Change the local row without a save: a value another endpoint already stored, or an edit saved later. */
export function applySettings(patch) {
  if (requests) {
    Object.assign(localEdits, patch);
    savedRow = { ...savedRow, ...patch };
  }
  S.settings = { ...S.settings, ...patch };
  notify("settings", patch);
}

/**
 * Read *keys* again from the server, in turn with this tab's saves: a save still in flight answers first, so the older row
 * it replaces can never override it, and a save queued after keeps its local value until it answers.
 */
export async function reloadSettingKeys(keys) {
  return enqueue(async () => {
    const row = await api.get("/settings");
    const patch = Object.fromEntries(keys.filter((key) => key in row).map((key) => [key, row[key]]));
    if (!Object.keys(patch).length) return;
    for (const key of Object.keys(patch)) delete localEdits[key];
    savedRow = { ...savedRow, ...patch };
    S.settings = Object.assign({}, S.settings, patch, ...unsaved);
    notify("settings", patch);
  });
}
