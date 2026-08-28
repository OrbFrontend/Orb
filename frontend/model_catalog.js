// Pure model-picker helpers. Discovery is intentionally separate from saved
// model configs: choosing a discovered model promotes it through the existing
// settings save path, while a saved config keeps its delete affordance.

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
  const needle = String(query || "")
    .trim()
    .toLocaleLowerCase();
  if (!needle) return choices;
  const compactNeedle = compactSearchValue(needle);
  return choices.filter((item) => {
    const haystack = item.value.toLocaleLowerCase();
    return haystack.includes(needle) || (compactNeedle && compactSearchValue(haystack).includes(compactNeedle));
  });
}

function compactSearchValue(value) {
  // Model ids use separators inconsistently across providers. Treat spaces,
  // punctuation, and path delimiters as equivalent while retaining letters
  // and numbers from every script. NFKC also folds full-width forms.
  return value.normalize("NFKC").replace(/[^\p{L}\p{N}]+/gu, "");
}
