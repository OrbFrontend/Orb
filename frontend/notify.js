// Notifications: the toast stack and the sticky error entry.
//
// Replaces the `#toast` singleton, which was one <div> and one shared timer for
// 192 call sites: a second toast overwrote the first's text *and* inherited its
// already-running timeout, so the second message could flash for 200ms. Each
// entry here owns its own element and its own timer.
//
// The other half of the fix is that an error is no longer transient. A failure
// the user is expected to act on cannot expire after three seconds, and it has to
// be selectable so it can be pasted into a bug report — so error entries are
// sticky, carry a `×`, and offer Copy.
//
// Imports nothing on purpose. utils.js re-exports `toast` from here so no call
// site moves, and a leaf module cannot participate in that cycle.

const MAX_VISIBLE = 4; // beyond this the oldest is evicted; a wall of toasts is no more legible than one
const TRANSIENT_MS = 3000; // must stay in sync with notifications.css's toastOut delay
const FADE_MS = 200;
const COPIED_MS = 1200; // matches the code-block copy button in utils.js

// Copy *text*, reporting whether it actually happened.
//
// `navigator.clipboard` is undefined outside a secure context, which for a
// self-hosted app is the *normal* case the moment it is opened from another
// device on the LAN over plain http. The old `navigator.clipboard?.writeText(…)`
// silently evaluated to undefined there while the button still said "Copied" --
// worst of both, since the whole point of this text is to be pasted into a bug
// report. execCommand is deprecated but it is what works without TLS.
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {
    // Permission denied, or a document that isn't focused. Fall through.
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Off-screen rather than hidden: execCommand ignores an unrendered element.
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

// Copy, then tell the truth on the button itself and put it back.
//
// `explain` adds a toast naming the cause, for callers that have somewhere to
// send the user (the failed-turn card has a Details pane with selectable text).
// Off by default so an error toast's own Copy button cannot spawn another toast.
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
    // index.html ships the container, but a test harness or a partial page may not.
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
  // Evict without the fade: an entry being pushed out has already lost its slot,
  // and animating it would leave more than MAX_VISIBLE on screen while it faded.
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

// The original signature, frozen by the plugin ABI. `isError` now means "sticky
// and red" rather than "red" (which it never actually was — the class written here
// was `toast-error` while the CSS styled `.toast.error`, so error toasts rendered
// identically to success toasts in every theme).
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

// A failure worth reading: a headline Orb can assert, the provider's own sentence
// under it, and optional Details. Sticky — it goes away when the user says so.
//
// Every string is written with textContent. A provider message is untrusted input
// and must never reach innerHTML.
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

  const close = button("×", "toast-close", () => dismiss(el), { title: "Dismiss" });
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
