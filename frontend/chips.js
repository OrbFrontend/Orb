// One chip-input widget. A chip field renders an array of short strings as
// removable pills followed by a text input; typing then Enter or comma adds an
// item, Backspace on an empty input removes the last, and a per-item × button
// removes it. The lorebook keyword editor and the character-tag editor each
// carried a byte-for-byte copy of this (down to the shared `lb-chip*` CSS); this
// is the single implementation they both drive.
//
// State-agnostic: the caller owns the array via getItems/setItems and supplies an
// optional onChange side effect (e.g. mark-dirty). The widget attaches its own DOM
// listeners inside render(), so callers no longer need window-bridged inline
// handlers — call render() wherever the wrap element is (re)created.

import { esc } from "./utils.js";

export function createChipInput({
  wrapId,
  inputId,
  placeholder = "",
  disabledPlaceholder = "",
  getItems,
  setItems,
  onChange,
  isDisabled,
}) {
  function commit(next) {
    setItems(next);
    onChange?.();
    render();
    // Preserve typing focus across the innerHTML rebuild.
    setTimeout(() => document.getElementById(inputId)?.focus(), 0);
  }

  // Add the trimmed token (a trailing comma is dropped). Returns false when it is
  // empty or already present, so the input handler can clear a stray comma.
  function addValue(raw) {
    const val = raw.replace(/,$/, "").trim();
    const items = getItems();
    if (!val || items.includes(val)) return false;
    commit([...items, val]);
    return true;
  }

  function onKeydown(e) {
    const input = e.target;
    if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
      e.preventDefault();
      addValue(input.value);
      return;
    }
    if (e.key === "Backspace" && !input.value && getItems().length) {
      commit(getItems().slice(0, -1));
    }
  }

  function onInput(e) {
    // A trailing comma (e.g. from a paste) commits the token, matching keydown.
    if (e.target.value.endsWith(",") && !addValue(e.target.value)) e.target.value = "";
  }

  function render() {
    const wrap = document.getElementById(wrapId);
    if (!wrap) return;
    const items = getItems();
    const disabled = isDisabled ? isDisabled() : false;
    const chips = items
      .map((c, i) => {
        const rm = disabled ? "" : `<button type="button" class="lb-chip-remove" data-chip-index="${i}">×</button>`;
        return `<span class="lb-chip">${esc(c)}${rm}</span>`;
      })
      .join("");
    const input = disabled
      ? `<span class="lb-chip-placeholder">${items.length ? "" : esc(disabledPlaceholder)}</span>`
      : `<input id="${inputId}" class="lb-chip-text" placeholder="${items.length ? "" : esc(placeholder)}">`;
    wrap.innerHTML = chips + input;
    if (disabled) return;
    for (const btn of wrap.querySelectorAll("[data-chip-index]")) {
      btn.addEventListener("click", () => commit(getItems().filter((_, j) => j !== Number(btn.dataset.chipIndex))));
    }
    const inputEl = document.getElementById(inputId);
    if (inputEl) {
      inputEl.addEventListener("keydown", onKeydown);
      inputEl.addEventListener("input", onInput);
    }
  }

  return { render };
}
