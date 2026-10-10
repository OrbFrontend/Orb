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
    toBottom({ smooth = false, now = false } = {}) {
      if (!following) return;
      programmatic = true;
      clearTimeout(programmaticTimer);
      const scroll = () => {
        if (smooth) {
          el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
          programmaticTimer = setTimeout(() => {
            programmatic = false;
          }, 400);
        } else {
          el.scrollTo({ top: el.scrollHeight, behavior: "instant" });
          programmatic = false;
        }
      };
      if (now) scroll();
      else requestAnimationFrame(scroll);
    },
  };
}

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

// Off the bottom, `anchor(el)` may name a row to hold still: it returns a finder that locates that row again after
// the mutation, so growth below the viewport no longer shifts what the reader is looking at.
export function preserveScrollDistance(getEl, threshold, mutate, { forceBottom = false, anchor = null } = {}) {
  const before = getEl();
  if (!before) {
    mutate();
    return;
  }
  const distFromBottom = before.scrollHeight - before.scrollTop - before.clientHeight;
  const pinned = forceBottom || distFromBottom <= threshold;
  const find = pinned ? null : anchor?.(before);
  // Layout offsets, not rects: a rebuilt row's entrance animation shifts its rect while it plays.
  const heldTop = find?.()?.offsetTop ?? null;
  const scrollTop = before.scrollTop;
  mutate();
  const after = getEl();
  if (!after) return;
  const heldNow = heldTop !== null ? find() : null;
  let targetTop;
  if (pinned) targetTop = after.scrollHeight;
  else if (heldNow) targetTop = scrollTop + heldNow.offsetTop - heldTop;
  else targetTop = Math.max(0, after.scrollHeight - after.clientHeight - distFromBottom);
  after.scrollTo({ top: targetTop, behavior: "instant" });
}
