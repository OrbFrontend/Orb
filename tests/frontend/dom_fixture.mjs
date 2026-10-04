const CORE_GLOBALS = ["document", "Node", "NodeFilter", "Element", "DocumentFragment", "HTMLElement", "DOMParser"];
export const MESSAGE_GLOBALS = [...CORE_GLOBALS, "HTMLUnknownElement", "HTMLImageElement", "MouseEvent"];

// Install before importing DOMPurify: it binds to window during module evaluation.
// Node's navigator is a getter-only global; the renderers tolerate its absent clipboard.
export async function loadDom({ html = "<!doctype html><html><body></body></html>", globals = CORE_GLOBALS } = {}) {
  let dom;
  try {
    const { JSDOM } = await import("jsdom");
    dom = new JSDOM(html, { url: "https://orb.invalid/" });
  } catch (e) {
    return { dom: null, failure: e?.message || String(e) };
  }
  globalThis.window = dom.window;
  for (const name of globals) {
    if (dom.window[name] !== undefined) globalThis[name] = dom.window[name];
  }
  return { dom, failure: "" };
}

// String-rendering tests need only the detached element used by utils.esc.
export function installEscapingDocument() {
  globalThis.document = {
    createElement() {
      return {
        innerHTML: "",
        set textContent(value) {
          this.innerHTML = String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        },
      };
    },
  };
}
