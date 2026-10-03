import { fromMessageBody } from "./utils.js";

// Delegated UI actions for markup rendered from HTML strings. An element names its
// action with data-wf-action="<scope>:<name>", and the module that renders that
// markup registers the handler, which receives (element, event). Click is the
// default event; data-wf-on lists others, space-separated. Only the innermost
// element naming an action is considered, and model markup inside .msg-body
// never names one.

const _actions = new Map();
let _wired = false;

const _ACTION_EVENTS = ["click", "change", "input", "keydown", "dragover", "dragleave", "drop"];

function _dispatch(e, type) {
  const el = e.target.closest?.("[data-wf-action]");
  if (!el || fromMessageBody(el)) return;
  if (!(el.dataset.wfOn || "click").split(/\s+/).includes(type)) return;
  const fn = _actions.get(el.dataset.wfAction);
  if (!fn) return;
  try {
    fn(el, e);
  } catch (err) {
    console.error(`data-wf-action "${el.dataset.wfAction}" handler threw:`, err);
  }
}

function _wire() {
  if (_wired) return;
  _wired = true;
  for (const type of _ACTION_EVENTS) document.addEventListener(type, (e) => _dispatch(e, type));
}

export function registerAction(scope, name, fn) {
  if (typeof scope !== "string" || !scope || typeof name !== "string" || !name) {
    console.error("registerAction: scope and name must be non-empty strings", scope, name);
    return;
  }
  if (typeof fn !== "function") {
    console.error(`registerAction: fn must be a function (${scope}:${name})`);
    return;
  }
  _wire();
  _actions.set(`${scope}:${name}`, fn);
}

/** Register several actions under one scope: `{ name: (el, event) => ... }`. */
export function registerActions(scope, handlers) {
  for (const [name, fn] of Object.entries(handlers)) registerAction(scope, name, fn);
}
