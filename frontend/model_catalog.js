export function mergeModelChoices(configs, availableModels) {
  const choices = [];
  const seen = new Set();

  for (const config of Array.isArray(configs) ? configs : []) {
    const value = typeof config?.model_name === "string" ? config.model_name.trim() : "";
    if (!value || seen.has(value)) continue;
    seen.add(value);
    choices.push({ value, id: config.id, type: "model" });
  }

  for (const raw of Array.isArray(availableModels) ? availableModels : []) {
    const value = typeof raw === "string" ? raw.trim() : "";
    if (!value || seen.has(value)) continue;
    seen.add(value);
    choices.push({ value, type: "available" });
  }

  return choices;
}

export function filterModelChoices(choices, query) {
  const terms = normalizeSearchValue(query).match(/[\p{L}\p{N}]+/gu) ?? [];
  if (!terms.length) return choices;
  return choices.filter((item) => {
    const haystack = compactSearchValue(normalizeSearchValue(item.value));
    return terms.every((term) => haystack.includes(term));
  });
}

function normalizeSearchValue(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase();
}

function compactSearchValue(value) {
  return value.replace(/[^\p{L}\p{N}]+/gu, "");
}
