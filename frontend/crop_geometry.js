// The avatar crop box, as pure geometry. Coordinates are in the displayed
// image's CSS pixels (W x H); a box is { cx, cy, cw, ch } and always stays
// inside the image. A numeric `aspect` (width / height) locks the box to that
// shape; `null` is free, where width and height move independently but the
// shape stays between FREE_ASPECT_MIN and FREE_ASPECT_MAX.

// Smallest box side a resize settles on, unless the image leaves less room.
export const CROP_MIN = 40;

// Every avatar surface is a centre-cropped square or circle, so a free crop is
// kept within 1:2..2:1: past that, the small avatars show a slice of torso or
// background rather than the face the crop was drawn around.
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
 * The box after switching ratio: same centre and roughly the same area, in the
 * new shape, shrunk only as far as the image requires. Free keeps the box as it
 * is, since every locked shape already sits inside the free range.
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

/**
 * What a pointer at (x, y) grabs: the nearest corner within `radius` as
 * { corner }, the box body as { move: true }, or null for the dimmed margin.
 * Corners win over the body so a handle is grabbable from inside the box.
 */
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

/**
 * Start a corner drag: the opposite corner is pinned for the whole drag and the
 * box grows away from it in the grabbed corner's direction. The direction never
 * flips mid-drag, so pulling past the anchor shrinks to the minimum instead of
 * jumping the box to the other side.
 */
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
 * Output pixel size for a box: the crop at the source image's own resolution,
 * the long side capped at `maxSide`, never upscaled. A locked ratio derives the
 * short side from the rounded long side, so rounding can neither drift the
 * shape nor push the long side past the cap.
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
