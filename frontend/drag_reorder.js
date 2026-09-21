// Pointer-driven list reordering, shared by the scene cast list and the
// interactive fragment list.
//
// Both lists used to reorder through HTML5 drag-and-drop, which no mobile
// browser synthesises from touch input: on a phone the lists could not be
// reordered at all. Pointer events cover mouse, touch and pen from one code
// path, and the arrow-key path gives the same reordering to the keyboard.
//
// The drag starts from a handle rather than the row body, so a finger landing
// anywhere else still scrolls the list, and a tap still activates the row.
//
// A fingertip is far wider than the handle it lands on, so on touch and pen a
// press only picks a row up once it has been held still for HOLD_MS. A finger
// that crosses the handle mid-swipe scrolls the list the way it would anywhere
// else, instead of silently reordering it; a mouse still picks up on press,
// where the button is deliberate and the pointer never scrolls. That is why
// the handle carries `touch-action: pan-y` rather than `none`: the browser has
// to stay free to scroll a press that turns out to be a swipe, so an armed
// drag takes the gesture back by cancelling the touchmove itself.

const AUTOSCROLL_EDGE_PX = 44; // proximity to the scrollport edge that starts a scroll
const AUTOSCROLL_STEP_PX = 10; // per-frame scroll while the pointer is held at the edge
const DRAG_SLOP_PX = 4; // travel before a press counts as a drag rather than a tap
const KEY_COMMIT_MS = 400; // quiet period before a run of arrow presses is committed
const HOLD_MS = 300; // touch/pen press held still before the row is picked up
const HOLD_SLOP_PX = 8; // travel during that hold that means scrolling, not dragging

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
 * ArrowUp/ArrowDown while the handle has focus. Touch and pen have to hold the
 * handle still for a moment before the row is picked up. `onReorder(container)`
 * fires once per committed reorder. Returns a teardown function.
 */
export function initDragReorder(container, { itemSelector, handleSelector, onReorder = null } = {}) {
  let item = null;
  let handleEl = null;
  let pointerId = null;
  let startIndex = -1;
  let startX = 0;
  let startY = 0;
  let pointerY = 0;
  let armed = false; // the press has become a drag; before this it may still scroll
  let heldPickup = false; // this pointer type has to hold the handle to pick up
  let holdTimer = 0;
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
    if (!armed || !scroller) {
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

  // The handle's `touch-action: pan-y` leaves the browser free to scroll a
  // press that turns out to be a swipe. Once the press is armed the drag owns
  // the gesture instead: it has been still for HOLD_MS, so no pan has started
  // yet, and cancelling this move keeps one from starting.
  function blockScroll(e) {
    if (armed && e.cancelable) e.preventDefault();
  }

  function armDrag() {
    if (!item || armed) return;
    clearTimeout(holdTimer);
    holdTimer = 0;
    armed = true;
    item.classList.add("dragging");
    try {
      handleEl.setPointerCapture(pointerId);
    } catch {
      // Capture is an optimisation; the document listeners below still track.
    }
    if (heldPickup) {
      // Holding long enough to pick a row up is already a commitment: the
      // press must not also read as a tap on whatever the row drops onto.
      dragged = true;
      document.addEventListener("touchmove", blockScroll, { passive: false });
      navigator.vibrate?.(8); // a fingertip covers the handle it just picked up
    }
    if (scroller) rafId = requestAnimationFrame(autoScroll);
  }

  function endPress() {
    if (!item) return;
    clearTimeout(holdTimer);
    holdTimer = 0;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
    const moved = armed && items().indexOf(item) !== startIndex;
    const wasDragged = dragged;
    item.classList.remove("dragging");
    try {
      handleEl.releasePointerCapture(pointerId);
    } catch {
      // Never captured (the hold never elapsed), or the capture is already
      // gone (pointercancel, or a detached handle).
    }
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", onPointerUp);
    document.removeEventListener("pointercancel", onPointerUp);
    document.removeEventListener("touchmove", blockScroll);
    item = null;
    handleEl = null;
    pointerId = null;
    scroller = null;
    armed = false;
    heldPickup = false;
    dragged = false;
    if (wasDragged) swallowNextClick();
    if (moved) onReorder?.(container);
  }

  function onPointerMove(e) {
    if (!item || e.pointerId !== pointerId) return;
    pointerY = e.clientY;
    if (!armed) {
      // Travel before the hold elapses is a swipe, not a pickup: drop the
      // press so the browser keeps the gesture and scrolls with it.
      if (Math.hypot(e.clientX - startX, e.clientY - startY) > HOLD_SLOP_PX) endPress();
      return;
    }
    if (!dragged && Math.abs(pointerY - startY) > DRAG_SLOP_PX) dragged = true;
    placeAt(pointerY);
  }

  function onPointerUp(e) {
    if (!item || e.pointerId !== pointerId) return;
    endPress();
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
    startX = e.clientX;
    startY = e.clientY;
    pointerY = e.clientY;
    armed = false;
    dragged = false;
    heldPickup = e.pointerType === "touch" || e.pointerType === "pen";
    scroller = scrollportFor(row);
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerUp);
    if (heldPickup) {
      // Claim nothing yet, and no preventDefault: until the hold elapses this
      // press is still allowed to turn into a scroll.
      holdTimer = setTimeout(armDrag, HOLD_MS);
      return;
    }
    armDrag();
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
    endPress();
    commitKeyMoves();
    container.removeEventListener("pointerdown", onPointerDown);
    container.removeEventListener("keydown", onKeydown);
  };
}
