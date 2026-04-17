import { $ } from './utils.js';

// ── Crop modal state
let _cs = null; // { img, scale, onConfirm, aspect, cx, cy, cw, ch, drag }

export function showModal(html) {
  $('modal-root').innerHTML =
    `<div class="modal-overlay" onclick="if(event.target===this)closeModal()">
       <div class="modal">${html}</div>
     </div>`;
}

export function closeModal() {
  $('modal-root').innerHTML = '';
}

export function switchTab(tab, contentId) {
  tab.parentElement.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  tab.classList.add('active');
  tab.closest('.modal').querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
  $(contentId).classList.add('active');
}

export function showConfirmModal({ title, message, confirmText = 'Confirm', confirmClass = 'btn-danger', extraHtml = '' }, onConfirm) {
  window._confirmCb = onConfirm;
  showModal(`
    <h2>${title}</h2>
    <p>${message}</p>
    ${extraHtml}
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn ${confirmClass}" onclick="runConfirmCb()">${confirmText}</button>
    </div>`);
}

export function runConfirmCb() {
  const cb = window._confirmCb;
  window._confirmCb = null;
  if (cb) cb();
  closeModal();
}

// ── Image crop modal
// Opens a file picker; on selection shows a canvas crop editor in #modal-crop-root
// (a separate overlay so it stacks above the character create/edit modal).
// onConfirm receives { b64, mime } where the cropped image is 400×600 PNG (2:3 ratio,
// matching the SillyTavern character card standard).

export function showCropModal(onConfirm, aspect = 2 / 3) {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => _openCropEditor(ev.target.result, onConfirm, aspect);
    reader.readAsDataURL(file);
  };
  input.click();
}

function _openCropEditor(dataUrl, onConfirm, aspect) {
  const root = $('modal-crop-root');
  root.innerHTML = `
    <div class="modal-overlay">
      <div class="modal" style="align-items:center;gap:12px">
        <h2>Crop Avatar</h2>
        <canvas id="crop-canvas"></canvas>
        <div style="font-size:11px;color:var(--text-muted)">Drag to move &middot; Drag corners to resize</div>
        <div class="modal-actions" style="width:100%;margin-top:0">
          <button class="btn" onclick="closeCropModal()">Cancel</button>
          <button class="btn btn-accent" onclick="confirmCrop()">Use Image</button>
        </div>
      </div>
    </div>`;

  const img = new Image();
  img.onload = () => {
    const MAX = 480;
    const scale = Math.min(MAX / img.naturalWidth, MAX / img.naturalHeight, 1);
    const canvas = $('crop-canvas');
    canvas.width  = Math.round(img.naturalWidth  * scale);
    canvas.height = Math.round(img.naturalHeight * scale);

    // Initial crop box: largest 2:3 portrait box that fits, centred
    let cw, ch;
    if (canvas.width <= canvas.height * aspect) {
      cw = Math.round(canvas.width * 0.85);
      ch = Math.round(cw / aspect);
    } else {
      ch = Math.round(canvas.height * 0.85);
      cw = Math.round(ch * aspect);
    }
    _cs = {
      img, scale, onConfirm, aspect,
      cx: Math.round((canvas.width  - cw) / 2),
      cy: Math.round((canvas.height - ch) / 2),
      cw, ch,
      drag: null,
    };
    _drawCrop(canvas);
    _attachCropEvents(canvas);
  };
  img.src = dataUrl;

  window.confirmCrop = () => _confirmCrop($('crop-canvas'));
}

function _drawCrop(canvas) {
  if (!_cs) return;
  const { img, cx, cy, cw, ch } = _cs;
  const W = canvas.width, H = canvas.height;
  const ctx = canvas.getContext('2d');

  ctx.drawImage(img, 0, 0, W, H);

  // Dark vignette outside the crop box (four rectangles around it)
  ctx.fillStyle = 'rgba(0,0,0,0.6)';
  ctx.fillRect(0, 0, W, cy);
  ctx.fillRect(0, cy + ch, W, H - cy - ch);
  ctx.fillRect(0, cy, cx, ch);
  ctx.fillRect(cx + cw, cy, W - cx - cw, ch);

  // Rule-of-thirds lines
  ctx.strokeStyle = 'rgba(255,255,255,0.25)';
  ctx.lineWidth = 0.5;
  for (let i = 1; i < 3; i++) {
    const gx = cx + (cw * i) / 3;
    const gy = cy + (ch * i) / 3;
    ctx.beginPath(); ctx.moveTo(gx, cy); ctx.lineTo(gx, cy + ch); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cx, gy); ctx.lineTo(cx + cw, gy); ctx.stroke();
  }

  // Crop border
  ctx.strokeStyle = 'rgba(255,255,255,0.9)';
  ctx.lineWidth = 1.5;
  ctx.strokeRect(cx + 0.75, cy + 0.75, cw - 1.5, ch - 1.5);

  // Corner handles
  const hs = 8;
  ctx.fillStyle = 'white';
  [[cx, cy], [cx + cw, cy], [cx, cy + ch], [cx + cw, cy + ch]].forEach(([hx, hy]) => {
    ctx.fillRect(hx - hs / 2, hy - hs / 2, hs, hs);
  });
}

