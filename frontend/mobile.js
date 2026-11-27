import { registerActions } from "./actions.js";
import { ARROW_LEFT_ICON } from "./icons.js";
import { closeTopModal, isModalOpen } from "./modal.js";
import { closeUtilityPanel } from "./panels.js";
import { $ } from "./utils.js";

const MOBILE_SIDEBAR_BREAKPOINT = 900;
const MOBILE_VIEWPORT = window.matchMedia(`(max-width: ${MOBILE_SIDEBAR_BREAKPOINT}px)`);

const IDS = Object.freeze({
  app: "app",
  burgerButton: "burger-btn",
  burgerMenu: "burger-dropdown",
  docWorkflowMobileToggle: "doc-workflow-mobile-btn",
  mobileSidebarToggle: "mobile-sidebar-toggle",
  docMobileSidebarToggle: "doc-mobile-sidebar-toggle",
  sidebar: "sidebar",
  chatAvatar: "chat-avatar",
  toolsPanel: "tools-panel",
  toolsPanelToggle: "tools-panel-btn",
  inspector: "inspector",
  inspectorToggle: "inspector-toggle",
  assistantPanel: "assistant-panel",
  assistantToggle: "assistant-btn",
  docAssistantMobileToggle: "doc-assistant-mobile-btn",
  drawerAssistantToggle: "lb-assistant-btn",
  modalRoot: "modal-root",
  subModalRoot: "modal-sub-root",
  cropModalRoot: "modal-crop-root",
});

const APP_STATE = Object.freeze({
  sidebarOpen: "mobile-sidebar-open",
  toolsOpen: "mobile-tools-open",
  inspectorOpen: "mobile-inspector-open",
  assistantOpen: "mobile-assistant-open",
  sidebarCollapsed: "sidebar-collapsed",
  sidebarToggled: "sidebar-toggled",
});

// A tap anywhere but the panel or one of its toggles closes the panel.
const MOBILE_SIDE_PANELS = Object.freeze([
  {
    elementId: IDS.toolsPanel,
    toggleIds: [IDS.toolsPanelToggle, IDS.docWorkflowMobileToggle],
    appStateClass: APP_STATE.toolsOpen,
  },
  {
    elementId: IDS.inspector,
    toggleIds: [IDS.inspectorToggle],
    appStateClass: APP_STATE.inspectorOpen,
  },
  {
    elementId: IDS.assistantPanel,
    toggleIds: [IDS.assistantToggle, IDS.docAssistantMobileToggle, IDS.drawerAssistantToggle],
    appStateClass: APP_STATE.assistantOpen,
  },
]);

let _mobileBackArmed = false;
let _handlingMobilePop = false;
let _initialized = false;
let _closeBurger = () => {};
let _closeSidebarPortrait = () => false;

function getElement(id) {
  return $(id);
}

function getApp() {
  return getElement(IDS.app);
}

function isMobileSidebarViewport() {
  return MOBILE_VIEWPORT.matches;
}

function isElementOpen(id) {
  return getElement(id)?.classList.contains("open") ?? false;
}

function hasAppState(stateClass) {
  return getApp()?.classList.contains(stateClass) ?? false;
}

function setAppState(stateClass, enabled) {
  getApp()?.classList.toggle(stateClass, enabled);
}

function createEventMatcher(event) {
  const path = typeof event.composedPath === "function" ? event.composedPath() : [];
  const target = event.target instanceof Element ? event.target : null;

  return {
    hasId(id) {
      return path.some((node) => node instanceof Element && node.id === id) || Boolean(target?.closest(`#${id}`));
    },
    matches(selector) {
      return Boolean(target?.closest(selector));
    },
  };
}

function closeMobileSidebar() {
  setAppState(APP_STATE.sidebarOpen, false);
}

// Slide-overs start under the visible toggle header (the lorebook drawer covers the chat's), so the opening tap closes.
const PANEL_TOGGLE_HEADERS = ["#lorebook-drawer:not(.hidden) .lb-header", "#doc-header", "#chat-header"];

function syncMobilePanelTop() {
  const header = PANEL_TOGGLE_HEADERS.map((sel) => document.querySelector(sel)).find(
    (el) => el?.getClientRects().length,
  );
  const top = header ? Math.max(0, Math.round(header.getBoundingClientRect().bottom)) : 0;
  getApp()?.style.setProperty("--mobile-panel-top", `${top}px`);
}

