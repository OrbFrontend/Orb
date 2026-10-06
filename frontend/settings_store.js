// The one writer of S.settings; every change is announced on the "settings" topic with the keys it touched.
import { api } from "./api.js";
import { toast } from "./notify.js";
import { notify, S } from "./state.js";

const unsaved = new Set(); // patches applied locally whose save has not answered yet

export async function loadSettingsRow() {
  S.settings = Object.assign(await api.get("/settings"), ...unsaved);
  notify("settings", S.settings);
}

/** Apply *patch* now and save it. A failed save restores the keys it changed and rejects. */
export async function saveSettings(patch) {
  const before = S.settings;
  unsaved.add(patch);
  applySettings(patch);
  try {
    const row = await api.put("/settings", patch);
    unsaved.delete(patch);
    S.settings = Object.assign(row, ...unsaved);
  } catch (e) {
    unsaved.delete(patch);
    const restored = Object.fromEntries(Object.keys(patch).map((key) => [key, before[key]]));
    S.settings = Object.assign({ ...S.settings, ...restored }, ...unsaved);
    notify("settings", restored);
    throw e;
  }
}

/** saveSettings with a toast for a failure; *repaint* runs now and again if the save restores the old value. */
export async function persistSettings(patch, repaint = () => {}) {
  const saving = saveSettings(patch);
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
  S.settings = { ...S.settings, ...patch };
  notify("settings", patch);
}
