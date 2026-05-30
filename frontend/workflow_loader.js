import { S } from "./state.js";
import { renderToolsPanel } from "./settings.js";

// Imports run sequentially (await in the loop, not Promise.all) so each
// module's top-level registry pushes land in manifest order; the renderer
// arrays in S are iterated in push order, so this fixes render order in the
// secondary tabs.
export async function loadWorkflowModules() {
  let loaded = false;
  for (const w of S.workflowManifest) {
    if (!w || typeof w.id !== "string") continue;
    try {
      await import(`/static/workflows/${w.id}/index.js`);
      loaded = true;
    } catch (e) {
      // A backend-only workflow ships no module (expected 404); a present
      // module that throws is contained here so it cannot abort the others.
      console.error(`workflow module "${w.id}" failed to load:`, e);
    }
  }
  // The tools panel renders once at startup, before these modules load, and
  // switching to its secondary tab only toggles visibility without
  // re-rendering -- so a freshly registered tools-panel renderer would stay
  // hidden behind the stale paint. The other workflow surfaces first render
  // after startup and read the registries live, so they do not need this.
  if (loaded) renderToolsPanel();
}
