import { $ } from "./utils.js";

// The right rail hosts three mutually-exclusive utility panels sharing one slot.
const UTILITY_PANELS = [
  ["tools-panel", "tools-panel-btn"],
  ["inspector", "inspector-toggle"],
  ["direction-notes-panel", "direction-notes-panel-btn"],
];

function clearActive(btnId) {
  const btn = $(btnId);
  if (btn) btn.classList.remove("btn-active");
}

// Open one panel and close the others. When another panel was already open the
// two swap in place with no slide -- they share width and position in the slot.
export function openUtilityPanel(panelId, btnId, render) {
  const target = $(panelId);
  const others = UTILITY_PANELS.filter(([p]) => p !== panelId);
  const swapping = others.some(([p]) => $(p).classList.contains("open"));
  const animated = swapping ? [target, ...others.map(([p]) => $(p))] : [];
  animated.forEach((el) => {
    el.classList.add("no-anim");
  });
  for (const [p, b] of others) {
    $(p).classList.remove("open");
    clearActive(b);
  }
  target.classList.add("open");
  const btn = $(btnId);
  if (btn) btn.classList.add("btn-active");
  if (render) render();
  if (swapping) {
    void target.offsetWidth; // commit the swapped state before re-enabling transitions
    animated.forEach((el) => {
      el.classList.remove("no-anim");
    });
  }
}

export function closeUtilityPanel(panelId, btnId) {
  $(panelId).classList.remove("open");
  clearActive(btnId);
}

export function isUtilityPanelOpen(panelId) {
  return $(panelId).classList.contains("open");
}
