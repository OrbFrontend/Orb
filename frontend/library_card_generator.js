// Character Library card generator: each mounted panel owns its request.
import { SPARKLE_ICON } from "./icons.js";
import { sseEvents, streamPost, unescapeSSE } from "./sse.js";

let _unmount = null;

const TAILORING_NOTES = {
  off: "Draft from your idea alone.",
  summary: "Use your library's tags, persona names, and most-played characters as inspiration.",
  deep: "Sends excerpts of your chats, characters and persona descriptions to your Agent endpoint. Use a provider you trust with that data. Slower; thinking is always on.",
};

export function cardGeneratorToolHtml() {
  return `
    <section class="lib-tool" data-tool="card-generator">
      <header class="lib-tool-head">
        <span class="lib-tool-icon">${SPARKLE_ICON}</span>
        <div class="lib-tool-heading">
          <h3 class="lib-tool-name">Card generator</h3>
          <p class="lib-manager-note">Describe a character and let the Agent model draft a card. Review and edit it before saving to your library.</p>
        </div>
      </header>
      <div class="lib-tool-body">
        <label class="lib-manager-field">
          <span class="lib-manager-field-label">Character idea</span>
          <textarea data-cardgen-idea rows="4" maxlength="2000" placeholder="A jaded harbour-town fence who owes everyone money…"></textarea>
        </label>
        <label class="lib-manager-field">
          <span class="lib-manager-field-label">Tailored to me</span>
          <select data-cardgen-tailoring>
            <option value="off">Off</option>
            <option value="summary">Library summary</option>
            <option value="deep">Deep: reads your chats</option>
          </select>
          <span class="lib-manager-note" data-cardgen-tailoring-note>${TAILORING_NOTES.off}</span>
        </label>
        <label class="lib-manager-toggle">
          <input type="checkbox" data-cardgen-reasoning>
          <span class="lib-manager-toggle-text">
            <span class="lib-manager-toggle-label">Enable generator thinking</span>
            <span class="lib-manager-note">Slower and uses more model resources.</span>
          </span>
        </label>
        <div class="lib-manager-actions">
          <div class="lib-manager-status" data-cardgen-progress role="status" aria-live="polite"></div>
          <button class="btn btn-accent" data-cardgen-action="generate">Generate</button>
          <button class="btn" data-cardgen-action="cancel" hidden>Cancel</button>
        </div>
      </div>
    </section>`;
}

export function mountCardGenerator(root, callbacks = {}) {
  _unmount?.();
  let controller = null;
  const idea = root.querySelector("[data-cardgen-idea]");
  const reasoning = root.querySelector("[data-cardgen-reasoning]");
  const tailoring = root.querySelector("[data-cardgen-tailoring]");
  const tailoringNote = root.querySelector("[data-cardgen-tailoring-note]");
  const generate = root.querySelector('[data-cardgen-action="generate"]');
  const cancel = root.querySelector('[data-cardgen-action="cancel"]');
  const progress = root.querySelector("[data-cardgen-progress]");

  function status(message, { error = false } = {}) {
    progress.textContent = message;
    progress.classList.toggle("is-error", error);
  }

  // Deep research always thinks. The user's own choice is kept aside while the
  // checkbox is locked on, and comes back when they pick another mode.
  let reasoningBeforeDeep = null;
  function syncTailoring() {
    const deep = tailoring.value === "deep";
    if (deep && reasoningBeforeDeep === null) {
      reasoningBeforeDeep = reasoning.checked;
      reasoning.checked = true;
    } else if (!deep && reasoningBeforeDeep !== null) {
      reasoning.checked = reasoningBeforeDeep;
      reasoningBeforeDeep = null;
    }
    tailoringNote.textContent = TAILORING_NOTES[tailoring.value];
    tailoringNote.classList.toggle("is-warning", deep);
  }

  function paint() {
    generate.disabled = !!controller || !idea.value.trim();
    generate.textContent = controller ? "Generating…" : "Generate";
    cancel.hidden = !controller;
    idea.disabled = tailoring.disabled = !!controller;
    reasoning.disabled = !!controller || tailoring.value === "deep";
    root.classList.toggle("ml-busy", !!controller);
  }

  function onChange(event) {
    if (event.target === tailoring) syncTailoring();
    paint();
  }

  async function run() {
    if (controller || !root.isConnected || !idea.value.trim()) return;
    const runController = new AbortController();
    controller = runController;
    paint();
    status("");
    try {
      const response = await streamPost(
        "/library/card-generator/run",
        { idea: idea.value.trim(), reasoning: reasoning.checked, tailoring: tailoring.value },
        runController.signal,
      );
      if (!response.ok) throw new Error(`Generation request failed (${response.status})`);
      let finished = false;
      for await (const { event, data } of sseEvents(response.body, { signal: runController.signal })) {
        if (runController.signal.aborted || !root.isConnected) return;
        if (event === "progress") status(JSON.parse(data).label);
        else if (event === "error") throw new Error(unescapeSSE(data));
        else if (event === "done") {
          finished = true;
          status("Draft ready to review.");
          await callbacks.onOpenDraft?.(JSON.parse(data).card);
          break;
        }
      }
      if (runController.signal.aborted) status("Generation cancelled.");
      else if (!finished) status("Generation ended without a draft. Try again.", { error: true });
    } catch (error) {
      if (root.isConnected) {
        const aborted = runController.signal.aborted;
        status(aborted ? "Generation cancelled." : error.message, { error: !aborted });
      }
    } finally {
      controller = null;
      if (root.isConnected) paint();
    }
  }

  function onClick(event) {
    const action = event.target.closest("[data-cardgen-action]")?.dataset.cardgenAction;
    if (action === "generate") run();
    else if (action === "cancel") controller?.abort();
  }

  // Modal close and tab switches replace DOM outside this module. Observe only
  // while mounted so those paths cancel the upstream call immediately too.
  const observer = new MutationObserver(() => {
    if (!root.isConnected) dispose();
  });
  function dispose() {
    controller?.abort();
    observer.disconnect();
    root.removeEventListener("click", onClick);
    root.removeEventListener("input", paint);
    root.removeEventListener("change", onChange);
    if (_unmount === dispose) _unmount = null;
  }
  _unmount = dispose;
  observer.observe(document.body, { childList: true, subtree: true });
  root.addEventListener("click", onClick);
  root.addEventListener("input", paint);
  root.addEventListener("change", onChange);
  syncTailoring();
  paint();
  return dispose;
}
