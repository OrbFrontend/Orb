import { replacePlaceholders } from "./identity_macros.js";

const MAX_TEXT_LENGTH = 100_000;
const SLOW_MS = 100;
const JS_FLAGS = /^(?!.*?(.).*?\1)[gmixXsuUAJ]+$/;
const ENGINE_FLAGS = /^[gimsu]*$/;
const TOKEN = /\$(?:[$&`']|<[^>]*>|[0-9]{1,2})/g;

/** Compile a JavaScript pattern literal, or the whole string when it is not one. */
export function compileCardScriptPattern(source) {
  let pattern = source;
  let flags = "";
  const end = source.startsWith("/") ? source.lastIndexOf("/") : 0;
  const declared = end > 0 ? source.slice(end + 1) : "";
  if (end > 0 && (!declared || JS_FLAGS.test(declared))) {
    pattern = source.slice(1, end);
    flags = declared;
  }
  return ENGINE_FLAGS.test(flags) ? new RegExp(pattern, flags) : null;
}

/** Normalize only applicable declarations, keeping irrelevant card metadata out of the worker and cache key. */
export function displayScripts(scripts, role) {
  const placement = { user: 1, assistant: 2 }[role];
  if (!placement || !Array.isArray(scripts)) return [];
  return scripts
    .slice(0, 50)
    .filter(
      (script) =>
        script &&
        !script.disabled &&
        Array.isArray(script.placement) &&
        script.placement.includes(placement) &&
        (!script.promptOnly || script.markdownOnly) &&
        typeof script.findRegex === "string" &&
        script.findRegex.length > 0 &&
        script.findRegex.length <= 4096 &&
        typeof (script.replaceString ?? "") === "string",
    )
    .map(({ findRegex, replaceString }) => ({ findRegex, replaceString: replaceString ?? "", placement: [placement] }));
}

function expandReplacement(template, captures, groups, offset, source, names) {
  const expanded = template.replace(/\{\{match\}\}/gi, "$0").replace(TOKEN, (token) => {
    const key = token.slice(1);
    if (key === "$") return "$";
    if (key === "&") return captures[0];
    if (key === "`") return source.slice(0, offset);
    if (key === "'") return source.slice(offset + captures[0].length);
    if (key.startsWith("<")) return groups?.[key.slice(1, -1)] ?? "";
    const index = Number(key);
    if (index === 0) return captures[0];
    if (index > 0 && index < captures.length) return captures[index] ?? "";
    if (key.length === 2 && Number(key[0]) > 0 && Number(key[0]) < captures.length)
      return (captures[Number(key[0])] ?? "") + key[1];
    return token;
  });
  return expanded.includes("{{") ? replacePlaceholders(expanded, ...names) : expanded;
}

/** Pure projection rules; the browser calls these only inside the disposable worker. */
export function projectCardScripts({ text, scripts, role, names = [] }, running = () => {}) {
  const original = text;
  const disabled = [];
  if (text.length > MAX_TEXT_LENGTH) return { text, disabled };
  for (const script of displayScripts(scripts, role)) {
    try {
      const pattern = compileCardScriptPattern(script.findRegex);
      if (!pattern) continue;
      const key = pattern.toString();
      running(key);
      const start = performance.now();
      let size = 0,
        end = 0;
      const projected = text.replace(pattern, (...args) => {
        const named = typeof args.at(-1) === "object" ? args.at(-1) : undefined;
        const tail = named ? 3 : 2;
        const offset = args.at(-tail);
        const value = expandReplacement(
          script.replaceString,
          args.slice(0, -tail),
          named,
          offset,
          args.at(-tail + 1),
          names,
        );
        size += offset - end + value.length;
        end = offset + args[0].length;
        if (size > MAX_TEXT_LENGTH) throw new RangeError("card projection output limit");
        return value;
      });
      if (performance.now() - start > SLOW_MS) disabled.push(key);
      else text = projected;
    } catch (error) {
      if (error instanceof RangeError) return { text: original, disabled };
      console.warn("Ignoring invalid or unsupported card regex");
    }
    if (text.length > MAX_TEXT_LENGTH) return { text: original, disabled };
  }
  return { text, disabled };
}

if (typeof WorkerGlobalScope !== "undefined" && self instanceof WorkerGlobalScope) {
  self.addEventListener("message", ({ data }) => {
    self.postMessage(projectCardScripts(data, (running) => self.postMessage({ running })));
  });
}
