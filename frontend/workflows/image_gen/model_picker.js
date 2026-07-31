// Pure state for the image-generation checkpoint control. Keeping this free of
// DOM and workflow imports makes the success/fallback rules directly testable.
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
