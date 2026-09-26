import { $ } from "./utils.js";

let _cs = null; // { img, scale, onConfirm, aspect, cx, cy, cw, ch, drag }

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

export function showCropModal(onConfirm, aspect = 2 / 3) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => _openCropEditor(ev.target.result, onConfirm, aspect);
    reader.readAsDataURL(file);
  };
  input.click();
}

function _openCropEditor(dataUrl, onConfirm, aspect) {
  const root = mountLayer(
    "modal-crop-root",
    `<h2>Crop avatar</h2>
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

  const img = new Image();
  img.onload = () => {
    const MAX = 480;
    const scale = Math.min(MAX / img.naturalWidth, MAX / img.naturalHeight, 1);
    const canvas = $("crop-canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);

    let cw, ch;
    if (canvas.width <= canvas.height * aspect) {
      cw = Math.round(canvas.width * 0.85);
      ch = Math.round(cw / aspect);
    } else {
      ch = Math.round(canvas.height * 0.85);
      cw = Math.round(ch * aspect);
    }
    _cs = {
      img,
      scale,
      onConfirm,
      aspect,
      cx: Math.round((canvas.width - cw) / 2),
      cy: Math.round((canvas.height - ch) / 2),
      cw,
      ch,
      drag: null,
    };
    _drawCrop(canvas);
    _attachCropEvents(canvas);
  };
  img.src = dataUrl;
}

function _drawCrop(canvas) {
  if (!_cs) return;
  const { img, cx, cy, cw, ch } = _cs;
  const W = canvas.width,
    H = canvas.height;
  const ctx = canvas.getContext("2d");

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
  [
    [cx, cy],
    [cx + cw, cy],
    [cx, cy + ch],
    [cx + cw, cy + ch],
  ].forEach(([hx, hy]) => {
    ctx.fillRect(hx - hs / 2, hy - hs / 2, hs, hs);
  });
}

function _attachCropEvents(canvas) {
  const toLocal = (e) => {
    const r = canvas.getBoundingClientRect();
    const src = e.touches ? e.touches[0] : e;
    return { x: src.clientX - r.left, y: src.clientY - r.top };
  };

  const onStart = (e) => {
    e.preventDefault();
    if (!_cs) return;
    const { x, y } = toLocal(e);
    const { cx, cy, cw, ch } = _cs;
    const hs = 14; // hit-test radius for corner handles

    const corners = [
      [cx, cy, cx + cw, cy + ch],
      [cx + cw, cy, cx, cy + ch],
      [cx, cy + ch, cx + cw, cy],
      [cx + cw, cy + ch, cx, cy],
    ];
    for (const [hx, hy, ax, ay] of corners) {
      if (Math.abs(x - hx) < hs && Math.abs(y - hy) < hs) {
        _cs.drag = { mode: "corner", ax, ay };
        return;
      }
    }
    if (x >= cx && x <= cx + cw && y >= cy && y <= cy + ch) {
      _cs.drag = { mode: "move", ox: x - cx, oy: y - cy };
    }
  };

  const onMove = (e) => {
    e.preventDefault();
    if (!_cs?.drag) return;
    const { x, y } = toLocal(e);
    const { drag } = _cs;
    const W = canvas.width,
      H = canvas.height;
    const A = _cs.aspect; // width / height

    if (drag.mode === "move") {
      _cs.cx = Math.max(0, Math.min(W - _cs.cw, x - drag.ox));
      _cs.cy = Math.max(0, Math.min(H - _cs.ch, y - drag.oy));
    } else {
      const { ax, ay } = drag;
      const dx = Math.abs(x - ax);
      const dy = Math.abs(y - ay);
      let cw = Math.max(40, Math.min(dx, dy * A));
      let ch = Math.round(cw / A);

      let nx = x < ax ? ax - cw : ax;
      let ny = y < ay ? ay - ch : ay;

      nx = Math.max(0, nx);
      ny = Math.max(0, ny);
      cw = Math.min(cw, W - nx);
      ch = Math.min(ch, H - ny);
      if (cw / ch > A) {
        cw = Math.round(ch * A);
      } else {
        ch = Math.round(cw / A);
      }

      _cs.cw = Math.max(40, cw);
      _cs.ch = Math.max(Math.round(40 / A), ch);
      _cs.cx = nx;
      _cs.cy = ny;
    }
    _drawCrop(canvas);
  };

  const onEnd = () => {
    if (_cs) _cs.drag = null;
  };

  canvas.addEventListener("mousedown", onStart);
  canvas.addEventListener("mousemove", onMove);
  canvas.addEventListener("mouseup", onEnd);
  canvas.addEventListener("touchstart", onStart, { passive: false });
  canvas.addEventListener("touchmove", onMove, { passive: false });
  canvas.addEventListener("touchend", onEnd);
}

function _confirmCrop() {
  if (!_cs) return;
  const { img, cx, cy, cw, ch, scale, onConfirm, aspect } = _cs;
  const OUT_W = 400;
  const OUT_H = Math.round(OUT_W / aspect); // 600 for standard 2:3 portrait
  const out = document.createElement("canvas");
  out.width = OUT_W;
  out.height = OUT_H;
  out.getContext("2d").drawImage(img, cx / scale, cy / scale, cw / scale, ch / scale, 0, 0, OUT_W, OUT_H);
  const b64 = out.toDataURL("image/png").split(",")[1];
  closeCropModal();
  onConfirm({ b64, mime: "image/png" });
}

export function closeCropModal() {
  const root = $("modal-crop-root");
  if (root) root.innerHTML = "";
  _cs = null;
}
