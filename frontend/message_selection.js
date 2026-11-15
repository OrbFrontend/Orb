/** Map a selection in a rendered message body back to the span of saved text it shows. */

// What rendering hides between two visible characters: whitespace, emphasis and code marks, heading hashes, tags, links.
const GAP = "(?:[\\s*`#\\[]|<[^>]*>|\\]\\([^)]*\\))*";
const WORD = /[\p{L}\p{N}_'’]/u;

const visible = (text) => text.replace(/\s+/g, "");
const escapeRe = (ch) => ch.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");

/**
 * The selected span of *source*, widened to whole words: `{start, text}` with *start* in code points, `null` when
 * nothing in *body* is selected, or `false` when the selection does not match *source*.
 */
export function selectionInSource(body, range, source) {
  if (!body || !range || range.collapsed || !range.intersectsNode(body)) return null;
  const head = document.createRange();
  head.selectNodeContents(body);
  const tail = head.cloneRange();
  if (body.contains(range.startContainer)) head.setEnd(range.startContainer, range.startOffset);
  else head.collapse(true);
  if (body.contains(range.endContainer)) tail.setEnd(range.endContainer, range.endOffset);
  const prefix = visible(head.toString());
  const needle = visible(tail.toString()).slice(prefix.length);
  if (!needle) return null;
  // Which repeat of the selected text this is, counted in what the reader sees.
  let ordinal = 0;
  for (let i = prefix.indexOf(needle); i !== -1; i = prefix.indexOf(needle, i + needle.length)) ordinal++;
  const match = [...source.matchAll(new RegExp(Array.from(needle, escapeRe).join(GAP), "gu"))][ordinal];
  if (!match) return false;
  let start = match.index;
  let end = start + match[0].length;
  while (start > 0 && WORD.test(source[start - 1]) && WORD.test(source[start])) start--;
  while (end < source.length && WORD.test(source[end]) && WORD.test(source[end - 1])) end++;
  return { start: Array.from(source.slice(0, start)).length, text: source.slice(start, end) };
}
