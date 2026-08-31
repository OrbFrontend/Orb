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
    setTimeout(() => document.getElementById(inputId)?.focus(), 0);
  }

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
