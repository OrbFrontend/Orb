export function modelPickerState(discovered, current = "") {
  const models = [];
  const seen = new Set();
  if (Array.isArray(discovered)) {
    for (const candidate of discovered) {
      if (typeof candidate !== "string" || !candidate || seen.has(candidate)) continue;
      seen.add(candidate);
      models.push(candidate);
    }
  }

  return {
    kind: models.length ? "select" : "input",
    models,
    current: typeof current === "string" ? current : "",
  };
}
