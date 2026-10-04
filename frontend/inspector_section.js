// Shared Inspector section markup for the panel and in-chat blocks.
import { CHEVRON_RIGHT_ICON } from "./icons.js";

/**
 * A section with an open-state *key* collapses; one without keeps an empty
 * chevron slot so every title starts at the same x. *title*, *meta* and *body*
 * are HTML; callers escape what they interpolate.
 */
export function sectionHtml({ title, meta = "", body = "", key = "", open = false, id = "", className = "" }) {
  const attrs = `class="inspector-block${className ? ` ${className}` : ""}"${id ? ` id="${id}"` : ""}`;
  const head = `<h4>${title}</h4>${meta ? `<span class="inspector-meta">${meta}</span>` : ""}`;
  const bodyHtml = body ? `<div class="inspector-body">${body}</div>` : "";
  if (!key) {
    return `<div ${attrs}><div class="inspector-head"><span class="inspector-head-slot"></span>${head}</div>${bodyHtml}</div>`;
  }
  return `<details ${attrs} data-inspect-section="${key}"${open ? " open" : ""}>
    <summary class="inspector-head"><span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>${head}</summary>${bodyHtml}
  </details>`;
}
