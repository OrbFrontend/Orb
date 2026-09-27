import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CROP_MIN,
  FREE_ASPECT_MAX,
  FREE_ASPECT_MIN,
  cropHit,
  cropInitial,
  cropMove,
  cropOutputSize,
  cropReshape,
  cropResize,
  cropResizeStart,
} from "../../frontend/crop_geometry.js";

const ASPECT = 2 / 3;
const W = 300;
const H = 480;

const close = (actual, expected) => {
  for (const key of Object.keys(expected)) assert.ok(Math.abs(actual[key] - expected[key]) < 1e-9, `${key}: ${actual[key]}`);
};

test("the starting box is centered and keeps the aspect", () => {
  const box = cropInitial(W, H, ASPECT);
  close(box, { cw: 255, ch: 382.5, cx: 22.5, cy: 48.75 });
});

test("a corner beats the body, the margin grabs nothing", () => {
  const box = { cx: 50, cy: 50, cw: 100, ch: 150 };
  assert.deepEqual(cropHit(box, 55, 55, 14), { corner: 0 });
  assert.deepEqual(cropHit(box, 145, 195, 14), { corner: 3 });
  assert.deepEqual(cropHit(box, 100, 120, 14), { move: true });
  assert.equal(cropHit(box, 10, 10, 14), null);
});

test("a move past the edge clamps instead of leaving the image", () => {
  const box = { cx: 50, cy: 50, cw: 100, ch: 150 };
  close(cropMove(box, -500, 900, W, H), { cx: 0, cy: H - 150 });
});

test("a resize pins the opposite corner and keeps the aspect", () => {
  const box = { cx: 50, cy: 50, cw: 100, ch: 150 };
  const drag = cropResizeStart(box, 3); // bottom-right, anchored at top-left
  const next = cropResize(drag, 170, 210, ASPECT, W, H);
  close(next, { cx: 50, cy: 50, cw: 120, ch: 180 });
});

test("dragging a corner far outside stops at the image edge with the anchor fixed", () => {
  const box = { cx: 100, cy: 100, cw: 100, ch: 150 };
  const drag = cropResizeStart(box, 0); // top-left, anchored at (200, 250)
  const next = cropResize(drag, -1000, -1000, ASPECT, W, H);
  close(next, { cx: 200 - next.cw, cy: 250 - next.ch });
  assert.ok(next.cx >= 0 && next.cy >= 0);
  assert.ok(Math.abs(next.cw / next.ch - ASPECT) < 1e-9);
});

test("pulling past the anchor shrinks to the minimum rather than flipping", () => {
  const box = { cx: 50, cy: 50, cw: 100, ch: 150 };
  const drag = cropResizeStart(box, 3);
  const next = cropResize(drag, 0, 0, ASPECT, W, H);
  close(next, { cx: 50, cy: 50, cw: CROP_MIN, ch: CROP_MIN / ASPECT });
});

test("free resizes each side on its own", () => {
  const box = { cx: 50, cy: 50, cw: 100, ch: 150 };
  const next = cropResize(cropResizeStart(box, 3), 250, 200, null, W, H);
  close(next, { cx: 50, cy: 50, cw: 200, ch: 150 });
});

test("free keeps the shape within 1:2..2:1, the longer side giving way", () => {
  const box = { cx: 0, cy: 0, cw: 100, ch: 100 };
  const wide = cropResize(cropResizeStart(box, 3), 300, 60, null, W, H);
  close(wide, { cx: 0, cy: 0, cw: 60 * FREE_ASPECT_MAX, ch: 60 });
  const tall = cropResize(cropResizeStart(box, 3), 60, 400, null, W, H);
  close(tall, { cx: 0, cy: 0, cw: 60, ch: 60 / FREE_ASPECT_MIN });
});

test("free starts at the image's shape, clamped to the range", () => {
  const box = cropInitial(1000, 200, null);
  assert.ok(Math.abs(box.cw / box.ch - FREE_ASPECT_MAX) < 1e-9);
  assert.ok(box.cw <= 1000 && box.ch <= 200);
});

test("switching ratio keeps the centre and area, shrinking only to fit", () => {
  const box = { cx: 50, cy: 90, cw: 200, ch: 300 };
  const square = cropReshape(box, 1, W, H);
  close(square, { cw: Math.sqrt(60000), ch: Math.sqrt(60000) });
  close({ mx: square.cx + square.cw / 2, my: square.cy + square.ch / 2 }, { mx: 150, my: 240 });
  const full = { cx: 0, cy: 0, cw: W, ch: 450 };
  const fitted = cropReshape(full, 1, W, H);
  assert.equal(fitted.cw, W, "a square can be no wider than the image");
  assert.ok(fitted.cy >= 0 && fitted.cy + fitted.ch <= H);
  assert.equal(cropReshape(box, null, W, H), box, "free keeps the box as it is");
});

test("output keeps source resolution, capped on the long side, never upscaled", () => {
  // scale 0.25: the displayed box is a quarter of the source.
  assert.deepEqual(cropOutputSize({ cw: 100, ch: 150 }, 0.25, 2 / 3, 1024), { w: 400, h: 600 });
  assert.deepEqual(cropOutputSize({ cw: 400, ch: 600 }, 0.25, 2 / 3, 1024), { w: 683, h: 1024 });
  assert.deepEqual(cropOutputSize({ cw: 30, ch: 30 }, 1, 1, 1024), { w: 30, h: 30 });
  assert.deepEqual(cropOutputSize({ cw: 300, ch: 170 }, 0.5, null, 512), { w: 512, h: 290 });
});
