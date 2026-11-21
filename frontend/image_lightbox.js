/** Full-screen image viewer: opens fitted, zooms with wheel, pinch, double-click and keys, pans by drag. */
import { CLOSE_ICON, MINUS_ICON, PLUS_ICON } from "./icons.js";

const MARGIN = 0.95;
const MAX_SCALE = 8;
const STEP = 1.25;
const TAP_SLOP = 6;
const DOUBLE_TAP_MS = 300;

export function openImageLightbox(src, alt) {
  const box = document.createElement("div");
  box.className = "image-lightbox";
  const img = document.createElement("img");
  img.alt = alt;
  img.draggable = false;
  const bar = document.createElement("div");
  bar.className = "image-lightbox-bar";
  const button = (html, title, run) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "image-lightbox-btn";
    b.innerHTML = html;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.addEventListener("click", run);
    bar.appendChild(b);
    return b;
  };
  let fit = 1;
  let scale = 1;
  let tx = 0;
  let ty = 0;

  const view = () => box.getBoundingClientRect();
  const minScale = () => Math.min(fit, 1);
  const maxScale = () => Math.max(MAX_SCALE, minScale() * 4);

  const render = (animate = false) => {
    const { width: vw, height: vh } = view();
    const w = img.naturalWidth * scale;
    const h = img.naturalHeight * scale;
    tx = w <= vw ? (vw - w) / 2 : Math.min(0, Math.max(vw - w, tx));
    ty = h <= vh ? (vh - h) / 2 : Math.min(0, Math.max(vh - h, ty));
    img.classList.toggle("animating", animate);
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    box.classList.toggle("pannable", w > vw + 0.5 || h > vh + 0.5);
    zoomLabel.textContent = `${Math.round(scale * 100)}%`;
  };

  const zoomTo = (next, cx, cy, animate = false) => {
    const { width: vw, height: vh } = view();
    const px = cx ?? vw / 2;
    const py = cy ?? vh / 2;
    const clamped = Math.min(maxScale(), Math.max(minScale(), next));
    tx = px - ((px - tx) * clamped) / scale;
    ty = py - ((py - ty) * clamped) / scale;
    scale = clamped;
    render(animate);
  };

  const refit = () => {
    const { width: vw, height: vh } = view();
    if (!img.naturalWidth || !img.naturalHeight) return;
    const atFit = Math.abs(scale - minScale()) < 1e-3;
    fit = Math.min((vw * MARGIN) / img.naturalWidth, (vh * MARGIN) / img.naturalHeight);
    if (atFit) scale = minScale();
    else scale = Math.min(maxScale(), Math.max(minScale(), scale));
    render();
  };

  const toggleZoom = (cx, cy) => {
    if (scale > minScale() + 1e-3) zoomTo(minScale(), cx, cy, true);
    else zoomTo(Math.max(1, minScale() * 2), cx, cy, true);
  };

  button(MINUS_ICON, "Zoom out (−)", () => zoomTo(scale / STEP, undefined, undefined, true));
  const zoomLabel = button("", "Fit to screen (0)", () => zoomTo(minScale(), undefined, undefined, true));
  zoomLabel.classList.add("image-lightbox-zoom");
  button(PLUS_ICON, "Zoom in (+)", () => zoomTo(scale * STEP, undefined, undefined, true));
  button("1:1", "Actual size (1)", () => zoomTo(1, undefined, undefined, true)).classList.add("image-lightbox-zoom");
  button(CLOSE_ICON, "Close (Esc)", () => close());

  const pointers = new Map();
  let moved = false;
  let pinch = null;
  let lastTap = null;
  let downAt = null;

  const local = (e) => {
    const r = view();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const pinchState = () => {
    const [a, b] = [...pointers.values()];
    return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  };

  box.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".image-lightbox-bar") || e.button !== 0) return;
    box.setPointerCapture(e.pointerId);
    const p = local(e);
    pointers.set(e.pointerId, p);
    if (pointers.size === 1) {
      moved = false;
      downAt = { ...p, onImage: e.target === img };
    } else if (pointers.size === 2) {
      moved = true;
      pinch = pinchState();
    }
  });

  box.addEventListener("pointermove", (e) => {
    const prev = pointers.get(e.pointerId);
    if (!prev) return;
    const p = local(e);
    pointers.set(e.pointerId, p);
    if (pointers.size >= 2 && pinch) {
      const now = pinchState();
      tx += now.x - pinch.x;
      ty += now.y - pinch.y;
      zoomTo((scale * now.dist) / pinch.dist, now.x, now.y);
      pinch = now;
      return;
    }
    if (!moved && Math.hypot(p.x - downAt.x, p.y - downAt.y) < TAP_SLOP) return;
    moved = true;
    box.classList.add("dragging");
    tx += p.x - prev.x;
    ty += p.y - prev.y;
    render();
  });

  const release = (e) => {
    if (!pointers.delete(e.pointerId)) return;
    if (pointers.size < 2) pinch = null;
    if (pointers.size) return;
    box.classList.remove("dragging");
    if (moved || e.type === "pointercancel") return;
    const p = local(e);
    const now = performance.now();
    if (lastTap && now - lastTap.t < DOUBLE_TAP_MS && Math.hypot(p.x - lastTap.x, p.y - lastTap.y) < 30) {
      lastTap = null;
      toggleZoom(p.x, p.y);
    } else if (downAt.onImage) {
      lastTap = { ...p, t: now };
    } else {
      close();
    }
  };
  box.addEventListener("pointerup", release);
  box.addEventListener("pointercancel", release);

  box.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const p = local(e);
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      zoomTo(scale * Math.exp(-e.deltaY * unit * 0.0015), p.x, p.y);
    },
    { passive: false },
  );

  const onKey = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "+" || e.key === "=") zoomTo(scale * STEP, undefined, undefined, true);
    else if (e.key === "-" || e.key === "_") zoomTo(scale / STEP, undefined, undefined, true);
    else if (e.key === "0") zoomTo(minScale(), undefined, undefined, true);
    else if (e.key === "1") zoomTo(1, undefined, undefined, true);
    else return;
    e.preventDefault();
    e.stopPropagation();
  };
  const resize = new ResizeObserver(refit);
  const close = () => {
    resize.disconnect();
    box.remove();
    document.removeEventListener("keydown", onKey, true);
  };

  img.addEventListener("load", refit);
  img.src = src;
  box.append(img, bar);
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(box);
  resize.observe(box);
  if (img.complete) refit();
}