function syncMobilePanelState() {
  const app = getApp();
  if (!app) return;
  syncMobilePanelTop();

  if (!isMobileSidebarViewport()) {
    for (const panel of MOBILE_SIDE_PANELS) {
      app.classList.remove(panel.appStateClass);
    }
    return;
  }

  let anyUtilityPanelOpen = false;
  for (const panel of MOBILE_SIDE_PANELS) {
    const isOpen = isElementOpen(panel.elementId);
    app.classList.toggle(panel.appStateClass, isOpen);
    anyUtilityPanelOpen ||= isOpen;
  }

  if (anyUtilityPanelOpen) closeMobileSidebar();
}

function closeMobileUtilityPanels() {
  for (const panel of MOBILE_SIDE_PANELS) closeUtilityPanel(panel.elementId);
  syncMobilePanelState();
}

function hasOpenMobileOverlay() {
  if (!isMobileSidebarViewport()) return false;

  return (
    isModalOpen() ||
    hasAppState(APP_STATE.sidebarOpen) ||
    MOBILE_SIDE_PANELS.some(({ elementId, appStateClass }) => isElementOpen(elementId) || hasAppState(appStateClass))
  );
}

function armMobileBackIfNeeded() {
  if (_handlingMobilePop || !isMobileSidebarViewport() || _mobileBackArmed || !hasOpenMobileOverlay()) return;
  history.pushState({ orbMobileOverlay: true }, "");
  _mobileBackArmed = true;
}

function closeTopMobileOverlay() {
  if (!isMobileSidebarViewport()) return false;

  if (closeTopModal()) return true;
  if (MOBILE_SIDE_PANELS.some(({ elementId }) => isElementOpen(elementId))) {
    closeMobileUtilityPanels();
    return true;
  }
  if (hasAppState(APP_STATE.sidebarOpen)) {
    closeMobileSidebar();
    return true;
  }

  return false;
}

function toggleMobileSidebar() {
  if (!isMobileSidebarViewport()) return;
  closeMobileUtilityPanels();
  syncMobilePanelTop();
  // The portrait shares the drawer: from it, the menu button switches to the menu rather than closing.
  const sidebarOpen = hasAppState(APP_STATE.sidebarOpen);
  if (!(sidebarOpen && _closeSidebarPortrait())) setAppState(APP_STATE.sidebarOpen, !sidebarOpen);
  _closeBurger();
  armMobileBackIfNeeded();
}

/** Close the narrow layout's sidebar drawer; the wide layout's column is left as it is. */
export function closeSidebarDrawer() {
  if (isMobileSidebarViewport()) closeMobileSidebar();
}

/** Whether the sidebar column is on screen: the open drawer when narrow, the uncollapsed column otherwise. */
export function sidebarShown() {
  if (isMobileSidebarViewport()) return hasAppState(APP_STATE.sidebarOpen);
  return !hasAppState(APP_STATE.sidebarCollapsed);
}

/** Brings the sidebar on screen; returns how to put it back, or null when it was already showing. */
export function revealSidebar() {
  if (sidebarShown()) return null;
  if (isMobileSidebarViewport()) {
    toggleMobileSidebar();
    return closeMobileSidebar;
  }
  setAppState(APP_STATE.sidebarToggled, true);
  setAppState(APP_STATE.sidebarCollapsed, false);
  return () => setAppState(APP_STATE.sidebarCollapsed, true);
}

function handleDocumentClick(event) {
  const matcher = createEventMatcher(event);
  const sidebarOpen = hasAppState(APP_STATE.sidebarOpen);

  if (!isMobileSidebarViewport()) return;

  // Picking an item closes the sidebar; its own buttons (edit, delete) do not.
  if (sidebarOpen && matcher.matches(".char-item, .world-item-main, .doc-item") && !matcher.matches("button")) {
    setTimeout(closeMobileSidebar, 0);
  }

  if (
    sidebarOpen &&
    !matcher.hasId(IDS.sidebar) &&
    !matcher.hasId(IDS.mobileSidebarToggle) &&
    !matcher.hasId(IDS.docMobileSidebarToggle) &&
    !matcher.hasId(IDS.chatAvatar) &&
    !matcher.hasId(IDS.modalRoot) &&
    !matcher.hasId(IDS.subModalRoot) &&
    !matcher.hasId(IDS.cropModalRoot)
  ) {
    closeMobileSidebar();
  }

  let closedUtilityPanel = false;
  for (const panel of MOBILE_SIDE_PANELS) {
    if (!isElementOpen(panel.elementId)) continue;
    if (matcher.hasId(panel.elementId) || panel.toggleIds.some((id) => matcher.hasId(id))) continue;

    closeUtilityPanel(panel.elementId);
    closedUtilityPanel = true;
  }

  if (closedUtilityPanel) {
    syncMobilePanelState();
  }
}

