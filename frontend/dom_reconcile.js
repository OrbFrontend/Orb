// Keyed child reconciliation for containers rendered from HTML strings.
//
// Assigning `innerHTML` throws away every child, even the ones whose markup did
// not change: CSS entrance animations replay across the whole list, images and
// scroll anchors are rebuilt, and every node has to be laid out again. This
// keeps the nodes whose markup is byte-identical to the previous pass and
// touches only the rows that actually differ.

// container -> Map(key -> html string produced for it last pass).
const _signatures = new WeakMap();

function signaturesFor(container) {
  let sigs = _signatures.get(container);
  if (!sigs) {
    sigs = new Map();
    _signatures.set(container, sigs);
  }
  return sigs;
}

/**
 * Sync `container`'s children to `entries` — `[{ key, html }]`, in document
 * order. Each `html` must have exactly one root element. Children that this
 * function did not create are removed, matching the `innerHTML` assignment
 * callers replace with it, so anything the caller re-attaches afterwards (a
 * badge, a streaming bubble) still lands last.
 *
 * A row whose html is unchanged keeps its existing DOM node, untouched. A row
 * that changed is rebuilt and, since it replaces a node that was already on
 * screen, gets `swapClass` so the caller's CSS can skip the entrance animation
 * for an in-place update while genuinely new rows still animate.
 *
 * Returns the elements built this pass, so the caller can restrict per-node
 * work (measuring, observers) to just those.
 */
export function reconcileChildren(container, entries, swapClass = null) {
  const sigs = signaturesFor(container);
  const wanted = new Set(entries.map((e) => e.key));

  const kept = new Map();
  for (const el of Array.from(container.children)) {
    const key = el.dataset ? el.dataset.rkey : undefined;
    if (key === undefined || !wanted.has(key) || kept.has(key)) el.remove();
    else kept.set(key, el);
  }

  const scratch = document.createElement("div");
  const fresh = [];
  let cursor = null;
  for (const { key, html } of entries) {
    let el = kept.get(key);
    kept.delete(key); // a repeated key must not re-adopt the node the first one used
    if (!el || sigs.get(key) !== html) {
      scratch.innerHTML = html;
      const built = scratch.firstElementChild;
      if (!built) {
        el?.remove();
        sigs.delete(key);
        continue;
      }
      built.dataset.rkey = key;
      if (el && swapClass) built.classList.add(swapClass);
      el?.remove();
      el = built;
      sigs.set(key, html);
      fresh.push(el);
    }
    // Only move a node that is genuinely out of place. insertBefore detaches and
    // re-attaches, which restarts the node's CSS animations.
    const anchor = cursor ? cursor.nextSibling : container.firstChild;
    if (el !== anchor) container.insertBefore(el, anchor);
    cursor = el;
  }

  for (const key of sigs.keys()) if (!wanted.has(key)) sigs.delete(key);
  return fresh;
}
