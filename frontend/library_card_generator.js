// Character Library card generator: each mounted panel owns its request.
import { responseError, sseError } from "./errors.js";
import { SPARKLE_ICON } from "./icons.js";
import { sseEvents, streamPost } from "./sse.js";
import { validate } from "./validate.js";

let _unmount = null;

// The model copy is a capped JPEG every vision endpoint decodes; the avatar copy matches the crop modal's PNG cap.
const MODEL_IMAGE_EDGE = 1568;
const AVATAR_EDGE = 1024;

async function encodeImage(bitmap, edge, mime) {
  const scale = Math.min(1, edge / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const ctx = canvas.getContext("2d");
  if (mime === "image/jpeg") {
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return { b64: canvas.toDataURL(mime, 0.9).split(",")[1], mime };
}

async function readReference(file) {
  const check = validate.validateImageFile(file);
  if (!check.valid) throw new Error(check.error);
  const bitmap = await createImageBitmap(file).catch(() => {
    throw new Error("That image could not be read.");
  });
  try {
    return {
      model: await encodeImage(bitmap, MODEL_IMAGE_EDGE, "image/jpeg"),
      avatar: await encodeImage(bitmap, AVATAR_EDGE, "image/png"),
      filename: file.name,
    };
  } finally {
    bitmap.close();
  }
}

function imageFile(dataTransfer) {
  return Array.from(dataTransfer?.files || []).find((file) => file.type.startsWith("image/")) || null;
}

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
        <div class="lib-manager-field">
          <label>Reference image</label>
          <div class="cardgen-image">
            <div class="cardgen-image-preview" data-cardgen-image-preview hidden>
              <img class="cardgen-image-thumb" alt="Reference image">
            </div>
            <div class="cardgen-image-controls">
              <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" data-cardgen-image-input>
              <button class="btn btn-sm" data-cardgen-action="remove-image" hidden>Clear</button>
            </div>
          </div>
        </div>
        <label class="lib-manager-toggle" data-cardgen-avatar-row hidden>
          <input type="checkbox" data-cardgen-avatar checked>
          <span class="lib-manager-toggle-text">
            <span class="lib-manager-toggle-label">Use as the draft's avatar</span>
          </span>
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
  const imageInput = root.querySelector("[data-cardgen-image-input]");
  const imageClear = root.querySelector('[data-cardgen-action="remove-image"]');
  const imagePreview = root.querySelector("[data-cardgen-image-preview]");
  const avatarRow = root.querySelector("[data-cardgen-avatar-row]");
  const useAvatar = root.querySelector("[data-cardgen-avatar]");
  let reference = null;

  function status(message, { error = false } = {}) {
    progress.textContent = message;
    progress.classList.toggle("is-error", error);
  }

  // Deep mode locks thinking on and restores the user's choice when it ends.
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

  function paintReference() {
    imagePreview.hidden = imageClear.hidden = avatarRow.hidden = !reference;
    imagePreview.querySelector("img").src = reference
      ? `data:${reference.avatar.mime};base64,${reference.avatar.b64}`
      : "";
  }

  let pending = 0;
  async function attach(file) {
    if (!file || controller) return;
    const ticket = ++pending;
    try {
      const read = await readReference(file);
      if (ticket !== pending || !root.isConnected) return;
      reference = read;
      status("");
    } catch (error) {
      if (ticket === pending) status(error.message, { error: true });
    }
    paintReference();
    paint();
  }

  function paint() {
    const busy = !!controller;
    generate.disabled = busy || (!idea.value.trim() && !reference);
    generate.textContent = busy ? "Generating…" : "Generate";
    cancel.hidden = !busy;
    idea.disabled = tailoring.disabled = imageInput.disabled = imageClear.disabled = useAvatar.disabled = busy;
    reasoning.disabled = !!controller || tailoring.value === "deep";
    root.classList.toggle("ml-busy", !!controller);
  }

  function onChange(event) {
    if (event.target === tailoring) syncTailoring();
    if (event.target === imageInput) {
      attach(imageInput.files?.[0]);
      imageInput.value = "";
    }
    paint();
  }

  function onPaste(event) {
    const file = imageFile(event.clipboardData);
    if (!file) return;
    event.preventDefault();
    attach(file);
  }

  function onDragOver(event) {
    if (controller || !Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault();
  }

  function onDrop(event) {
    const file = imageFile(event.dataTransfer);
    if (!file) return;
    event.preventDefault();
    attach(file);
  }

  async function run() {
    if (controller || !root.isConnected || (!idea.value.trim() && !reference)) return;
    const runController = new AbortController();
    controller = runController;
    const sent = reference;
    paint();
    status("");
    try {
      const response = await streamPost(
        "/library/card-generator/run",
        {
          idea: idea.value.trim(),
          ...(sent && { image: { ...sent.model, filename: sent.filename } }),
          reasoning: reasoning.checked,
          tailoring: tailoring.value,
        },
        runController.signal,
      );
      if (!response.ok) throw await responseError(response);
      let finished = false;
      for await (const { event, data } of sseEvents(response.body, { signal: runController.signal })) {
        if (runController.signal.aborted || !root.isConnected) return;
        if (event === "progress") status(JSON.parse(data).label);
        else if (event === "error") throw sseError(data);
        else if (event === "done") {
          finished = true;
          status("Draft ready to review.");
          const card = JSON.parse(data).card;
          if (sent && useAvatar.checked)
            Object.assign(card, { avatar_b64: sent.avatar.b64, avatar_mime: sent.avatar.mime });
          await callbacks.onOpenDraft?.(card);
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
    else if (action === "remove-image" && !controller) {
      pending++;
      reference = null;
      paintReference();
      paint();
    }
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
    root.removeEventListener("paste", onPaste);
    root.removeEventListener("dragover", onDragOver);
    root.removeEventListener("drop", onDrop);
    if (_unmount === dispose) _unmount = null;
  }
  _unmount = dispose;
  observer.observe(document.body, { childList: true, subtree: true });
  root.addEventListener("click", onClick);
  root.addEventListener("input", paint);
  root.addEventListener("change", onChange);
  root.addEventListener("paste", onPaste);
  root.addEventListener("dragover", onDragOver);
  root.addEventListener("drop", onDrop);
  syncTailoring();
  paint();
  return dispose;
}
