import { S } from "./state.js";

const workflowEntry = (w) => (w && typeof w.id === "string" ? `/static/workflows/${w.id}/index.js` : null);

/**
 * Preload workflow entry modules for later sequential imports.
 * Chrome fetches their dependencies only when each entry evaluates.
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

/** Import each workflow entry in manifest order; resolves true if any loaded. */
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
  return loaded;
}
