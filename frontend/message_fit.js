// Restore card blocks that have text and height but collapse to zero width. Run after layout: return the width their
// margins require inside a pannable wrapper, preserving surrounding prose width. Trigger on collapse at any viewport.

/** Characters of text before a zero-width box is worth rescuing. */
const MIN_TEXT = 20;

/** Readable column to seat beside the margins that squeezed the block out. */
const COLUMN = 260;

/** Bounds on the width handed back, so one absurd margin cannot set it. */
const MIN_PAN = 420;
const MAX_PAN = 900;

/** Find blocks with height but no width, excluding hidden 0x0 blocks and inline boxes. */
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
 * Rescue collapsed card layouts under one root or a list of roots.
 * Call after insertion and before measuring bubble height.
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
