// Sizes the reader sets by hand, kept per device: a panel's width (drag its edge), a region's scale (pinch or
// Ctrl+wheel over it), and the height cap of a scrolling box (its native resize grip). Everything lands in
// custom properties on the root, so markup that is rebuilt keeps its size and the stylesheets own the defaults.

const LS_KEY = "orb-ui-sizing";
const SCALE_MIN = 0.75;
const SCALE_MAX = 2;
const SCALE_SNAP = 0.03; // a scale this close to 1 shows as exactly 1, so the default is easy to return to
const WHEEL_STEP_CAP = 50; // one mouse notch (~100) counts as this much; pinch deltas are far smaller
const WHEEL_GAIN = 0.002;
const PANEL_MIN_PX = 200; // on screen
const PANEL_MAX_FRACTION = 0.45; // of the window, so the two panels cannot close the chat between them
const EDGE_GRAB_PX = 5;
const BOX_MIN_PX = 60;

// `edge` is the panel side that borders the chat. Its grab zone sits on the side with no scrollbar.
const REGIONS = {
  sidebar: { selector: "#sidebar", scaleVar: "--sidebar-scale", widthVar: "--sidebar-width", edge: "right" },
  main: { selector: "#main, #document-main", scaleVar: "--main-scale" },
  panel: {
    selector: "#inspector, #tools-panel",
    scaleVar: "--panel-scale",
    widthVar: "--inspector-width",
    edge: "left",
  },
};

// First match wins.
const BOXES = [
  { selector: ".reasoning-section .inspect-raw", cssVar: "--reasoning-box-height", key: "reasoning" },
  { selector: ".inspect-raw", cssVar: "--raw-box-height", key: "raw" },
  { selector: ".state-history", cssVar: "--state-history-height", key: "stateHistory" },
];
const BOX_SELECTOR = BOXES.map((b) => b.selector).join(", ");

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

/** The scale a wheel or pinch step of *deltaY* moves *scale* to. */
export function steppedScale(scale, deltaY) {
  const step = clamp(-deltaY, -WHEEL_STEP_CAP, WHEEL_STEP_CAP) * WHEEL_GAIN;
  return clamp(scale * Math.exp(step), SCALE_MIN, SCALE_MAX);
}

/** The scale applied for a stored one: exactly 1 near 1, two decimals otherwise. */
export function shownScale(scale) {
  if (!(scale >= SCALE_MIN && scale <= SCALE_MAX) || Math.abs(scale - 1) < SCALE_SNAP) return 1;
  return Math.round(scale * 100) / 100;
}

/** A panel's own (unscaled) width for an on-screen width of *visible* px, kept inside the window's bounds. */
export function panelWidth(visible, scale, windowWidth) {
  const most = Math.max(PANEL_MIN_PX, windowWidth * PANEL_MAX_FRACTION);
  return Math.round(clamp(visible, PANEL_MIN_PX, most) / scale);
}

/**
 * *px* measured on screen (a bounding rect, a pointer position) in the units *el* lays out and scrolls in.
 * The two differ inside a scaled region, so a rect delta added to `scrollTop` or written to a style goes through this.
 */
export function localPx(el, px) {
  return px / (el.currentCSSZoom || 1);
}

const numbers = (value) =>
  Object.fromEntries(Object.entries(value || {}).filter(([, n]) => typeof n === "number" && Number.isFinite(n)));

/** Stored sizing as `{ scale, width, box }` maps of numbers; anything unreadable is dropped. */
export function parseSizing(text) {
  let raw = null;
  try {
    raw = JSON.parse(text || "null");
  } catch {}
  return { scale: numbers(raw?.scale), width: numbers(raw?.width), box: numbers(raw?.box) };
}

let sizing = parseSizing(null);
let frame = 0;
// Scrollers that sat at their end when a scale gesture began: fewer lines fit afterwards, and they should still end
// there. Rows scrolled into view take their real height a frame later, so the gesture's end pins once more.
const atEnd = new Set();
const END_SLACK_PX = 20;
const GESTURE_IDLE_MS = 200;
let gestureTimer = 0;

function pinEnds() {
  for (const el of atEnd) el.scrollTop = el.scrollHeight;
}

function save() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(sizing));
  } catch {}
}

function apply() {
  frame = 0;
  const root = document.documentElement.style;
  const set = (name, value) => (value == null ? root.removeProperty(name) : root.setProperty(name, value));
  for (const [name, region] of Object.entries(REGIONS)) {
    const scale = shownScale(sizing.scale[name]);
    set(region.scaleVar, scale === 1 ? null : String(scale));
    if (!region.widthVar) continue;
    const width = sizing.width[name];
    // Re-fit a stored width to this window: it may have been set on a wider one.
    set(region.widthVar, width ? `${panelWidth(width * scale, scale, window.innerWidth)}px` : null);
  }
  for (const box of BOXES) set(box.cssVar, sizing.box[box.key] ? `${sizing.box[box.key]}px` : null);
  pinEnds();
}

