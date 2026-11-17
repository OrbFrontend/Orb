import { $ } from "./utils.js";

// Each right panel and the buttons that toggle it. One is open at a time.
const UTILITY_PANELS = {
  "tools-panel": ["tools-panel-btn", "doc-workflow-btn"],
  inspector: ["inspector-toggle"],
  "assistant-panel": ["assistant-btn", "doc-assistant-btn"],
};

// The panel left open, kept in this browser so a reload reopens it.
const LS_OPEN_PANEL = "orb-open-panel";
const NARROW = "(max-width: 900px)";

const watchers = new Map();

function storedOpenPanel() {
  try {
    return localStorage.getItem(LS_OPEN_PANEL);
  } catch {
    return null;
  }
}

function rememberOpen(panelId, open) {
  if (!open && storedOpenPanel() !== panelId) return;
  try {
    localStorage.setItem(LS_OPEN_PANEL, open ? panelId : "");
  } catch {}
}

function setOpen(panelId, open) {
  $(panelId).classList.toggle("open", open);
  for (const btnId of UTILITY_PANELS[panelId]) $(btnId)?.classList.toggle("btn-active", open);
  rememberOpen(panelId, open);
  for (const fn of watchers.get(panelId) || []) fn(open);
}

/** Call *fn* with the new state whenever panel *panelId* opens or closes, whoever opened or closed it. */
export function watchUtilityPanel(panelId, fn) {
  watchers.set(panelId, [...(watchers.get(panelId) || []), fn]);
}

export function openUtilityPanel(panelId, render) {
  const target = $(panelId);
  const others = Object.keys(UTILITY_PANELS).filter((p) => p !== panelId && isUtilityPanelOpen(p));
  const animated = others.length ? [target, ...others.map((p) => $(p))] : [];
  animated.forEach((el) => {
    el.classList.add("no-anim");
  });
  for (const p of others) setOpen(p, false);
  setOpen(panelId, true);
  if (render) render();
  if (others.length) {
    void target.offsetWidth; // commit the swapped state before re-enabling transitions
    animated.forEach((el) => {
      el.classList.remove("no-anim");
    });
  }
}

export function closeUtilityPanel(panelId) {
  if (isUtilityPanelOpen(panelId)) setOpen(panelId, false);
}

export function isUtilityPanelOpen(panelId) {
  return $(panelId).classList.contains("open");
}

/** Reopen *panelId* if it was open when the page was left. A slide-over on the narrow layout stays shut: it would cover the page. */
export function restoreUtilityPanel(panelId, render) {
  if (storedOpenPanel() === panelId && !window.matchMedia?.(NARROW).matches) openUtilityPanel(panelId, render);
}
