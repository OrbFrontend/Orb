// The string-payload codec shared by stream consumers and legacy SSE errors.
export function unescapeSSE(data) {
  return data.replace(/\\([\\nr])/g, (_, escaped) => ({ n: "\n", r: "\r", "\\": "\\" })[escaped]);
}
