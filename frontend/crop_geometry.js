// Crop geometry in displayed CSS pixels: { cx, cy, cw, ch }, bounded by the image.
// `aspect` locks width / height; null allows FREE_ASPECT_MIN..FREE_ASPECT_MAX.
//
// Smallest resize side, unless the image leaves less room.
export const CROP_MIN = 40;

// Limit free crops to 1:2..2:1 so square avatar surfaces still show the subject.
export const FREE_ASPECT_MIN = 1 / 2;
export const FREE_ASPECT_MAX = 2;

export const CROP_RATIOS = {
  portrait: { label: "Portrait 2:3", aspect: 2 / 3 },
  square: { label: "Square", aspect: 1 },
  free: { label: "Free", aspect: null },
};

const clampAspect = (a) => Math.max(FREE_ASPECT_MIN, Math.min(FREE_ASPECT_MAX, a));

// Corner order everywhere: top-left, top-right, bottom-left, bottom-right.
const CORNER_SIGNS = [
  [-1, -1],
  [1, -1],
  [-1, 1],
  [1, 1],
];

/** The starting box: 85% of the image along its limiting side, centered. Free starts at the image's own shape. */
export function cropInitial(W, H, aspect) {
  const a = aspect ?? clampAspect(W / H);
  const cw = Math.min(W, H * a) * 0.85;
  const ch = cw / a;
  return { cx: (W - cw) / 2, cy: (H - ch) / 2, cw, ch };
}

/**
 * Switch ratio while preserving centre and approximate area; shrink to fit the image.
 * Free mode keeps the existing box.
 */
export function cropReshape(box, aspect, W, H) {
  if (aspect == null) return box;
  let cw = Math.sqrt(box.cw * box.ch * aspect);
  cw = Math.min(cw, W, H * aspect);
  const ch = cw / aspect;
  const mx = box.cx + box.cw / 2;
  const my = box.cy + box.ch / 2;
  return cropMove({ cw, ch }, mx - cw / 2, my - ch / 2, W, H);
}

export function cropCorners({ cx, cy, cw, ch }) {
  return [
    [cx, cy],
    [cx + cw, cy],
    [cx, cy + ch],
    [cx + cw, cy + ch],
  ];
}

/** Return { corner }, { move: true }, or null for a pointer hit. Corners win over the body. */
export function cropHit(box, x, y, radius) {
  let best = null;
  let bestDist = Infinity;
  cropCorners(box).forEach(([hx, hy], corner) => {
    const dist = Math.max(Math.abs(x - hx), Math.abs(y - hy));
    if (dist <= radius && dist < bestDist) {
      best = { corner };
      bestDist = dist;
    }
  });
  if (best) return best;
  const { cx, cy, cw, ch } = box;
  if (x >= cx && x <= cx + cw && y >= cy && y <= cy + ch) return { move: true };
  return null;
}

/** Diagonal cursor for a corner: tl/br share one, tr/bl the other. */
export function cropCornerCursor(corner) {
  return corner === 0 || corner === 3 ? "nwse-resize" : "nesw-resize";
}

/** The box with its top-left placed at (x, y), clamped inside the image. */
export function cropMove(box, x, y, W, H) {
  return {
    ...box,
    cx: Math.max(0, Math.min(W - box.cw, x)),
    cy: Math.max(0, Math.min(H - box.ch, y)),
  };
}

/** Pin the opposite corner for the drag. Crossing it shrinks to the minimum without flipping. */
export function cropResizeStart(box, corner) {
  const [sx, sy] = CORNER_SIGNS[corner];
  const [ax, ay] = cropCorners(box)[3 - corner];
  return { ax, ay, sx, sy };
}

/** The box when the grabbed corner is dragged to (x, y). */
export function cropResize({ ax, ay, sx, sy }, x, y, aspect, W, H) {
  const roomW = sx < 0 ? ax : W - ax;
  const roomH = sy < 0 ? ay : H - ay;
  const clampSide = (want, room) => Math.max(Math.min(CROP_MIN, room), Math.min(room, want));
  let cw;
  let ch;
  if (aspect == null) {
    cw = clampSide(sx * (x - ax), roomW);
    ch = clampSide(sy * (y - ay), roomH);
    // Out of range, the longer side gives way; shrinking always fits the room.
    if (cw / ch > FREE_ASPECT_MAX) cw = ch * FREE_ASPECT_MAX;
    else if (cw / ch < FREE_ASPECT_MIN) ch = cw / FREE_ASPECT_MIN;
  } else {
    // The box covers the pointer: whichever axis it has travelled further along wins.
    cw = clampSide(Math.max(sx * (x - ax), sy * (y - ay) * aspect), Math.min(roomW, roomH * aspect));
    ch = cw / aspect;
  }
  return { cx: sx < 0 ? ax - cw : ax, cy: sy < 0 ? ay - ch : ay, cw, ch };
}

/**
 * Return source-resolution dimensions, capped at `maxSide` without upscaling.
 * For locked ratios, derive the short side from the rounded long side.
 */
export function cropOutputSize(box, scale, aspect, maxSide) {
  const srcW = box.cw / scale;
  const srcH = box.ch / scale;
  const k = Math.min(1, maxSide / Math.max(srcW, srcH));
  let w = Math.max(1, Math.round(srcW * k));
  let h = Math.max(1, Math.round(srcH * k));
  if (aspect != null) {
    if (aspect >= 1) h = Math.max(1, Math.round(w / aspect));
    else w = Math.max(1, Math.round(h * aspect));
  }
  return { w, h };
}
