import { renderToolsPanel } from "./settings.js";
import { S } from "./state.js";

export async function loadWorkflowModules() {
  let loaded = false;
  for (const w of S.workflowManifest) {
    if (!w || typeof w.id !== "string") continue;
    try {
      await import(`/static/workflows/${w.id}/index.js`);
      loaded = true;
    } catch (e) {
      console.error(`workflow module "${w.id}" failed to load:`, e);
    }
  }
  if (loaded) renderToolsPanel();
}
