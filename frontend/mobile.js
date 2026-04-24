import { $ } from "./utils.js";
import { closeModal, closeCropModal } from "./modal.js";

// ── Mobile config
const MOBILE_SIDEBAR_BREAKPOINT = 900;
const MOBILE_VIEWPORT = window.matchMedia(`(max-width: ${MOBILE_SIDEBAR_BREAKPOINT}px)`);

const IDS = Object.freeze({
  app: "app",
  burgerButton: "burger-btn",
  burgerMenu: "burger-dropdown",
  mobileActionsToggle: "mobile-chat-actions-toggle",
  mobileActionsMenu: "mobile-chat-actions-menu",
  mobileSidebarToggle: "mobile-sidebar-toggle",
  sidebar: "sidebar",
  toolsPanel: "tools-panel",
  toolsPanelToggle: "tools-panel-btn",
  inspector: "inspector",
  inspectorToggle: "inspector-toggle",
  modalRoot: "modal-root",
  cropModalRoot: "modal-crop-root",
});

const APP_STATE = Object.freeze({
  sidebarOpen: "mobile-sidebar-open",
  toolsOpen: "mobile-tools-open",
  inspectorOpen: "mobile-inspector-open",
});

const MOBILE_SIDE_PANELS = Object.freeze([
  {
    elementId: IDS.toolsPanel,
    toggleId: IDS.toolsPanelToggle,
    appStateClass: APP_STATE.toolsOpen,
  },
  {
    elementId: IDS.inspector,
    toggleId: IDS.inspectorToggle,
    appStateClass: APP_STATE.inspectorOpen,
  },
]);

let _mobileBackArmed = false;
let _handlingMobilePop = false;
let _initialized = false;
let _closeBurger = () => {};

// ── DOM/state helpers
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

function setElementOpen(id, open) {
  getElement(id)?.classList.toggle("open", open);
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

export function closeMobileHeaderActions() {
  setElementOpen(IDS.mobileActionsMenu, false);
}

export function toggleMobileHeaderActions() {
  if (!isMobileSidebarViewport()) return;
  setElementOpen(IDS.mobileActionsMenu, !isElementOpen(IDS.mobileActionsMenu));
  _closeBurger();
  armMobileBackIfNeeded();
}

// ── Panel sync
function syncMobilePanelState() {
  const app = getApp();
  if (!app) return;

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

  if (anyUtilityPanelOpen) {
    closeMobileSidebar();
    closeMobileHeaderActions();
  }
}

function closeMobileUtilityPanels() {
  for (const panel of MOBILE_SIDE_PANELS) {
    setElementOpen(panel.elementId, false);
  }
  syncMobilePanelState();
}

// ── Overlay stack
function hasOpenBaseModal() {
  return Boolean(getElement(IDS.modalRoot)?.firstElementChild);
}

function hasOpenCropModal() {
  return Boolean(getElement(IDS.cropModalRoot)?.firstElementChild);
}

function hasOpenMobileOverlay() {
  if (!isMobileSidebarViewport()) return false;

  return (
    hasOpenCropModal() ||
    hasOpenBaseModal() ||
    isElementOpen(IDS.mobileActionsMenu) ||
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

  if (hasOpenCropModal()) {
    closeCropModal();
    return true;
  }
  if (hasOpenBaseModal()) {
    closeModal();
    return true;
  }
  if (isElementOpen(IDS.mobileActionsMenu)) {
    closeMobileHeaderActions();
    return true;
  }
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

export function toggleMobileSidebar() {
  if (!isMobileSidebarViewport()) return;
  closeMobileUtilityPanels();
  closeMobileHeaderActions();
  setAppState(APP_STATE.sidebarOpen, !hasAppState(APP_STATE.sidebarOpen));
  _closeBurger();
  armMobileBackIfNeeded();
}

// ── Event handlers
function handleDocumentClick(event) {
  const matcher = createEventMatcher(event);
  const clickedMobileActionsMenu = matcher.hasId(IDS.mobileActionsMenu);
  const sidebarOpen = hasAppState(APP_STATE.sidebarOpen);

  if (!matcher.hasId(IDS.mobileActionsToggle) && !clickedMobileActionsMenu) {
    closeMobileHeaderActions();
  }

  if (!isMobileSidebarViewport()) return;

  if (sidebarOpen && matcher.matches("#sidebar .btn, #sidebar .char-item, #sidebar .fragment-item")) {
    setTimeout(closeMobileSidebar, 0);
  }

  if (sidebarOpen && !matcher.hasId(IDS.sidebar) && !matcher.hasId(IDS.mobileSidebarToggle)) {
    closeMobileSidebar();
  }

  let closedUtilityPanel = false;
  for (const panel of MOBILE_SIDE_PANELS) {
    if (!isElementOpen(panel.elementId)) continue;
    if (matcher.hasId(panel.elementId) || matcher.hasId(panel.toggleId) || clickedMobileActionsMenu) continue;

    setElementOpen(panel.elementId, false);
    closedUtilityPanel = true;
  }

  if (closedUtilityPanel) {
    syncMobilePanelState();
  }
}

function handleEscape(event) {
  if (event.key !== "Escape") return;
  closeMobileSidebar();
  closeMobileHeaderActions();
  closeMobileUtilityPanels();
}

function handleViewportChange() {
  if (!isMobileSidebarViewport()) {
    closeMobileSidebar();
    closeMobileHeaderActions();
  }
  syncMobilePanelState();
  armMobileBackIfNeeded();
}

// ── Observers
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

// ── Init
export function initMobileUi(deps) {
  if (_initialized) return;
  _initialized = true;
  _closeBurger = deps.closeBurger;

  document.addEventListener("click", handleDocumentClick);
  window.addEventListener("keydown", handleEscape);
  bindViewportListener(handleViewportChange);

  observeClassChanges(
    MOBILE_SIDE_PANELS.map(({ elementId }) => elementId),
    () => {
      syncMobilePanelState();
      if (!_handlingMobilePop) armMobileBackIfNeeded();
    },
  );

  observeChildChanges([IDS.modalRoot, IDS.cropModalRoot], () => {
    if (!_handlingMobilePop) armMobileBackIfNeeded();
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
