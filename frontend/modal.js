import {
  CROP_RATIOS,
  cropCornerCursor,
  cropCorners,
  cropHit,
  cropInitial,
  cropMove,
  cropOutputSize,
  cropReshape,
  cropResize,
  cropResizeStart,
} from "./crop_geometry.js";
import { $ } from "./utils.js";

let _cs = null; // { img, scale, onConfirm, aspect, W, H, box: { cx, cy, cw, ch }, drag }

// Largest side of a saved avatar. The crop keeps the source resolution up to this.
const CROP_OUT_MAX = 1024;

// Backdrop clicks, Escape and mobile Back all dismiss through closeTopModal, so a
// modal whose Cancel does more than close (return to its list, abort a stream)
// names that once with setModalDismiss, and every way out agrees with its Cancel.
let _modalDismiss = null;
let _modalCloseCallback = null;
let _modalCloseGuard = null;

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeTopModal();
});

export function isModalOpen() {
  return !!($("modal-crop-root")?.innerHTML || $("modal-sub-root")?.innerHTML || $("modal-root")?.innerHTML);
}

/** Dismiss the topmost modal layer. Returns whether one was open. */
export function closeTopModal() {
  if ($("modal-crop-root")?.innerHTML) closeCropModal();
  else if ($("modal-sub-root")?.innerHTML) closeSubModal();
  else if ($("modal-root")?.innerHTML) dismissModal();
  else return false;
  return true;
}

// Width follows the content: "narrow" for a question and its answer, the default
// for a form, "wide" for a workspace (a card grid, a long editor, a settings page).
const SIZE_CLASS = { narrow: " modal-narrow", wide: " modal-wide" };

function mountLayer(rootId, html, { onBackdrop = null, modalClass = "modal" } = {}) {
  const root = $(rootId);
  root.innerHTML = `<div class="modal-overlay"><div class="${modalClass}">${html}</div></div>`;
  const overlay = root.firstElementChild;
  if (onBackdrop) overlay.addEventListener("click", (e) => e.target === overlay && onBackdrop());
  return root;
}

export function showModal(html, { size = "" } = {}) {
  _modalCloseGuard = null;
  _modalDismiss = null;
  mountLayer("modal-root", html, { onBackdrop: dismissModal, modalClass: `modal${SIZE_CLASS[size] || ""}` });
}

export function closeModal() {
  if (_modalCloseGuard && _modalCloseGuard() === false) return;
  _modalCloseGuard = null;
  _modalDismiss = null;
  $("modal-root").innerHTML = "";
  const cb = _modalCloseCallback;
  _modalCloseCallback = null;
  if (cb) cb();
}

/** What the base modal's backdrop, Escape and mobile Back do: its dismissal, or close. */
export function dismissModal() {
  if (_modalDismiss) _modalDismiss();
  else closeModal();
}

export function setModalDismiss(fn) {
  _modalDismiss = fn;
}

export function setModalCloseCallback(cb) {
  _modalCloseCallback = cb;
}

export function setModalCloseGuard(fn) {
  _modalCloseGuard = fn;
}

export function switchTab(tab, contentId) {
  tab.parentElement.querySelectorAll(".tab").forEach((x) => {
    x.classList.remove("active");
  });
  tab.classList.add("active");
  tab
    .closest(".modal")
    .querySelectorAll(".tab-content")
    .forEach((x) => {
      x.classList.remove("active");
    });
  $(contentId).classList.add("active");
}

/**
 * A title, a message, Cancel and one action — or several, as
 * `actions: [{ label, className, run }]`, when the choice has more than one way
 * to go through (delete a variant or the whole attachment).
 */
function mountConfirm(rootId, show, close, opts, onConfirm) {
  const { title, message, confirmText = "Confirm", confirmClass = "btn-danger", extraHtml = "" } = opts;
  const actions = opts.actions || [{ label: confirmText, className: confirmClass, run: onConfirm }];
  show(
    `
    <h2>${title}</h2>
    <div class="modal-confirm-body">${message ? `<p>${message}</p>` : ""}${extraHtml}</div>
    <div class="modal-actions">
      <button class="btn" data-confirm-action="cancel">Cancel</button>
      ${actions.map((a, i) => `<button class="btn ${a.className ?? "btn-danger"}" data-confirm-action="${i}">${a.label}</button>`).join("")}
    </div>`,
    { size: "narrow" },
  );
  for (const button of $(rootId).querySelectorAll("[data-confirm-action]")) {
    const action = actions[button.dataset.confirmAction];
    button.addEventListener("click", () => {
      action?.run?.();
      close();
    });
  }
}

