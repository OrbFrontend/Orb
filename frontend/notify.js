import { CLOSE_ICON } from "./icons.js";

const MAX_VISIBLE = 4; // maximum visible notifications
const TRANSIENT_MS = 3000; // match notifications.css
const FADE_MS = 200;
const COPIED_MS = 1200; // copy feedback duration

export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {}
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-9999px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (_) {
    return false;
  }
}

export async function copyToButton(btn, text, { explain = false } = {}) {
  const ok = await copyText(text);
  const original = btn.dataset.copyLabel || btn.textContent;
  btn.dataset.copyLabel = original;
  btn.textContent = ok ? "Copied" : "Copy failed";
  if (!ok && explain) {
    notifyError("Couldn't reach the clipboard.", {
      sentence: "Copying needs HTTPS or localhost. Open Details and select the text instead.",
    });
  }
  setTimeout(() => {
    btn.textContent = btn.dataset.copyLabel || original;
  }, COPIED_MS);
  return ok;
}

function stack() {
  let el = document.getElementById("toast-stack");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast-stack";
    el.className = "toast-stack";
    document.body.appendChild(el);
  }
  return el;
}

function clearTimer(el) {
  if (el._toastTimer) {
    clearTimeout(el._toastTimer);
    el._toastTimer = null;
  }
}

function dismiss(el) {
  if (!el?.isConnected) return;
  clearTimer(el);
  el.classList.add("toast-leaving");
  setTimeout(() => el.remove(), FADE_MS);
}

function mount(el) {
  const ct = stack();
  ct.appendChild(el);
  while (ct.children.length > MAX_VISIBLE && ct.firstElementChild) {
    clearTimer(ct.firstElementChild);
    ct.firstElementChild.remove();
  }
  return el;
}

function button(label, className, onClick, { title = "" } = {}) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = className;
  b.textContent = label;
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

export function toast(msg, isError = false) {
  const text = msg == null ? "" : String(msg);
  if (isError) return notifyError(text);

  const el = document.createElement("div");
  el.className = "toast toast-transient";
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  el.textContent = text;
  mount(el);
  el._toastTimer = setTimeout(() => dismiss(el), TRANSIENT_MS);
  return el;
}

export function notifyError(headline, { sentence = "", onDetails = null } = {}) {
  const head = String(headline ?? "");
  const detail = String(sentence ?? "");

  const el = document.createElement("div");
  el.className = "toast error";
  el.setAttribute("role", "alert");
  el.setAttribute("aria-live", "assertive");

  const top = document.createElement("div");
  top.className = "toast-head";

  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "⚠";

  const title = document.createElement("span");
  title.className = "toast-headline";
  title.textContent = head;

  const close = button("", "toast-close", () => dismiss(el), { title: "Dismiss" });
  close.innerHTML = CLOSE_ICON;
  close.setAttribute("aria-label", "Dismiss");

  top.append(icon, title, close);
  el.appendChild(top);

  if (detail) {
    const line = document.createElement("div");
    line.className = "toast-sentence";
    line.textContent = detail;
    el.appendChild(line);
  }

  const actions = document.createElement("div");
  actions.className = "toast-actions";
  if (typeof onDetails === "function") {
    actions.appendChild(button("Details", "toast-link", () => onDetails()));
  }
  actions.appendChild(
    button("Copy", "toast-link", (e) => {
      copyToButton(e.currentTarget, detail ? `${head}\n${detail}` : head);
    }),
  );
  el.appendChild(actions);

  return mount(el);
}
