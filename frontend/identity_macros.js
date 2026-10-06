/** Resolve identity macros literally, leaving inline code alone. */
export function replacePlaceholders(text, userName, charName, cast = "") {
  if (!text || typeof text !== "string") return text || "";
  for (const [pattern, value] of [
    [/\{\{user\}\}/gi, userName],
    [/\{\{char\}\}/gi, charName],
    [/\{\{cast\}\}/gi, cast],
  ]) {
    if (value)
      text = text
        .split(/(`[^`\n]*`)/)
        .map((piece, index) => (index % 2 ? piece : piece.replace(pattern, () => value)))
        .join("");
  }
  return text;
}
