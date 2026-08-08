// The failed-turn card: what a generation failure leaves behind once the toast
// is gone.
//
// A grey chip reading "Error: Generation failed; see server logs" that fades in
// three seconds is worse than nothing in a self-hosted app, where the user *is*
// the operator and the provider's actual sentence — "Reasoning is mandatory for
// this endpoint and cannot be disabled." — is one settings-panel edit away from a
// fix. `S.turnError` (set by chat_stream.js from the backend's `describe_failure`
// payload) is painted here as a persistent card at the tail of the chat: the
// provider's words inline and the full raw body behind Details. The card only
// reports — rerunning the turn is the regenerate button's job, on the tail
// message itself, where the user can see what they are about to replace.
//
// Painted from renderMessages() rather than from the stream handler, because
// afterStream() unconditionally refetches and repaints — a card drawn by the
// handler would be wiped a moment later.
import { closeModal, showModal, switchTab } from "./modal.js";
import { copyToButton } from "./notify.js";
import { S } from "./state.js";
import { $, esc } from "./utils.js";

const CARD_ID = "turn-error-card";

// The failure object this card has already announced to assistive tech. Compared
// by identity, never read for content.
let _announced = null;

function meta(err) {
  const bits = [];
  if (err.stage) bits.push(err.stage);
  if (err.status) bits.push(`HTTP ${err.status}`);
  if (err.host) bits.push(err.host);
  if (err.model) bits.push(err.model);
  return bits;
}

// A failure card belongs to the conversation it happened in. Without this guard a
// switch to another chat would leave the previous chat's failure hanging under
// someone else's messages.
function activeError() {
  const err = S.turnError;
  if (!err) return null;
  if (err.convId != null && err.convId !== S.activeConvId) return null;
  return err;
}

export function clearTurnError() {
  S.turnError = null;
  _announced = null;
  document.getElementById(CARD_ID)?.remove();
}

function prettyBody(body) {
  try {
    return JSON.stringify(JSON.parse(body), null, 2);
  } catch (_) {
    return body;
  }
}

function copyReport(err) {
  const lines = [err.headline || "Generation failed."];
  if (err.sentence) lines.push("", err.sentence);
  const bits = meta(err);
  if (err.at) bits.push(new Date(err.at).toISOString());
  if (bits.length) lines.push("", bits.join(" · "));
  if (err.body) lines.push("", "```", prettyBody(err.body), "```");
  return lines.join("\n");
}

// showModal takes an HTML string, so every interpolated value goes through esc():
// a provider response body is untrusted input.
function showDetails(err) {
  // err.at, not Date.now(): this pane is read minutes after the fact, and
  // stamping it on open reported the time the user clicked Details as the time
  // the turn failed. Older entries predate the field, hence the fallback.
  const when = err.at ? new Date(err.at).toLocaleString() : "";
  const rows = [
    ["When", when],
    ["Stage", err.stage],
    ["HTTP status", err.status],
    ["Host", err.host],
    ["Model", err.model],
  ]
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<div class="turn-error-row"><span>${esc(k)}</span><span>${esc(String(v))}</span></div>`)
    .join("");
  const body = err.body
    ? `<pre class="turn-error-raw">${esc(prettyBody(err.body))}</pre>`
    : `<div class="turn-error-empty">The endpoint sent no response body. This failure came from inside Orb — the server log has the traceback.</div>`;

  showModal(`
    <h2>Generation failed</h2>
    <div class="tabs">
      <div class="tab active" id="turn-error-tab-summary">Summary</div>
      <div class="tab" id="turn-error-tab-raw">Provider response</div>
    </div>
    <div class="tab-content active" id="turn-error-pane-summary">
      <div class="turn-error-headline">${esc(err.headline || "Generation failed.")}</div>
      ${err.sentence ? `<div class="turn-error-sentence">${esc(err.sentence)}</div>` : ""}
      <div class="turn-error-rows">${rows}</div>
    </div>
    <div class="tab-content" id="turn-error-pane-raw">${body}</div>
    <div class="modal-actions">
      <button class="btn btn-sm" id="turn-error-copy">Copy</button>
      <button class="btn btn-sm" id="turn-error-close">Close</button>
    </div>`);

  const summary = $("turn-error-tab-summary");
  const raw = $("turn-error-tab-raw");
  summary.addEventListener("click", () => switchTab(summary, "turn-error-pane-summary"));
  raw.addEventListener("click", () => switchTab(raw, "turn-error-pane-raw"));
  $("turn-error-copy").addEventListener("click", (e) => {
    copyToButton(e.currentTarget, copyReport(err));
  });
  $("turn-error-close").addEventListener("click", closeModal);
}

// Paint (or remove) the card inside *container*, the chat message list. Called at
// the end of every renderMessages(), so it survives afterStream()'s repaint and
// disappears the moment S.turnError is cleared.
export function renderTurnError(container) {
  container?.querySelector(`#${CARD_ID}`)?.remove();
  const err = activeError();
  if (!container || !err) return;

  const card = document.createElement("div");
  card.id = CARD_ID;
  card.className = "turn-error";
  // role="alert" fires an announcement every time the node enters the DOM, and
  // renderMessages() rebuilds this card on every repaint — switching branches or
  // opening the inspector re-read the same failure aloud. S.turnError is a fresh
  // object per failure, so identity marks the one paint that is actually news.
  if (err !== _announced) {
    _announced = err;
    card.setAttribute("role", "alert");
  }

  const head = document.createElement("div");
  head.className = "turn-error-head";
  const icon = document.createElement("span");
  icon.className = "turn-error-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "⚠";
  const headline = document.createElement("span");
  headline.className = "turn-error-headline";
  headline.textContent = err.headline || "Generation failed.";
  head.append(icon, headline);
  card.appendChild(head);

  // textContent, never innerHTML: this string came from the provider.
  if (err.sentence) {
    const sentence = document.createElement("div");
    sentence.className = "turn-error-sentence";
    sentence.textContent = err.sentence;
    card.appendChild(sentence);
  }

  const bits = meta(err);
  if (bits.length) {
    const line = document.createElement("div");
    line.className = "turn-error-meta";
    line.textContent = bits.join(" · ");
    card.appendChild(line);
  }

  const actions = document.createElement("div");
  actions.className = "turn-error-actions";
  actions.append(
    action("Details", "btn btn-xs", () => showDetails(err)),
    action("Dismiss", "btn btn-xs", clearTurnError),
  );
  card.appendChild(actions);

  container.appendChild(card);
}

function action(label, className, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = className;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}
