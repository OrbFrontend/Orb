import { renderToolsPanel } from "./settings.js";
import { S } from "./state.js";

const workflowEntry = (w) => (w && typeof w.id === "string" ? `/static/workflows/${w.id}/index.js` : null);

/**
 * Start fetching every workflow's entry module without evaluating it, so the
 * sequential imports in loadWorkflowModules find those files already in flight.
 * Chrome preloads only the named module; each entry's own imports still load
 * when it evaluates.
 */
export function preloadWorkflowModules() {
  for (const w of S.workflowManifest) {
    const href = workflowEntry(w);
    if (!href) continue;
    const link = document.createElement("link");
    link.rel = "modulepreload";
    link.href = href;
    document.head.appendChild(link);
  }
}

export async function loadWorkflowModules() {
  let loaded = false;
  for (const w of S.workflowManifest) {
    const href = workflowEntry(w);
    if (!href) continue;
    try {
      await import(href);
      loaded = true;
    } catch (e) {
      console.error(`workflow module "${w.id}" failed to load:`, e);
    }
  }
  if (loaded) renderToolsPanel();
}