function changed() {
  save();
  if (!frame) frame = requestAnimationFrame(apply);
}

/** Apply the stored sizes and start listening for the gestures that change them. */
export function initUiSizing() {
  try {
    sizing = parseSizing(localStorage.getItem(LS_KEY));
  } catch {}
  apply();

  const app = document.getElementById("app");
  if (!app) return;
  // The narrow layout turns the panels into overlays with their own widths; sizes are a wide-layout matter.
  const narrow = window.matchMedia("(max-width: 900px)");
  const regionOf = (el) => Object.keys(REGIONS).find((name) => el.closest?.(REGIONS[name].selector));

  // -- Scale: pinch or Ctrl+wheel over a region --
  app.addEventListener(
    "wheel",
    (e) => {
      if (!e.ctrlKey || narrow.matches) return; // a trackpad pinch arrives as a Ctrl+wheel
      const name = regionOf(e.target);
      if (!name) return;
      e.preventDefault();
      for (let el = e.target; el && el !== app; el = el.parentElement) {
        const below = el.scrollHeight - el.clientHeight - el.scrollTop;
        if (el.scrollTop > 0 && below <= END_SLACK_PX) atEnd.add(el);
      }
      clearTimeout(gestureTimer);
      gestureTimer = setTimeout(() => {
        pinEnds();
        requestAnimationFrame(() => {
          pinEnds();
          atEnd.clear();
        });
      }, GESTURE_IDLE_MS);
      const deltaY = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY; // lines to pixels
      sizing.scale[name] = steppedScale(sizing.scale[name] || 1, deltaY);
      changed();
    },
    { passive: false },
  );

  // -- Width: drag a panel's inner edge; double-click it to return the panel to its defaults --
  const edgeAt = (e) => {
    if (narrow.matches || e.target.closest?.(".avatar-popup")) return null;
    for (const [name, region] of Object.entries(REGIONS)) {
      if (!region.edge) continue;
      const el = [...document.querySelectorAll(region.selector)].find((p) => p.offsetWidth > 0);
      if (!el) continue;
      const rect = el.getBoundingClientRect();
      const into = region.edge === "right" ? e.clientX - rect.right : e.clientX - rect.left;
      if (into >= -1 && into <= EDGE_GRAB_PX) return { name, el };
    }
    return null;
  };

  let drag = null;
  app.addEventListener("pointermove", (e) => {
    if (!drag) {
      app.classList.toggle("ui-edge-hover", Boolean(edgeAt(e)));
      return;
    }
    const rect = drag.el.getBoundingClientRect();
    const visible = REGIONS[drag.name].edge === "right" ? e.clientX - rect.left : rect.right - e.clientX;
    sizing.width[drag.name] = panelWidth(visible, shownScale(sizing.scale[drag.name]), window.innerWidth);
    changed();
  });
  app.addEventListener("pointerleave", () => {
    if (!drag) app.classList.remove("ui-edge-hover");
  });
  app.addEventListener(
    "pointerdown",
    (e) => {
      const edge = e.button === 0 && edgeAt(e);
      if (!edge) return;
      e.preventDefault(); // no text selection, no press on whatever lies under the edge
      e.stopPropagation();
      drag = edge;
      app.setPointerCapture(e.pointerId);
      app.classList.add("ui-resizing");
    },
    true,
  );
  const endDrag = () => {
    drag = null;
    app.classList.remove("ui-resizing");
  };
  app.addEventListener("pointerup", endDrag);
  app.addEventListener("pointercancel", endDrag);
  app.addEventListener(
    "dblclick",
    (e) => {
      const edge = edgeAt(e);
      if (!edge) return;
      e.preventDefault();
      e.stopPropagation();
      delete sizing.width[edge.name];
      delete sizing.scale[edge.name];
      changed();
    },
    true,
  );

  // -- Box height: the native grip writes an inline height; keep it as that kind of box's cap instead --
  let pressed = null;
  document.addEventListener("pointerdown", (e) => (pressed = e.target.closest?.(BOX_SELECTOR) || null), true);
  const keepBoxHeight = () => {
    const el = pressed;
    pressed = null;
    const height = parseFloat(el?.style.height);
    if (!height) return;
    el.style.height = "";
    const box = BOXES.find((b) => el.matches(b.selector));
    sizing.box[box.key] = Math.max(BOX_MIN_PX, Math.round(height));
    changed();
  };
  window.addEventListener("pointerup", keepBoxHeight, true);
  window.addEventListener("mouseup", keepBoxHeight, true);
}