function _attachCropEvents(canvas) {
  const toLocal = e => {
    const r = canvas.getBoundingClientRect();
    const src = e.touches ? e.touches[0] : e;
    return { x: src.clientX - r.left, y: src.clientY - r.top };
  };

  const onStart = e => {
    e.preventDefault();
    if (!_cs) return;
    const { x, y } = toLocal(e);
    const { cx, cy, cw, ch } = _cs;
    const hs = 14; // hit-test radius for corner handles

    // Corner order: TL, TR, BL, BR — anchor = opposite corner
    const corners = [
      [cx,      cy,      cx + cw, cy + ch],
      [cx + cw, cy,      cx,      cy + ch],
      [cx,      cy + ch, cx + cw, cy     ],
      [cx + cw, cy + ch, cx,      cy     ],
    ];
    for (const [hx, hy, ax, ay] of corners) {
      if (Math.abs(x - hx) < hs && Math.abs(y - hy) < hs) {
        _cs.drag = { mode: 'corner', ax, ay };
        return;
      }
    }
    if (x >= cx && x <= cx + cw && y >= cy && y <= cy + ch) {
      _cs.drag = { mode: 'move', ox: x - cx, oy: y - cy };
    }
  };

  const onMove = e => {
    e.preventDefault();
    if (!_cs?.drag) return;
    const { x, y } = toLocal(e);
    const { drag } = _cs;
    const W = canvas.width, H = canvas.height;
    const A = _cs.aspect; // width / height

    if (drag.mode === 'move') {
      _cs.cx = Math.max(0, Math.min(W - _cs.cw, x - drag.ox));
      _cs.cy = Math.max(0, Math.min(H - _cs.ch, y - drag.oy));
    } else {
      // Keep the anchor corner fixed; maintain aspect ratio from mouse distance
      const { ax, ay } = drag;
      const dx = Math.abs(x - ax);
      const dy = Math.abs(y - ay);
      // Drive by whichever axis is more constrained
      let cw = Math.max(40, Math.min(dx, dy * A));
      let ch = Math.round(cw / A);

      let nx = x < ax ? ax - cw : ax;
      let ny = y < ay ? ay - ch : ay;

      // Clamp to canvas bounds then re-enforce ratio
      nx = Math.max(0, nx);
      ny = Math.max(0, ny);
      cw = Math.min(cw, W - nx);
      ch = Math.min(ch, H - ny);
      if (cw / ch > A) { cw = Math.round(ch * A); }
      else              { ch = Math.round(cw / A); }

      _cs.cw = Math.max(40, cw);
      _cs.ch = Math.max(Math.round(40 / A), ch);
      _cs.cx = nx;
      _cs.cy = ny;
    }
    _drawCrop(canvas);
  };

  const onEnd = () => { if (_cs) _cs.drag = null; };

  canvas.addEventListener('mousedown',  onStart);
  canvas.addEventListener('mousemove',  onMove);
  canvas.addEventListener('mouseup',    onEnd);
  canvas.addEventListener('touchstart', onStart, { passive: false });
  canvas.addEventListener('touchmove',  onMove,  { passive: false });
  canvas.addEventListener('touchend',   onEnd);
}

function _confirmCrop(canvas) {
  if (!_cs) return;
  const { img, cx, cy, cw, ch, scale, onConfirm, aspect } = _cs;
  const OUT_W = 400;
  const OUT_H = Math.round(OUT_W / aspect); // 600 for standard 2:3 portrait
  const out = document.createElement('canvas');
  out.width = OUT_W; out.height = OUT_H;
  // Map display-space crop back to image-space source region
  out.getContext('2d').drawImage(img, cx / scale, cy / scale, cw / scale, ch / scale, 0, 0, OUT_W, OUT_H);
  const b64 = out.toDataURL('image/png').split(',')[1];
  closeCropModal();
  onConfirm({ b64, mime: 'image/png' });
}

export function closeCropModal() {
  const root = $('modal-crop-root');
  if (root) root.innerHTML = '';
  _cs = null;
}