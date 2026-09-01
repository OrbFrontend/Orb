// Pointer-driven list reordering, shared by the scene cast list and the
// interactive fragment list.
//
// Both lists used to reorder through HTML5 drag-and-drop, which no mobile
// browser synthesises from touch input: on a phone the lists could not be
// reordered at all. Pointer events cover mouse, touch and pen from one code
// path, and the arrow-key path gives the same reordering to the keyboard.
//
// The drag starts from a handle rather than the row body, so a finger landing
// anywhere else still scrolls the list, and a tap still activates the row. The
// handle must carry `touch-action: none` in CSS, or the browser claims the
// gesture for scrolling before the first pointermove arrives.

const AUTOSCROLL_EDGE_PX = 44; // proximity to the scrollport edge that starts a scroll
const AUTOSCROLL_STEP_PX = 10; // per-frame scroll while the pointer is held at the edge
const DRAG_SLOP_PX = 4; // travel before a press counts as a drag rather than a tap
const KEY_COMMIT_MS = 400; // quiet period before a run of arrow presses is committed

/**
 * Index in `rects` that a pointer at `y` should insert before, or `rects.length`
 * to place last. `rects` are the other items' bounding boxes, in document order.
 */
export function dropTargetIndex(rects, y) {
  for (let i = 0; i < rects.length; i++) {
    if (y < rects[i].top + rects[i].height / 2) return i;
  }
  return rects.length;
}

/**
 * Make `container`'s items reorderable by dragging their handle, or by pressing
 * ArrowUp/ArrowDown while the handle has focus. `onReorder(container)` fires
 * once per committed reorder. Returns a teardown function.
 */
export function initDragReorder(container, { itemSelector, handleSelector, onReorder = null } = {}) {
  let item = null;
  let handleEl = null;
  let pointerId = null;
  let startIndex = -1;
  let startY = 0;
  let pointerY = 0;
  let dragged = false;
  let scroller = null;
  let rafId = 0;
  let keyCommitTimer = 0;

  const items = () => [...container.querySelectorAll(itemSelector)];

  function scrollportFor(el) {
    for (let node = el.parentElement; node; node = node.parentElement) {
      const overflowY = getComputedStyle(node).overflowY;
      if ((overflowY === "auto" || overflowY === "scroll") && node.scrollHeight > node.clientHeight) return node;
    }
    return null;
  }

  function placeAt(y) {
    const others = items().filter((el) => el !== item);
    if (!others.length) return;
    const at = dropTargetIndex(
      others.map((el) => el.getBoundingClientRect()),
      y,
    );
    // Insert relative to a sibling, never by appending: these containers hold
    // trailing non-item children (an "add" button) that must stay last.
    if (at < others.length) others[at].before(item);
    else others[others.length - 1].after(item);
  }

  function autoScroll() {
    if (!item || !scroller) {
      rafId = 0;
      return;
    }
    const box = scroller.getBoundingClientRect();
    let delta = 0;
    if (pointerY - box.top < AUTOSCROLL_EDGE_PX) delta = -AUTOSCROLL_STEP_PX;
    else if (box.bottom - pointerY < AUTOSCROLL_EDGE_PX) delta = AUTOSCROLL_STEP_PX;
    if (delta) {
      const before = scroller.scrollTop;
      scroller.scrollTop += delta;
      if (scroller.scrollTop !== before) placeAt(pointerY);
    }
    rafId = requestAnimationFrame(autoScroll);
  }

  // A drag ends over whatever row it dropped onto, so the trailing click would
  // land on an unrelated row. Swallow it, but only after a real drag.
  function swallowNextClick() {
    const swallow = (e) => {
      e.stopPropagation();
      e.preventDefault();
    };
    container.addEventListener("click", swallow, true);
    setTimeout(() => container.removeEventListener("click", swallow, true), 0);
  }

  function finishDrag() {
    if (!item) return;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
    const moved = items().indexOf(item) !== startIndex;
    const wasDragged = dragged;
    item.classList.remove("dragging");
    try {
      handleEl.releasePointerCapture(pointerId);
    } catch {
      // The capture is already gone (pointercancel, or a detached handle).
    }
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", onPointerUp);
    document.removeEventListener("pointercancel", onPointerUp);
    item = null;
    handleEl = null;
    pointerId = null;
    scroller = null;
    dragged = false;
    if (wasDragged) swallowNextClick();
    if (moved) onReorder?.(container);
  }

  function onPointerMove(e) {
    if (!item || e.pointerId !== pointerId) return;
    pointerY = e.clientY;
    if (!dragged && Math.abs(pointerY - startY) > DRAG_SLOP_PX) dragged = true;
    placeAt(pointerY);
  }

  function onPointerUp(e) {
    if (!item || e.pointerId !== pointerId) return;
    finishDrag();
  }

  function onPointerDown(e) {
    if (item || e.button > 0) return;
    const handle = e.target.closest?.(handleSelector);
    if (!handle || !container.contains(handle)) return;
    const row = handle.closest(itemSelector);
    if (!row) return;
    item = row;
    handleEl = handle;
    pointerId = e.pointerId;
    startIndex = items().indexOf(row);
    startY = e.clientY;
    pointerY = e.clientY;
    dragged = false;
    scroller = scrollportFor(row);
    row.classList.add("dragging");
    try {
      handle.setPointerCapture(e.pointerId);
    } catch {
      // Capture is an optimisation; the document listeners below still track.
    }
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerUp);
    if (scroller) rafId = requestAnimationFrame(autoScroll);
    e.preventDefault(); // no text selection or native image drag while dragging
  }

  function onKeydown(e) {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    const handle = e.target.closest?.(handleSelector);
    if (!handle || !container.contains(handle)) return;
    const row = handle.closest(itemSelector);
    if (!row) return;
    const rows = items();
    const to = rows.indexOf(row) + (e.key === "ArrowUp" ? -1 : 1);
    if (to < 0 || to >= rows.length) return;
    e.preventDefault();
    if (e.key === "ArrowUp") rows[to].before(row);
    else rows[to].after(row);
    handle.focus();
    clearTimeout(keyCommitTimer);
    keyCommitTimer = setTimeout(commitKeyMoves, KEY_COMMIT_MS);
  }

  function commitKeyMoves() {
    if (!keyCommitTimer) return;
    clearTimeout(keyCommitTimer);
    keyCommitTimer = 0;
    onReorder?.(container);
  }

  container.addEventListener("pointerdown", onPointerDown);
  container.addEventListener("keydown", onKeydown);
  return () => {
    finishDrag();
    commitKeyMoves();
    container.removeEventListener("pointerdown", onPointerDown);
    container.removeEventListener("keydown", onKeydown);
  };
}
