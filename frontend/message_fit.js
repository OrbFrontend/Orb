// Post-layout rescue for card HTML that was written for a desktop window.
//
// message_html.js scopes a card's CSS and chat.css contains it, but neither can
// see that the card's layout assumes a viewport far wider than a phone bubble.
// The failure that produces is specific, and silent: a two-column card built the
// way the old web built them -- `.rail { float: right; width: 300px }` beside
// `.article { margin-right: 320px }` -- leaves the article a negative content
// width inside a ~290px bubble. It clamps to zero, every word wraps onto its own
// line, and the message becomes thousands of pixels of blank space. `contain:
// paint` on `.msg-css-scope` then clips the overflow, so there is no scrollbar
// and nothing to hint that the card is there at all.
//
// Only layout knows this happened, so the check has to run after insertion: a
// block that has height but no width, and real text inside it, has collapsed.
// Such a block gets back the width its own margins asked for, inside a
// horizontally pannable wrapper, so the card lays out as its author wrote it and
// the prose around it keeps the bubble's width. Cards that already fit are left
// alone -- the collapse itself is the trigger, not the viewport, so a narrow
// desktop window is rescued on the same terms as a phone.

/** Characters of text before a zero-width box is worth rescuing. */
const MIN_TEXT = 20;

/** Readable column to seat beside the margins that squeezed the block out. */
const COLUMN = 260;

/** Bounds on the width handed back, so one absurd margin cannot set it. */
const MIN_PAN = 420;
const MAX_PAN = 900;

/**
 * Blocks that laid out with height but no width.
 *
 * A hidden block measures 0x0, and an inline box that wraps tightly still
 * reports a width, so height-without-width is what separates a real collapse
 * from either.
 */
function collapsedBlocks(scope) {
  const found = [];
  for (const el of scope.querySelectorAll("*")) {
    const text = el.textContent;
    if (!text || text.trim().length < MIN_TEXT) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width >= 1 || rect.height < 1) continue;
    found.push(el);
  }
  return found;
}

/** The width the collapsed blocks' own horizontal margins were written for. */
function panWidth(blocks) {
  let needed = 0;
  for (const el of blocks) {
    const cs = getComputedStyle(el);
    needed = Math.max(needed, (parseFloat(cs.marginLeft) || 0) + (parseFloat(cs.marginRight) || 0));
  }
  return Math.min(MAX_PAN, Math.max(MIN_PAN, Math.round(needed + COLUMN)));
}

/** Hoist each collapsed block to the outermost card block inside the scope. */
function outermostBlocks(scope, blocks) {
  const roots = new Set();
  for (const el of blocks) {
    let node = el;
    while (node.parentElement && node.parentElement !== scope) node = node.parentElement;
    if (node.parentElement === scope) roots.add(node);
  }
  return roots;
}

function fitScope(scope) {
  // One pass per scope. A re-render replaces the wrapper along with the rest of
  // the body, so the flag never outlives the markup it describes.
  if (scope.dataset.orbFit) return;
  scope.dataset.orbFit = "1";
  const blocks = collapsedBlocks(scope);
  if (!blocks.length) return;
  const width = panWidth(blocks);
  for (const block of outermostBlocks(scope, blocks)) {
    const pan = document.createElement("div");
    pan.className = "msg-pan";
    block.replaceWith(pan);
    pan.appendChild(block);
    // Inline, because the card's own rule for this block may carry `!important`
    // and this has to outrank it without competing on selector specificity.
    block.style.minWidth = `${width}px`;
  }
}

/**
 * Rescue collapsed card layouts under `roots` (an element, or a list of them).
 *
 * Call after the markup is in the document and before anything measures the
 * bubble's height: rescuing changes it by thousands of pixels.
 */
export function fitMessageCards(roots) {
  if (!roots) return;
  const nodes = roots instanceof Element ? [roots] : Array.from(roots);
  for (const node of nodes) {
    if (!(node instanceof Element)) continue;
    if (node.matches(".msg-css-scope")) fitScope(node);
    for (const scope of node.querySelectorAll(".msg-css-scope")) fitScope(scope);
  }
}
