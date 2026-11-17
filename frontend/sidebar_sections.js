// Left sidebar sections: each header's open state, kept in this browser.

const LS_SECTIONS = "orb-sidebar-sections";

function storedStates() {
  try {
    const states = JSON.parse(localStorage.getItem(LS_SECTIONS) || "{}");
    return states && typeof states === "object" ? states : {};
  } catch {
    return {};
  }
}

/** Collapse or expand the section under *header* without remembering it. */
export function setSectionCollapsed(header, collapsed) {
  header.setAttribute("aria-expanded", String(!collapsed));
  header.querySelector(".arrow")?.classList.toggle("collapsed", collapsed);
  header.nextElementSibling?.classList.toggle("collapsed", collapsed);
}

/** Flip the section under *header* and remember it; returns whether it is now collapsed. */
export function toggleSection(header) {
  const collapsed = !header.nextElementSibling.classList.contains("collapsed");
  setSectionCollapsed(header, collapsed);
  const states = storedStates();
  states[header.getAttribute("aria-controls")] = !collapsed;
  try {
    localStorage.setItem(LS_SECTIONS, JSON.stringify(states));
  } catch {}
  return collapsed;
}

/** Whether the reader has opened or closed section *bodyId* themselves. */
export function sectionRemembered(bodyId) {
  return typeof storedStates()[bodyId] === "boolean";
}

/** Put every section header under *root* back the way the reader left it. */
export function restoreSections(root) {
  const states = storedStates();
  for (const header of root.querySelectorAll(".sidebar-section-header[aria-controls]")) {
    const open = states[header.getAttribute("aria-controls")];
    if (typeof open === "boolean") setSectionCollapsed(header, !open);
  }
}