export function showConfirmModal(opts, onConfirm) {
  mountConfirm("modal-root", showModal, closeModal, opts, onConfirm);
}

export function confirmDelete(label, message, onOk) {
  showConfirmModal({ title: `Delete ${label}`, message, confirmText: "Delete" }, onOk);
}

export function showSubModal(html, { size = "" } = {}) {
  if ($("modal-sub-root").innerHTML) console.warn("modal-sub-root already in use; the sub-modal layer does not nest");
  mountLayer("modal-sub-root", html, { onBackdrop: closeSubModal, modalClass: `modal${SIZE_CLASS[size] || ""}` });
}

export function closeSubModal() {
  $("modal-sub-root").innerHTML = "";
}

export function showSubConfirmModal(opts, onConfirm) {
  mountConfirm("modal-sub-root", showSubModal, closeSubModal, opts, onConfirm);
}

// The ratio last picked for each kind of avatar, so someone who always wants
// square chooses it once. Storage can be unavailable (private window, blocked
// site data); the caller's default applies then.
const cropRatioKey = (kind) => `orb-crop-ratio-${kind}`;

function _rememberedRatio(kind, fallback) {
  try {
    const saved = localStorage.getItem(cropRatioKey(kind));
    return saved && Object.hasOwn(CROP_RATIOS, saved) ? saved : fallback;
  } catch {
    return fallback;
  }
}

function _rememberRatio(kind, ratio) {
  try {
    localStorage.setItem(cropRatioKey(kind), ratio);
  } catch {}
}

/**
 * Pick an image and crop it. `kind` names the avatar ("character", "persona")
 * so each remembers its own last ratio; `ratio` is its default, a CROP_RATIOS key.
 */
export function showCropModal(onConfirm, { kind, ratio = "portrait" }) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => _openCropEditor(ev.target.result, onConfirm, kind, _rememberedRatio(kind, ratio));
    reader.readAsDataURL(file);
  };
  input.click();
}

function _openCropEditor(dataUrl, onConfirm, kind, ratio) {
  const ratioButtons = Object.entries(CROP_RATIOS)
    .map(
      ([key, { label }]) =>
        `<button type="button" class="btn btn-sm${key === ratio ? " btn-active" : ""}" data-crop-ratio="${key}" aria-pressed="${key === ratio}">${label}</button>`,
    )
    .join("");
  const root = mountLayer(
    "modal-crop-root",
    `<h2>Crop avatar</h2>
        <div class="btn-row" role="group" aria-label="Crop shape">${ratioButtons}</div>
        <canvas id="crop-canvas"></canvas>
        <div style="font-size:11px;color:var(--text-muted)">Drag to move &middot; Drag corners to resize</div>
        <div class="modal-actions">
          <button class="btn" data-crop-action="cancel">Cancel</button>
          <button class="btn btn-accent" data-crop-action="confirm">Use Image</button>
        </div>`,
    { modalClass: "modal modal-crop" },
  );
  root.querySelector('[data-crop-action="cancel"]').addEventListener("click", closeCropModal);
  root.querySelector('[data-crop-action="confirm"]').addEventListener("click", () => _confirmCrop());
  for (const button of root.querySelectorAll("[data-crop-ratio]")) {
    button.addEventListener("click", () => {
      if (!_cs) return;
      const next = button.dataset.cropRatio;
      for (const other of root.querySelectorAll("[data-crop-ratio]")) {
        const on = other === button;
        other.classList.toggle("btn-active", on);
        other.setAttribute("aria-pressed", String(on));
      }
      _cs.aspect = CROP_RATIOS[next].aspect;
      _cs.box = cropReshape(_cs.box, _cs.aspect, _cs.W, _cs.H);
      _drawCrop($("crop-canvas"));
      _rememberRatio(kind, next);
    });
  }

  const img = new Image();
  img.onload = () => {
    // Box geometry lives in the displayed image's CSS pixels (W x H); the
    // backing store is scaled by devicePixelRatio so the preview stays sharp.
    const MAX = 480;
    const scale = Math.min(MAX / img.naturalWidth, MAX / img.naturalHeight, 1);
    const W = img.naturalWidth * scale;
    const H = img.naturalHeight * scale;
    const dpr = window.devicePixelRatio || 1;
    const canvas = $("crop-canvas");
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.width = `${W}px`;
    const { aspect } = CROP_RATIOS[ratio];
    _cs = { img, scale, onConfirm, aspect, W, H, box: cropInitial(W, H, aspect), drag: null };
    _drawCrop(canvas);
    _attachCropEvents(canvas);
  };
  img.src = dataUrl;
}

