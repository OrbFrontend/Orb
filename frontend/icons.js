// Shared inline SVG icons.
//
// Icons are drawn, never typed. A glyph like "×" is centred against the font's
// metrics, not against its own ink, so the same button looks centred in one
// theme and visibly low or high in the next -- every theme swaps --font-ui, and
// several also hang a text-shadow off it. Symbol glyphs make it worse: "⠿" and
// "⋯" miss most UI fonts entirely and land in whatever the OS substitutes, so
// they drift again per platform. A stroked path in a square viewBox is centred
// by geometry, so it lands identically in every theme and on every OS.
//
// Size with --ui-icon-size (default 16px); pair with .btn-square (css/forms.css)
// for an icon-only button that needs a fixed box.
const icon = (paths) =>
  `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths}</svg>`;

const dot = (cx, cy, r = 1.6) => `<circle cx="${cx}" cy="${cy}" r="${r}" fill="currentColor" stroke="none"/>`;

export const EDIT_ICON_PATHS = '<path d="M12 20H5a1 1 0 0 1-1-1v-7"/><path d="m16.5 3.5 4 4L11 17l-4 1 1-4 9.5-9.5z"/>';
export const CLOSE_ICON_PATHS = '<path d="m6 6 12 12M18 6 6 18"/>';

export const EDIT_ICON = icon(EDIT_ICON_PATHS);
export const CLOSE_ICON = icon(CLOSE_ICON_PATHS);
export const PLUS_ICON = icon('<path d="M12 5v14M5 12h14"/>');
export const MENU_ICON = icon('<path d="M3 6h18M3 12h18M3 18h18"/>');
export const DOWNLOAD_ICON = icon('<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 20h16"/>');
export const CHEVRON_DOWN_ICON = icon('<path d="m6 9 6 6 6-6"/>');
export const CHEVRON_LEFT_ICON = icon('<path d="m15 18-6-6 6-6"/>');
export const CHEVRON_RIGHT_ICON = icon('<path d="m9 18 6-6-6-6"/>');
export const ARROW_LEFT_ICON = icon('<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>');
export const ELLIPSIS_ICON = icon(dot(5, 12) + dot(12, 12) + dot(19, 12));
export const GRIP_ICON = icon(
  [dot(9, 5, 1.5), dot(15, 5, 1.5), dot(9, 12, 1.5), dot(15, 12, 1.5), dot(9, 19, 1.5), dot(15, 19, 1.5)].join(""),
);

// Code-block toolbar. Drawn at 14px with a lighter stroke than `icon()` above:
// they sit inside a message bubble, where the 16px/2 UI weight reads as chrome
// competing with the prose. message_html.js rebuilds this toolbar after
// sanitising, so these strings never pass through DOMPurify.
const codeIcon = (paths) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" aria-hidden="true" focusable="false">${paths}</svg>`;

export const CODE_WRAP_ICON = codeIcon(
  '<line x1="3" y1="6" x2="21" y2="6"/><path d="M3 12h15a3 3 0 1 1 0 6h-4"/><polyline points="16 16 14 18 16 20"/><line x1="3" y1="18" x2="10" y2="18"/>',
);
export const CODE_COPY_ICON = codeIcon(
  '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
);
