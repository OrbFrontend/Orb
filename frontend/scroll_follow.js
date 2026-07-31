// Shared "stick to bottom unless the user scrolled up" scroll mechanics, used by
// the chat area, the reasoning box(es), and Document mode's editor. Two shapes
// of the same underlying idea:
//   - createScrollFollow: a stateful controller for elements that get frequent,
//     independent mutations (streamed tokens) — wheel/touch disarm the follow
//     state, scrolling back to the bottom re-arms it.
//   - preserveScroll / preserveScrollDistance: a snapshot-mutate-restore wrapper
//     for elements whose content is rebuilt in one shot (innerHTML/outerHTML
//     rewrites) — no listeners needed, since the current scrollTop already
//     reflects whatever the user did.

// Stateful follow controller. `el` is the scrollable element itself (not a
// thunk — call sites construct this once the element exists).
export function createScrollFollow(
  el,
  { threshold = 20, onScroll = null, debounceMs = 100, twoWayScroll = false } = {},
) {
  let following = true;
  let programmatic = false;
  let programmaticTimer = null;
  let rearmTimer = null;

  const atBottom = () => el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;

  const markProgrammatic = (ms = 400) => {
    programmatic = true;
    clearTimeout(programmaticTimer);
    programmaticTimer = setTimeout(() => {
      programmatic = false;
    }, ms);
  };

  el.addEventListener(
    "wheel",
    (e) => {
      if (e.deltaY < 0) following = false;
    },
    { passive: true },
  );

  let touchStartY = 0;
  el.addEventListener(
    "touchstart",
    (e) => {
      touchStartY = e.touches[0].clientY;
    },
    { passive: true },
  );
  el.addEventListener(
    "touchmove",
    (e) => {
      if (e.touches[0].clientY > touchStartY) following = false;
    },
    { passive: true },
  );

  el.addEventListener("scroll", () => {
    if (programmatic) return;
    onScroll?.();
    const rearm = () => {
      if (atBottom()) following = true;
      else if (twoWayScroll) following = false;
    };
    if (debounceMs > 0) {
      clearTimeout(rearmTimer);
      rearmTimer = setTimeout(rearm, debounceMs);
    } else {
      rearm();
    }
  });

  return {
    isFollowing: () => following,
    setFollowing: (v) => {
      following = v;
    },
    markProgrammatic,
    toBottom({ smooth = false } = {}) {
      if (!following) return;
      programmatic = true;
      clearTimeout(programmaticTimer);
      requestAnimationFrame(() => {
        if (smooth) {
          el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
          programmaticTimer = setTimeout(() => {
            programmatic = false;
          }, 400);
        } else {
          el.scrollTo({ top: el.scrollHeight, behavior: "instant" });
          programmatic = false;
        }
      });
    },
  };
}

// Verbatim snapshot/mutate/restore: correct when content is only ever appended
// at the tail (the box never grows above the visible area). `getEl` is a thunk,
// re-invoked before AND after `mutate()`, since some callers replace the
// element's DOM identity via an ancestor's innerHTML/outerHTML rewrite.
export function preserveScroll(getEl, threshold, mutate) {
  const before = getEl();
  const snapshot = before
    ? {
        atBottom: before.scrollHeight - before.scrollTop - before.clientHeight <= threshold,
        scrollTop: before.scrollTop,
      }
    : null;
  mutate();
  const after = getEl();
  if (!after) return;
  if (!snapshot || snapshot.atBottom) after.scrollTop = after.scrollHeight;
  else after.scrollTop = snapshot.scrollTop;
}

// Distance-from-bottom-preserving snapshot/mutate/restore: needed when content
// can be inserted ABOVE the viewport (e.g. windowed backfill), where a verbatim
// scrollTop restore would leave the wrong messages in view.
export function preserveScrollDistance(getEl, threshold, mutate, { forceBottom = false } = {}) {
  const before = getEl();
  if (!before) {
    mutate();
    return;
  }
  const distFromBottom = before.scrollHeight - before.scrollTop - before.clientHeight;
  mutate();
  const after = getEl();
  if (!after) return;
  const targetTop =
    forceBottom || distFromBottom <= threshold
      ? after.scrollHeight
      : Math.max(0, after.scrollHeight - after.clientHeight - distFromBottom);
  after.scrollTo({ top: targetTop, behavior: "instant" });
}
