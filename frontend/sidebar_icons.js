// Sidebar actions must be vector artwork rather than font glyphs: themes are
// free to change --font-ui, while control geometry needs to remain stable.
const icon = (paths) =>
  `<svg class="sidebar-action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths}</svg>`;

export const SIDEBAR_EDIT_ICON = icon(
  '<path d="M12 20H5a1 1 0 0 1-1-1v-7"/><path d="m16.5 3.5 4 4L11 17l-4 1 1-4 9.5-9.5z"/>',
);

export const SIDEBAR_CLOSE_ICON = icon('<path d="m6 6 12 12M18 6 6 18"/>');
