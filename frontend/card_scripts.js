import { compileCardScriptPattern, displayScripts } from "./card_script_worker.js";
import { toast } from "./notify.js";
import { charactersView, S } from "./state.js";
import { placeholderNames, resolvePlaceholders } from "./utils.js";

export { compileCardScriptPattern } from "./card_script_worker.js";

const OFF_KEY = "orb.cardScriptOff.v1";
const DEADLINE_MS = 2000;
const MAX_WORKERS = 2;
const queuedJobs = [];
const activeJobs = new Set();
const jobs = new Set();
let pumpPending = false;
let guard;
let disabled;
let projections;
const recent = new Map();
let repaint = () => {};

export function setCardScriptRepaint(fn) {
  repaint = fn;
}

/** Replace the projector/storage/announcer in tests; production always executes in a worker. */
export function configureCardScriptGuard(options = {}) {
  for (const job of jobs) job.cancel();
  let storage = null;
  try {
    storage = globalThis.localStorage ?? null;
  } catch {}
  guard = { project: projectInWorker, storage, announce: (message) => toast(message, true), ...options };
  projections = new WeakMap();
  recent.clear();
  try {
    disabled = new Set(JSON.parse(guard.storage?.getItem(OFF_KEY) ?? "[]"));
  } catch {
    disabled = new Set();
  }
}

export function cardScriptTurnedOff(source) {
  try {
    return disabled.has(compileCardScriptPattern(source)?.toString());
  } catch {
    return false;
  }
}

function turnOff(source) {
  if (disabled.has(source)) return;
  disabled.add(source);
  try {
    guard.storage?.setItem(OFF_KEY, JSON.stringify([...disabled].slice(-512)));
  } catch {}
  guard.announce("Turned off a card display script on this device because it took too long to render.");
}

function scheduleWorkers() {
  if (pumpPending) return;
  pumpPending = true;
  queueMicrotask(() => {
    pumpPending = false;
    while (activeJobs.size < MAX_WORKERS && queuedJobs.length) {
      const task = queuedJobs.shift();
      activeJobs.add(task);
      task.start();
    }
  });
}

/** Bound worker concurrency; the timer still terminates patterns that hang on later inputs. */
function projectInWorker(job) {
  let resolve;
  const result = new Promise((done) => {
    resolve = done;
  });
  let worker;
  let deadline;
  let running;
  let finished = false;
  const finish = (projection) => {
    if (finished) return;
    finished = true;
    clearTimeout(deadline);
    worker?.terminate();
    activeJobs.delete(task);
    jobs.delete(task);
    const index = queuedJobs.indexOf(task);
    if (index !== -1) queuedJobs.splice(index, 1);
    resolve(projection);
    scheduleWorkers();
  };
  const task = {
    cancel: () => finish({ text: job.text }),
    start() {
      const scripts = job.scripts.filter((script) => !cardScriptTurnedOff(script.findRegex));
      if (!scripts.length) return finish({ text: job.text });
      try {
        worker = new Worker(new URL("./card_script_worker.js", import.meta.url), { type: "module" });
        deadline = setTimeout(() => finish({ text: job.text, disabled: running ? [running] : [] }), DEADLINE_MS);
        worker.addEventListener("message", ({ data }) => {
          if (finished) return;
          if (data.running) running = data.running;
          else finish(data);
        });
        worker.addEventListener("error", () => finish({ text: job.text }));
        worker.postMessage({ ...job, scripts });
      } catch {
        finish({ text: job.text });
      }
    },
  };
  jobs.add(task);
  queuedJobs.push(task);
  result.cancel = task.cancel;
  scheduleWorkers();
  return result;
}

/** Show canonical text while pending, then repaint from a result keyed by the exact input and identity context. */
export function applyCardScripts(text, scripts, role, owner = null) {
  if (text.length > 100_000) return text;
  scripts = displayScripts(scripts, role).filter((script) => !cardScriptTurnedOff(script.findRegex));
  if (!scripts.length) return text;
  const names = placeholderNames();
  const key = JSON.stringify([text, scripts, role, names]);
  // Each saved row/live bubble keeps a few inputs (including an editor-diff baseline) without retaining discarded rows.
  const cache = owner ? (projections.get(owner) ?? new Map()) : recent;
  if (owner) projections.set(owner, cache);
  const cached = cache.get(key);
  if (cached) return cached.text;
  const entry = { text };
  cache.set(key, entry);
  if (cache.size > (owner ? 4 : 128)) {
    const oldest = cache.keys().next().value;
    const evicted = cache.get(oldest);
    cache.delete(oldest);
    evicted.cancel?.();
  }
  const currentGuard = guard;
  const done = (result) => {
    delete entry.cancel;
    if (guard !== currentGuard) return;
    for (const source of result.disabled ?? []) turnOff(source);
    if (cache.get(key) !== entry) return;
    entry.text = result.text;
    if (entry.text !== text || result.disabled?.length) repaint();
  };
  const result = guard.project({ text, scripts, role, names });
  if (result instanceof Promise) {
    entry.cancel = () => result.cancel?.();
    result.then(done, () => done({ text }));
  } else done(result);
  return entry.text;
}

configureCardScriptGuard();

const STYLE_ELEMENT_RE = /<style\b[^>]*>([\s\S]*?)(?:<\/style\s*>|$)/gi;

function stylesheetText(css) {
  if (!/<style\b/i.test(css)) return css;
  return Array.from(css.matchAll(STYLE_ELEMENT_RE), (match) => match[1]).join("\n");
}

/** Project card CSS through the existing message sanitizer and scope. */
export function projectCardDisplay(text, card, role, owner = null) {
  text = applyCardScripts(text, card?.display_scripts, role, owner);
  const css = typeof card?.display_css === "string" ? stylesheetText(card.display_css) : "";
  if (role === "assistant" && css.trim()) {
    // Keep card CSS from terminating the injected style element.
    text = `<style>${css.replace(/<\/style/gi, "<\\/style")}</style>\n${text}`;
  }
  return text;
}

export function messageDisplaySource(message) {
  const conv = S.conversations?.find((c) => c.id === S.activeConvId);
  const cardId = S.groupCast
    ? (S.groupCast.speakerCardIds?.get(message.speaker_member_id) ??
      S.groupCast.members?.find((m) => m.id === message.speaker_member_id)?.character_card_id)
    : conv?.character_card_id;
  const card = cardId ? charactersView().find((c) => c.id === cardId) : null;
  return projectCardDisplay(
    resolvePlaceholders(message.content || ""),
    card,
    message.role,
    message.id ? S.messages?.find((row) => row.id === message.id) : S.streamingBodyEl,
  );
}