function handleEscape(event) {
  if (event.key !== "Escape") return;
  closeMobileSidebar();
}

function handleViewportChange() {
  if (!isMobileSidebarViewport()) closeMobileSidebar();
  syncMobilePanelState();
  armMobileBackIfNeeded();
}

function observeClassChanges(ids, callback) {
  const elements = ids.map(getElement).filter(Boolean);
  if (elements.length === 0) return;

  const observer = new MutationObserver(callback);
  for (const element of elements) {
    observer.observe(element, { attributes: true, attributeFilter: ["class"] });
  }
}

function observeChildChanges(ids, callback) {
  const elements = ids.map(getElement).filter(Boolean);
  if (elements.length === 0) return;

  const observer = new MutationObserver(callback);
  for (const element of elements) {
    observer.observe(element, { childList: true });
  }
}

function bindViewportListener(handler) {
  if (typeof MOBILE_VIEWPORT.addEventListener === "function") {
    MOBILE_VIEWPORT.addEventListener("change", handler);
    return;
  }

  MOBILE_VIEWPORT.addListener(handler);
}

// The keyboard shrinks the layout viewport (resizes-content), so vh-based sizes shrink with it. --app-full-height holds
// its height while a field has focus, so sizes built on it stay put when the keyboard opens.
let _fullHeight = { width: 0, height: 0 };

function typing() {
  return document.activeElement?.matches("input, textarea, select, [contenteditable]") ?? false;
}

function trackVisualViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const apply = () => {
    const root = document.documentElement.style;
    root.setProperty("--app-height", `${Math.round(vv.height)}px`);
    root.setProperty("--app-offset", `${Math.round(vv.offsetTop)}px`);
    const { innerWidth: width, innerHeight: height } = window;
    if (!typing() || width !== _fullHeight.width || height > _fullHeight.height) {
      _fullHeight = { width, height };
      root.setProperty("--app-full-height", `${height}px`);
    }
  };
  apply();
  vv.addEventListener("resize", apply);
  vv.addEventListener("scroll", apply); // offsetTop changes fire scroll, not resize
}

// iOS zooms into a focused field set below 16px and stays zoomed. Its focus zoom
// honours maximum-scale while pinch-zoom ignores it there; Android would lose pinch-zoom.
function preventIosFocusZoom() {
  if (!CSS.supports("-webkit-touch-callout", "none")) return;
  const meta = document.querySelector('meta[name="viewport"]');
  if (meta && !meta.content.includes("maximum-scale")) meta.content += ", maximum-scale=1";
}

export function initMobileUi(deps) {
  if (_initialized) return;
  _initialized = true;
  _closeBurger = deps.closeBurger;
  _closeSidebarPortrait = deps.closeSidebarPortrait;
  trackVisualViewport();
  preventIosFocusZoom();

  document.addEventListener("click", handleDocumentClick);
  window.addEventListener("keydown", handleEscape);
  window.addEventListener("resize", () => {
    if (hasOpenMobileOverlay()) syncMobilePanelTop();
  });
  bindViewportListener(handleViewportChange);

  observeClassChanges(
    MOBILE_SIDE_PANELS.map(({ elementId }) => elementId),
    () => {
      syncMobilePanelState();
      if (!_handlingMobilePop) armMobileBackIfNeeded();
    },
  );

  observeChildChanges([IDS.modalRoot, IDS.subModalRoot, IDS.cropModalRoot], () => {
    if (!_handlingMobilePop) armMobileBackIfNeeded();
  });

  observeChildChanges(["lorebook-drawer"], () => {
    const header = document.querySelector("#lorebook-drawer .lb-editor-header");
    if (!header || header.querySelector(".lb-back-btn")) return;
    const btn = document.createElement("button");
    btn.className = "btn btn-sm lb-back-btn";
    btn.title = "Back to entries";
    btn.setAttribute("aria-label", "Back to entries");
    btn.innerHTML = ARROW_LEFT_ICON;
    btn.dataset.wfAction = "lorebook:backToList";
    header.insertBefore(btn, header.firstChild);
  });

  syncMobilePanelState();

  window.addEventListener("popstate", () => {
    _mobileBackArmed = false;
    if (!isMobileSidebarViewport()) return;

    _handlingMobilePop = true;
    const closedAny = closeTopMobileOverlay();
    _handlingMobilePop = false;

    if (closedAny && hasOpenMobileOverlay()) {
      armMobileBackIfNeeded();
    }
  });
}

registerActions("mobile", {
  toggleSidebar: () => toggleMobileSidebar(),
});
