// Character Library card generator: each mounted panel owns its request.
import { EDIT_ICON } from "./icons.js";
import { sseEvents, streamPost, unescapeSSE } from "./sse.js";

let _unmount = null;

export function cardGeneratorToolHtml() {
  return `
    <section class="lib-tool" data-tool="card-generator">
      <header class="lib-tool-head">
        <span class="lib-tool-icon">${EDIT_ICON}</span>
        <div class="lib-tool-heading">
          <h3 class="lib-tool-name">Card Generator</h3>
          <p class="lib-manager-note">Describe a character and let the Agent model draft a card. Review and edit it before saving to your library.</p>
        </div>
      </header>
      <div class="lib-tool-body">
        <label class="field">Character idea
          <textarea data-cardgen-idea rows="4" maxlength="2000" placeholder="A jaded harbour-town fence who owes everyone money…"></textarea>
        </label>
        <label class="lib-manager-toggle">
          <input type="checkbox" data-cardgen-tailored>
          <span class="lib-manager-toggle-text">
            <span class="lib-manager-toggle-label">Tailored to me</span>
            <span class="lib-manager-note">Use your library's tags, persona names, and most-played characters as inspiration.</span>
          </span>
        </label>
        <label class="lib-manager-toggle">
          <input type="checkbox" data-cardgen-reasoning>
          <span class="lib-manager-toggle-text">
            <span class="lib-manager-toggle-label">Enable generator thinking</span>
            <span class="lib-manager-note">Slower and uses more model resources.</span>
          </span>
        </label>
        <div class="lib-manager-actions">
          <button class="btn btn-accent" data-cardgen-action="generate">Generate</button>
          <button class="btn" data-cardgen-action="cancel" hidden>Cancel</button>
        </div>
        <div class="lib-manager-progress" data-cardgen-progress role="status" aria-live="polite" hidden></div>
      </div>
    </section>`;
}

export function mountCardGenerator(root, callbacks = {}) {
  _unmount?.();
  let controller = null;
  const idea = root.querySelector("[data-cardgen-idea]");
  const reasoning = root.querySelector("[data-cardgen-reasoning]");
  const tailored = root.querySelector("[data-cardgen-tailored]");
  const generate = root.querySelector('[data-cardgen-action="generate"]');
  const cancel = root.querySelector('[data-cardgen-action="cancel"]');
  const progress = root.querySelector("[data-cardgen-progress]");

  function status(message) {
    progress.hidden = false;
    progress.textContent = message;
  }

  function paint() {
    generate.disabled = !!controller || !idea.value.trim();
    generate.textContent = controller ? "Generating…" : "Generate";
    cancel.hidden = !controller;
    idea.disabled = reasoning.disabled = tailored.disabled = !!controller;
  }

  async function run() {
    if (controller || !root.isConnected || !idea.value.trim()) return;
    const runController = new AbortController();
    controller = runController;
    paint();
    status("Drafting your character…");
    try {
      const response = await streamPost(
        "/library/card-generator/run",
        { idea: idea.value.trim(), reasoning: reasoning.checked, tailored: tailored.checked },
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
      else if (!finished) status("Generation ended without a draft. Try again.");
    } catch (error) {
      if (root.isConnected) {
        status(runController.signal.aborted ? "Generation cancelled." : error.message);
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
    if (_unmount === dispose) _unmount = null;
  }
  _unmount = dispose;
  observer.observe(document.body, { childList: true, subtree: true });
  root.addEventListener("click", onClick);
  root.addEventListener("input", paint);
  paint();
  return dispose;
}
