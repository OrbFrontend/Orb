// HTTP and SSE failures share one shape. Only legacy SSE text is newline-escaped;
// JSON payloads must be parsed before any unescaping.
function firstString(value) {
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) return value.map(firstString).filter(Boolean).join("; ");
  if (value && typeof value === "object") return firstString(value.msg ?? value.message ?? value.detail);
  return "";
}

function parseFailure(data, escaped = false) {
  const raw = typeof data === "string" ? data : "";
  let parsed = data;
  if (raw) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      return { headline: escaped ? raw.replace(/\\n/g, "\n") : raw, sentence: "", kind: "internal" };
    }
  }
  if (parsed && typeof parsed === "object") {
    if (typeof parsed.headline === "string" && parsed.headline) {
      return { sentence: "", kind: "internal", ...parsed };
    }
    const sentence = firstString(parsed.detail) || firstString(parsed.error) || firstString(parsed.message);
    return { headline: "", sentence: sentence || raw, kind: "internal", ...(raw && { body: raw }) };
  }
  return { headline: firstString(parsed), sentence: "", kind: "internal" };
}

function asError(failure, fallback) {
  const message = [failure.headline, failure.sentence].filter(Boolean).join(" ") || fallback;
  return Object.assign(new Error(message), { failure });
}

export async function responseError(response) {
  // Let body-read failures (including AbortError) propagate: the response never
  // arrived in full, so it cannot confirm a stream's settlement.
  const body = await response.text();
  return Object.assign(asError(parseFailure(body), `Orb returned HTTP ${response.status}.`), {
    status: response.status,
    body,
  });
}

export function sseError(data, fallback = "Generation failed.") {
  return asError(parseFailure(data, true), fallback);
}