function _drawCrop(canvas) {
  if (!_cs) return;
  const { img, W, H } = _cs;
  const { cx, cy, cw, ch } = _cs.box;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(canvas.width / W, 0, 0, canvas.height / H, 0, 0);
  ctx.imageSmoothingQuality = "high";
  ctx.clearRect(0, 0, W, H);
  ctx.drawImage(img, 0, 0, W, H);

  ctx.fillStyle = "rgba(0,0,0,0.6)";
  ctx.fillRect(0, 0, W, cy);
  ctx.fillRect(0, cy + ch, W, H - cy - ch);
  ctx.fillRect(0, cy, cx, ch);
  ctx.fillRect(cx + cw, cy, W - cx - cw, ch);

  ctx.strokeStyle = "rgba(255,255,255,0.25)";
  ctx.lineWidth = 0.5;
  for (let i = 1; i < 3; i++) {
    const gx = cx + (cw * i) / 3;
    const gy = cy + (ch * i) / 3;
    ctx.beginPath();
    ctx.moveTo(gx, cy);
    ctx.lineTo(gx, cy + ch);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx, gy);
    ctx.lineTo(cx + cw, gy);
    ctx.stroke();
  }

  ctx.strokeStyle = "rgba(255,255,255,0.9)";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(cx + 0.75, cy + 0.75, cw - 1.5, ch - 1.5);

  const hs = 8;
  ctx.fillStyle = "white";
  for (const [hx, hy] of cropCorners(_cs.box)) ctx.fillRect(hx - hs / 2, hy - hs / 2, hs, hs);
}

// Capture pointer moves and release outside the canvas; clamp positions to its bounds.
function _attachCropEvents(canvas) {
  const HIT_RADIUS = 14; // screen pixels around a corner handle
  // The canvas can render narrower than W (max-width: 100% on a small screen),
  // so client pixels are rescaled into box pixels rather than used as-is.
  const toLocal = (e) => {
    const r = canvas.getBoundingClientRect();
    const k = _cs.W / r.width;
    return { x: (e.clientX - r.left) * k, y: (e.clientY - r.top) * k, k };
  };
  const cursorFor = (hit) => (hit?.corner != null ? cropCornerCursor(hit.corner) : hit?.move ? "move" : "");

  canvas.addEventListener("pointerdown", (e) => {
    if (!_cs || !e.isPrimary || e.button !== 0) return;
    e.preventDefault();
    const { x, y, k } = toLocal(e);
    let hit = cropHit(_cs.box, x, y, HIT_RADIUS * k);
    if (!hit) {
      // A press on the dimmed margin brings the box to the pointer and carries on as a move.
      _cs.box = cropMove(_cs.box, x - _cs.box.cw / 2, y - _cs.box.ch / 2, _cs.W, _cs.H);
      hit = { move: true };
      _drawCrop(canvas);
    }
    _cs.drag =
      hit.corner != null
        ? { resize: cropResizeStart(_cs.box, hit.corner) }
        : { ox: x - _cs.box.cx, oy: y - _cs.box.cy };
    canvas.style.cursor = cursorFor(hit);
    canvas.setPointerCapture(e.pointerId);
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!_cs) return;
    const { x, y, k } = toLocal(e);
    const { drag, W, H } = _cs;
    if (!drag) {
      canvas.style.cursor = cursorFor(cropHit(_cs.box, x, y, HIT_RADIUS * k));
      return;
    }
    _cs.box = drag.resize
      ? cropResize(drag.resize, x, y, _cs.aspect, W, H)
      : cropMove(_cs.box, x - drag.ox, y - drag.oy, W, H);
    _drawCrop(canvas);
  });

  // lostpointercapture follows every pointerup and pointercancel, and also
  // covers the capture being dropped for any other reason.
  canvas.addEventListener("lostpointercapture", () => {
    if (_cs) _cs.drag = null;
  });
}

function _confirmCrop() {
  if (!_cs) return;
  const { img, scale, onConfirm, aspect } = _cs;
  const { cx, cy, cw, ch } = _cs.box;
  const { w, h } = cropOutputSize(_cs.box, scale, aspect, CROP_OUT_MAX);
  const out = document.createElement("canvas");
  out.width = w;
  out.height = h;
  const ctx = out.getContext("2d");
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, cx / scale, cy / scale, cw / scale, ch / scale, 0, 0, w, h);
  const b64 = out.toDataURL("image/png").split(",")[1];
  closeCropModal();
  onConfirm({ b64, mime: "image/png" });
}

export function closeCropModal() {
  const root = $("modal-crop-root");
  if (root) root.innerHTML = "";
  _cs = null;
}
