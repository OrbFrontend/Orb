import { compileCardScriptPattern } from "./card_scripts.js";
import { CHEVRON_DOWN_ICON, CHEVRON_RIGHT_ICON, CHEVRON_UP_ICON, CLOSE_ICON } from "./icons.js";
import { esc, escAttr } from "./utils.js";

const KNOWN_FIELDS = new Set([
  "id",
  "scriptName",
  "findRegex",
  "replaceString",
  "disabled",
  "placement",
  "markdownOnly",
  "promptOnly",
]);
const isRecord = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const textValue = (value) => (typeof value === "string" ? value : "");
const scope = (script) => {
  if (script.markdownOnly && !script.promptOnly) return "display";
  if (script.promptOnly && !script.markdownOnly) return "prompt";
  return "both";
};

function scriptId() {
  // randomUUID is unavailable when Orb runs over plain HTTP.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/* Report invalid or unsupported imported values not already visible in controls. */
function warningFor(script, index) {
  if (!isRecord(script)) return "Unrecognized declaration; preserved unchanged unless removed.";
  const warnings = [];
  const find = script.findRegex;
  if (index >= 50) warnings.push("Only the first 50 entries run.");
  if (find != null && typeof find !== "string") {
    warnings.push("Search pattern must be text; this entry is skipped.");
  } else if (typeof find === "string" && find.length > 4096) {
    warnings.push("Patterns longer than 4,096 characters are skipped.");
  } else if (find) {
    try {
      if (!compileCardScriptPattern(find)) warnings.push("Unsupported flags; use g, i, m, s or u.");
    } catch {
      warnings.push("Invalid display regex; check the search pattern.");
    }
  }
  if (script.replaceString != null && typeof script.replaceString !== "string") {
    warnings.push("Replacement must be text; this entry is skipped.");
  }
  return warnings.join(" ");
}

const removesMatches = (script) => (script.replaceString ?? "") === "";
const scopeLabel = (script) => (removesMatches(script) ? "Remove matches from" : "Replace matches in");

function noteFor(script) {
  if (script.disabled) return "This script is disabled, so it never runs.";
  if (!Array.isArray(script.placement) || !script.placement.some((p) => p === 1 || p === 2)) {
    return "No message type is selected, so this script never runs.";
  }
  const action = removesMatches(script) ? "Removes matching text from" : "Replaces matching text in";
  const notes = {
    display: `${action} the chat display. This script leaves the model prompt unchanged.`,
    prompt: `${action} the model prompt. This script leaves the chat display unchanged.`,
    both: `${action} both the chat display and the model prompt.`,
  };
  return notes[scope(script)];
}

/* A collapsed row stands in for its own body, so it has to say what the script
   searches for, what it leaves behind, and which copy of which messages it
   touches -- everything the open row spells out in controls. */
const SCOPE_SUMMARIES = { display: "Chat display", prompt: "Model prompt", both: "Display and prompt" };

function metaSummary(script) {
  const placement = Array.isArray(script.placement) ? script.placement : [];
  const authors = [
    [1, "User"],
    [2, "Assistant"],
  ]
    .filter(([value]) => placement.includes(value))
    .map(([, label]) => label);
  return `${authors.join(" and ") || "No authors"} · ${SCOPE_SUMMARIES[scope(script)]}`;
}

function patternSummary(script) {
  const oneLine = (value) => textValue(value).replace(/\s+/g, " ").trim();
  const find = oneLine(script.findRegex);
  if (!find) return "No search pattern";
  return `${find} → ${oneLine(script.replaceString) || "(removed)"}`;
}

const nameOf = (script) => textValue(script.scriptName).trim();

function rowHtml(script, index, count, open) {
  const valid = isRecord(script);
  const extras = valid ? Object.fromEntries(Object.entries(script).filter(([key]) => !KNOWN_FIELDS.has(key))) : script;
  const id = `ce-script-${index}`;
  const preserved =
    !valid || Object.keys(extras).length
      ? `<details class="ce-scripts">
      <summary>${CHEVRON_RIGHT_ICON}<span>Preserved imported data</span></summary>
      <p class="modal-hint">Kept on save and export. Depth limits, trim strings, macro substitution in search patterns, run-on-edit and other message targets are not applied by Orb.</p>
      <pre class="ce-scripts-json">${esc(JSON.stringify(extras, null, 2))}</pre>
    </details>`
      : "";
  const body = valid
    ? `<div class="ce-script-body" id="${id}-body">
    <div class="field"><label for="${id}-name">Script name</label>
      <input id="${id}-name" data-script-field="scriptName" value="${escAttr(textValue(script.scriptName))}" placeholder="Untitled script">
    </div>
    <div class="field"><label for="${id}-find">Find regex</label>
      <textarea id="${id}-find" data-script-field="findRegex" rows="2" spellcheck="false" placeholder="/pattern/g">\n${esc(textValue(script.findRegex))}</textarea>
    </div>
    <div class="field"><label for="${id}-replace">Replace with</label>
      <textarea id="${id}-replace" data-script-field="replaceString" rows="3" spellcheck="false" placeholder="Leave empty to remove matches">\n${esc(textValue(script.replaceString))}</textarea>
    </div>
    <div class="ce-script-options">
      <fieldset><legend>Message authors</legend>
        <div class="ce-script-checks">
          <label><input type="checkbox" data-script-field="placement" value="1" ${Array.isArray(script.placement) && script.placement.includes(1) ? "checked" : ""}> User</label>
          <label><input type="checkbox" data-script-field="placement" value="2" ${Array.isArray(script.placement) && script.placement.includes(2) ? "checked" : ""}> Assistant</label>
        </div>
      </fieldset>
      <div class="field"><label for="${id}-scope" data-script-scope-label>${scopeLabel(script)}</label>
        <select id="${id}-scope" data-script-field="scope" aria-describedby="${id}-note">
          ${[
            ["display", "Chat display"],
            ["prompt", "Model prompt"],
            ["both", "Chat display and model prompt"],
          ]
            .map(
              ([value, label]) =>
                `<option value="${value}" ${scope(script) === value ? "selected" : ""}>${label}</option>`,
            )
            .join("")}
        </select>
      </div>
    </div>
    <p id="${id}-note" class="ce-script-note">${esc(noteFor(script))}</p>
    ${preserved}
  </div>`
    : preserved;
  const disabled = valid && Boolean(script.disabled);
  return `<section class="ce-script-row${disabled ? " is-disabled" : ""}${valid && !open ? " is-collapsed" : ""}" data-script-index="${index}" aria-label="Script ${index + 1}">
    <div class="ce-script-toolbar">
      ${
        valid
          ? `<button type="button" class="ce-script-toggle" data-script-action="toggle" aria-expanded="${open}" aria-controls="${id}-body">
        <span class="ce-script-chevron">${CHEVRON_RIGHT_ICON}</span>
        <span class="ce-script-title">Script ${index + 1}</span>
        <span class="ce-script-label" data-script-label>${esc(nameOf(script))}</span>
      </button>`
          : `<span class="ce-script-title">Script ${index + 1}</span>`
      }
      ${valid ? `<label class="ce-script-enabled"><input type="checkbox" data-script-field="enabled" ${disabled ? "" : "checked"}> <span>Enabled</span></label>` : ""}
      <div class="ce-script-actions">
        <button type="button" class="btn btn-sm btn-square" data-script-action="up" title="Move up" aria-label="Move script ${index + 1} up" ${index === 0 ? "disabled" : ""}>${CHEVRON_UP_ICON}</button>
        <button type="button" class="btn btn-sm btn-square" data-script-action="down" title="Move down" aria-label="Move script ${index + 1} down" ${index === count - 1 ? "disabled" : ""}>${CHEVRON_DOWN_ICON}</button>
        <button type="button" class="btn btn-sm btn-square" data-script-action="remove" title="Remove" aria-label="Remove script ${index + 1}">${CLOSE_ICON}</button>
      </div>
    </div>
    ${valid ? `<p class="ce-script-summary"><code data-script-summary>${esc(patternSummary(script))}</code><span class="ce-script-meta" data-script-meta>${esc(metaSummary(script))}</span></p>` : ""}
    ${body}
    <p class="ce-script-warning" role="status">${esc(warningFor(script, index))}</p>
  </section>`;
}

/** Mount the editor and return a reader for its current draft. */
export function mountCardScriptsEditor(root, original) {
  const scripts = Array.isArray(original) ? structuredClone(original) : [];
  // Keyed by the draft object, so an open row stays open where reordering moves it.
  const expanded = new WeakSet();
  let changed = false;
  root.classList.add("ce-scripts-editor");
  function render() {
    root.innerHTML = `<div class="ce-script-list">${scripts.map((script, i) => rowHtml(script, i, scripts.length, expanded.has(script))).join("") || '<p class="ce-scripts-empty">This card carries no scripts.</p>'}</div>
      <div class="ce-script-footer">
        <button type="button" class="btn btn-sm" data-script-action="add">+ Add script</button>
        <p class="modal-hint">Use /pattern/g to replace every match. Replacements support $1, $2, $&lt;name&gt; and {{match}}. Changes take effect when you save the card.</p>
      </div>`;
  }
  const actions = new Map([
    [
      "add",
      () => {
        const script = {
          id: scriptId(),
          scriptName: "",
          findRegex: "",
          replaceString: "",
          placement: [2],
          disabled: false,
          markdownOnly: true,
          promptOnly: false,
        };
        scripts.push(script);
        expanded.add(script); // An empty row has nothing to preview, and is there to be filled in.
        return scripts.length - 1;
      },
    ],
    [
      "remove",
      (index) => {
        scripts.splice(index, 1);
        return Math.min(index, scripts.length - 1);
      },
    ],
    [
      "up",
      (index) => {
        if (index > 0) [scripts[index - 1], scripts[index]] = [scripts[index], scripts[index - 1]];
        return Math.max(0, index - 1);
      },
    ],
    [
      "down",
      (index) => {
        if (index < scripts.length - 1) [scripts[index + 1], scripts[index]] = [scripts[index], scripts[index + 1]];
        return Math.min(scripts.length - 1, index + 1);
      },
    ],
  ]);
  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-script-action]");
    if (!button || button.disabled) return;
    const row = button.closest("[data-script-index]");
    const index = Number(row?.dataset.scriptIndex);
    // Opening a row only reveals markup the row already carries, so it needs no re-render.
    if (button.dataset.scriptAction === "toggle") {
      const open = row.classList.contains("is-collapsed");
      if (open) expanded.add(scripts[index]);
      else expanded.delete(scripts[index]);
      row.classList.toggle("is-collapsed", !open);
      button.setAttribute("aria-expanded", String(open));
      return;
    }
    const action = actions.get(button.dataset.scriptAction);
    if (!action) return;
    const nextIndex = action(index);
    changed = true;
    render();
    const nextRow = root.querySelector(`[data-script-index="${nextIndex}"]`);
    const focusTarget =
      button.dataset.scriptAction === "add"
        ? nextRow?.querySelector('[data-script-field="scriptName"]')
        : nextRow?.querySelector(`[data-script-action="${button.dataset.scriptAction}"]:not(:disabled)`);
    (focusTarget || nextRow?.querySelector("input") || root.querySelector('[data-script-action="add"]'))?.focus();
  });
  function updateField(event) {
    const input = event.target.closest("[data-script-field]");
    if (!input) return;
    const row = input.closest("[data-script-index]");
    const index = Number(row.dataset.scriptIndex);
    const script = scripts[index];
    const field = input.dataset.scriptField;
    if (field === "enabled") {
      script.disabled = !input.checked;
      row.classList.toggle("is-disabled", script.disabled);
    } else if (field === "scope") {
      script.markdownOnly = input.value !== "prompt";
      script.promptOnly = input.value !== "display";
    } else if (field === "placement") {
      const target = Number(input.value);
      const placement = Array.isArray(script.placement) ? script.placement : [];
      script.placement = placement.filter((p) => p !== target);
      if (input.checked) script.placement.push(target);
    } else script[field] = input.value;
    changed = true;
    row.querySelector(".ce-script-warning").textContent = warningFor(script, index);
    row.querySelector("[data-script-scope-label]").textContent = scopeLabel(script);
    row.querySelector(".ce-script-note").textContent = noteFor(script);
    row.querySelector("[data-script-label]").textContent = nameOf(script);
    row.querySelector("[data-script-summary]").textContent = patternSummary(script);
    row.querySelector("[data-script-meta]").textContent = metaSummary(script);
  }
  root.addEventListener("input", updateField);
  root.addEventListener("change", updateField);
  render();
  return () => structuredClone(changed ? scripts : original);
}
