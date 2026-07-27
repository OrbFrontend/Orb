// extension_commands.js — the host command model, slot placement, workspace
// lifecycle, and the fixed effect-to-refetch mapping.
//
// Three responsibilities, all host-owned:
//
//   * **Commands are data.** A community command is a `{id, label, icon, opens,
//     action, when}` record from the catalog. It never becomes a module path, an
//     inline handler, a `window.*` name, or an entry in a workflow_registry
//     callback array. Rendering a slot builds buttons and attaches listeners
//     here, so a package id cannot reach a handler string.
//   * **The menu model is shared with the built-ins.** The composer burger and
//     the mobile action menu are now one list of host commands with two source
//     bands: Orb's own entries, then enabled extension placements. Built-in
//     items are supplied by the shell at init rather than imported, which keeps
//     this module from reaching up into the chat feature layer.
//   * **Effects drive refetches, packages do not.** A response's `effects` array
//     is a closed vocabulary of resource names; this module owns what each one
//     refetches locally and rebroadcasts cross-tab. An unrecognised resource is
//     dropped and logged — a newer server degrades to "no repaint" rather than
//     reaching an unintended handler.
//
// Generation discipline: every catalog replacement bumps
// `S.extensionRuntimeGeneration`. A response carrying a lower generation is
// discarded, an open workspace whose extension vanished is closed, and form
// drafts keyed to a departed digest are dropped. A renderer-driven loop (the
// library sweep) also stops when the generation advances mid-run, so the server
// is never left committing writes whose envelopes the frontend throws away.

import { api } from "./api.js";
import { disposeStaleDrafts, evaluatePredicate, iconGlyph, renderView } from "./extension_renderer.js";
import { closeModal, setModalCloseCallback, showModal } from "./modal.js";
import { effectiveWorkflowEnabled, notify, S } from "./state.js";
import { broadcastExtensionMutation } from "./tabLock.js";
import { $, toast } from "./utils.js";

/** Built-in menu entries, supplied by the shell so this module imports no feature. */
let _builtins = {};

/** Host callbacks the shell registers for effects it owns (chat repaints). */
let _refetch = {};

export function initExtensionCommands({ builtins = {}, refetch = {} } = {}) {
  _builtins = builtins;
  _refetch = refetch;
}

// ── the command model ───────────────────────────────────────────────────────

/**
 * Every command placed in `slot`, built-ins first and extensions second.
 *
 * The two bands never interleave: a package cannot push itself above "New
 * conversation" by choosing a label, because ordering is by band and then by
 * extension id, not by anything a manifest says.
 */
export function slotCommands(slot) {
  const items = (_builtins[slot] || []).map((item) => ({ ...item, source: "builtin" }));
  for (const extension of enabledExtensions()) {
    const commands = new Map((extension.commands || []).map((command) => [command.id, command]));
    for (const placement of extension.placements || []) {
      if (placement.slot !== slot) continue;
      if (placement.command) {
        const command = commands.get(placement.command);
        if (command) items.push({ ...command, source: "community", extensionId: extension.id });
      } else if (placement.view) {
        items.push({
          id: `view:${placement.view}`,
          label: viewLabel(extension, placement.view),
          opens: placement.view,
          source: "community",
          extensionId: extension.id,
        });
      }
    }
  }
  return items;
}

function viewLabel(extension, viewId) {
  return `${extension.name || extension.id} — ${viewId}`;
}

function enabledExtensions() {
  // `installed`, `available`, and `enabled` are three axes; a placement needs
  // all three plus a grant for its slot, which the server already applied by
  // listing the placement at all only when it published.
  return [...(S.extensionCatalog || [])]
    .filter(
      (entry) => entry.load_status === "available" && entry.enabled && effectiveWorkflowEnabled(entry.id, "community"),
    )
    .sort((a, b) => a.id.localeCompare(b.id));
}

/** Whether a command's availability predicate holds right now. */
function commandAvailable(command) {
  if (!command.when) return true;
  // Availability reads a tiny host projection and cannot call a flow: a menu
  // that had to run package logic to decide whether to render would put package
  // logic on every repaint.
  const host = {
    active_conversation_id: S.activeConvId || undefined,
    active_character_id: S.activeCharId || undefined,
  };
  return evaluatePredicate(command.when, { host });
}

// ── slot rendering ──────────────────────────────────────────────────────────

function menuItem(command, onPick) {
  const item = document.createElement("div");
  item.className = "burger-menu-item";
  item.setAttribute("role", "button");
  item.tabIndex = 0;
  const icon = document.createElement("span");
  icon.className = "burger-icon";
  icon.textContent = command.icon ? iconGlyph(command.icon) : command.glyph || "";
  item.appendChild(icon);
  const label = document.createElement("span");
  // A built-in may supply a function so a live label (the persona name on the
  // mobile menu) stays correct without a bespoke updater reaching into the DOM.
  // A community command's label is always a plain manifest string.
  label.textContent = typeof command.label === "function" ? command.label() : command.label;
  item.appendChild(label);
  const run = () => onPick(command);
  item.addEventListener("click", run);
  item.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      run();
    }
  });
  return item;
}

/**
 * Paint one slot's menu into `host`, replacing whatever was there.
 *
 * Replacing wholesale is what makes hot reload safe: disabling or uninstalling
 * an extension removes its placement without leaving a detached listener
 * behind, and a catalog generation swap never duplicates a command.
 */
export function renderSlot(slot, host, { onDone } = {}) {
  if (!host) return;
  const nodes = [];
  for (const command of slotCommands(slot)) {
    // Built-ins gate on a host predicate function (the Notes entry is hidden
    // while the feature is dormant); community commands gate on their declared
    // `when` over a tiny host projection.
    if (command.visible && !command.visible()) continue;
    if (!commandAvailable(command)) continue;
    nodes.push(
      menuItem(command, (picked) => {
        onDone?.();
        void runCommand(picked);
      }),
    );
  }
  host.replaceChildren(...nodes);
}

export function renderComposerMenu() {
  renderSlot("composer.menu", $("burger-dropdown"), { onDone: () => $("burger-dropdown")?.classList.remove("open") });
}

export function renderMobileActionsMenu() {
  const host = $("mobile-chat-actions-menu");
  renderSlot("mobile.chat_actions", host, { onDone: () => host?.classList.remove("open") });
}

/** Repaint every menu-shaped slot. Called on catalog and conversation changes. */
export function renderCommandSlots() {
  renderComposerMenu();
  renderMobileActionsMenu();
  renderPanelSlots();
}

/**
 * Fill every `[data-ext-slot]` container under `root` with that slot's content.
 *
 * The host declares where a slot lives by putting an empty container in its own
 * markup; extensions never insert DOM nodes and never name a selector. A slot's
 * *view* placements mount through the renderer, and its *command* placements
 * become buttons — which is why one function covers both the panel slots and
 * the per-message ones.
 */
export function renderPanelSlots(root = document) {
  for (const host of root.querySelectorAll("[data-ext-slot]")) {
    const slot = host.dataset.extSlot;
    const cardId = host.dataset.extCardId || null;
    const input = host.dataset.extMsgId ? { message_id: Number(host.dataset.extMsgId) } : {};
    host.replaceChildren();
    for (const extension of enabledExtensions()) {
      for (const placement of extension.placements || []) {
        if (placement.slot !== slot) continue;
        if (placement.view) {
          const mount = document.createElement("div");
          mount.className = "xc-view";
          host.appendChild(mount);
          void mountView(mount, extension.id, placement.view, {
            instanceId: `${slot}:${host.dataset.extMsgId || "0"}`,
          });
        } else if (placement.command) {
          const command = (extension.commands || []).find((c) => c.id === placement.command);
          if (!command || !commandAvailable(command)) continue;
          host.appendChild(slotButton(extension.id, command, { slot, input, cardId }));
        }
      }
    }
  }
}

function slotButton(extensionId, command, { slot, input, cardId }) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-sm xc-slot-btn";
  button.title = command.label;
  const glyph = command.icon ? iconGlyph(command.icon) : "";
  if (glyph) {
    // Its own span, like the burger menu's icon. A glyph that falls back to the
    // emoji font inflates whatever line box it shares, so leaving it in the
    // label's text node pushes the label off the button's vertical centre.
    const icon = document.createElement("span");
    icon.className = "xc-slot-icon";
    icon.textContent = glyph;
    button.appendChild(icon);
  }
  const label = document.createElement("span");
  label.textContent = command.label;
  button.appendChild(label);
  button.addEventListener("click", (event) => {
    // The library card is itself clickable ("open this character"), so a slot
    // button inside it must not also select the card.
    event.stopPropagation();
    if (command.opens) {
      openWorkspace(extensionId, command.opens);
    } else if (command.action) {
      // `card_id` travels beside `slot`, never inside `input`: the server uses
      // that distinction to tell a host-supplied card from a package-named one,
      // and the two cost different grants.
      void dispatchAction(extensionId, command.action, cardId ? { slot, card_id: cardId } : { input });
    }
  });
  return button;
}

// ── command execution ───────────────────────────────────────────────────────

async function runCommand(command) {
  if (command.source === "builtin") {
    command.run?.();
    return;
  }
  if (command.opens) {
    openWorkspace(command.extensionId, command.opens);
    return;
  }
  if (command.action) await dispatchAction(command.extensionId, command.action, {});
}

/**
 * Dispatch one named action and apply its fixed effect envelope.
 *
 * `slot` and `card_id` travel as top-level fields, never inside `input`: the
 * server distinguishes "the host supplied this card because the user clicked
 * it" from "the package asked for this card", and the two cost different
 * grants. Folding them into `input` would erase that distinction on the wire.
 */
export async function dispatchAction(
  extensionId,
  action,
  { input = {}, slot = null, card_id = null, signal = null } = {},
) {
  const body = { conversation_id: S.activeConvId || null, input };
  if (slot) body.slot = slot;
  if (card_id) body.card_id = card_id;
  let envelope;
  try {
    envelope = await api.post(
      `/extensions/${encodeURIComponent(extensionId)}/actions/${encodeURIComponent(action)}`,
      body,
      signal ? { signal } : {},
    );
  } catch (e) {
    // An abort is the user closing the surface that started this, not a
    // failure: the request drops, the server's disconnect watcher aborts the
    // model call, and there is nothing to report.
    if (e?.name === "AbortError") return null;
    toast(errorText(e), true);
    return null;
  }
  await applyEffects(envelope);
  return envelope;
}

export function errorText(e) {
  try {
    const parsed = JSON.parse(e.message);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    // Not JSON: fall through to the raw message.
  }
  return e?.message || "The extension action failed";
}

// ── the fixed effect mapping ────────────────────────────────────────────────
//
// The frontend owns this table; the server owns the vocabulary. Coalescing is
// the frontend's business too — a library sweep emits one `character.card`
// effect per card, and debouncing that into a bounded number of refetches
// belongs here rather than in an envelope that would then describe the UI's
// intentions instead of the invocation's writes.

let _cardRefetchTimer = null;

function debouncedCardRefetch() {
  if (_cardRefetchTimer) clearTimeout(_cardRefetchTimer);
  _cardRefetchTimer = setTimeout(() => {
    _cardRefetchTimer = null;
    _refetch.characters?.();
    notify("characters", { reason: "extension" });
  }, 400);
}

export async function applyEffects(envelope) {
  if (!envelope) return;
  if (typeof envelope.runtime_generation === "number") {
    if (envelope.runtime_generation < S.extensionRuntimeGeneration) return;
    S.extensionRuntimeGeneration = envelope.runtime_generation;
  }
  for (const toastPayload of envelope.toasts || []) {
    toast(toastPayload.text, toastPayload.tone === "error" || toastPayload.tone === "warning");
  }
  // Collected first, dispatched once. Branch activation emits messages,
  // director, and direction-notes together and they share one refetch path;
  // running that path per effect would triple the work to satisfy an envelope
  // that describes a single mutation.
  const changed = new Set();
  for (const effect of envelope.effects || []) {
    switch (effect?.resource) {
      case "conversation.messages":
      case "conversation.director":
        changed.add("conversation");
        break;
      case "conversation.direction_notes":
        changed.add("direction_notes");
        break;
      case "character.card":
        debouncedCardRefetch();
        break;
      case "extension.view":
        refreshOpenView(effect.extension_id, effect.view);
        break;
      case "extension.catalog":
        changed.add("catalog");
        break;
      default:
        console.warn("unknown extension effect dropped:", effect?.resource);
    }
  }
  if (changed.has("conversation")) await _refetch.conversation?.();
  if (changed.has("direction_notes")) await _refetch.directionNotes?.();
  if (changed.has("catalog")) await _refetch.catalog?.();
  broadcastExtensionMutation({ generation: S.extensionRuntimeGeneration });
}

// ── the library sweep ───────────────────────────────────────────────────────

/**
 * Walk the library one page at a time and dispatch `action` once per card.
 *
 * The loop is *here*, not in the flow language, which is what lets a
 * library-wide feature fit inside the 128-step, two-model-call per-invocation
 * budget. Each dispatch classifies one card and commits independently, so a
 * sweep interrupted at card 87 leaves 86 written and 214 untouched and the next
 * run resumes from the remainder — there is no partial commit to reconcile,
 * because there was never one transaction.
 *
 * Three properties come from the loop rather than from any one invocation:
 *
 *   * **A generation change halts it.** Every envelope carries
 *     `runtime_generation`, and responses below the generation already seen are
 *     discarded. Left running, an update or disable at card 87 would leave the
 *     server committing writes whose envelopes we throw away — work with no
 *     feedback and no record in the view. So the loop stops and reports the
 *     count it completed.
 *   * **Concurrency stays at one.** A deliberate floor, not a measurement: each
 *     card costs a model call on a user-configured lane and there is no
 *     rate-limit model for those lanes.
 *   * **Cursors are opaque.** The walk sends back whatever `next_cursor` the
 *     server issued and never constructs one.
 *
 * A failed card stops the sweep rather than skipping on: the most likely cause
 * is a revoked grant or an unreachable lane, and grinding through 200 more
 * failures would bury that.
 */
async function runLibrarySweep(extensionId, action, unclassifiedKey, report, signal) {
  const startGeneration = S.extensionRuntimeGeneration;
  let cursor = null;
  let done = 0;
  for (;;) {
    let page;
    try {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      page = await api.get(`/extensions/${encodeURIComponent(extensionId)}/resources/library.cards${query}`, {
        signal,
      });
    } catch (e) {
      if (e?.name !== "AbortError") toast(errorText(e), true);
      return done;
    }
    for (const card of page.cards || []) {
      // Closing the view aborts its signal. Checked here as well as passed to
      // each request so the loop stops at the card boundary rather than firing
      // one more invocation it would immediately throw away.
      if (signal?.aborted) return done;
      if (S.extensionRuntimeGeneration !== startGeneration) {
        toast(`Stopped after ${done} card(s): the extension changed while the sweep was running.`, true);
        return done;
      }
      if (unclassifiedKey && card.state?.[unclassifiedKey]) continue;
      report(`${card.name || card.id}…`);
      const envelope = await dispatchAction(extensionId, action, { input: { card_id: card.id }, signal });
      if (!envelope) return done; // the error has already been surfaced
      done += 1;
    }
    cursor = page.next_cursor;
    if (!cursor) return done;
  }
}

// ── workspace + view lifecycle ──────────────────────────────────────────────

/** The views currently mounted, so an `extension.view` effect can find them. */
const _openViews = new Map();

function viewKey(extensionId, viewId) {
  return `${extensionId}:${viewId}`;
}

/**
 * Mount one extension view into `container` and keep it refreshable.
 *
 * Returns a disposer. The mount is recorded so `ui.invalidate` can repaint
 * exactly this view rather than everything, and so a generation swap can drop
 * a view whose extension no longer publishes it.
 */
export async function mountView(container, extensionId, viewId, { instanceId = "0" } = {}) {
  const key = viewKey(extensionId, viewId);
  // One controller per mount, aborted when the view goes away. Work a view
  // started is work the view owns: a sweep that outlived the modal it was
  // launched from keeps calling the model for a surface nobody is looking at.
  const controller = new AbortController();
  const load = async () => {
    let payload;
    try {
      const query = S.activeConvId ? `?conversation_id=${encodeURIComponent(S.activeConvId)}` : "";
      payload = await api.get(
        `/extensions/${encodeURIComponent(extensionId)}/views/${encodeURIComponent(viewId)}${query}`,
      );
    } catch (e) {
      container.replaceChildren();
      const box = document.createElement("div");
      box.className = "xc-error";
      box.textContent = errorText(e);
      container.appendChild(box);
      return;
    }
    if (typeof payload.runtime_generation === "number" && payload.runtime_generation < S.extensionRuntimeGeneration)
      return;
    renderView(container, payload, {
      extensionId,
      viewId,
      instanceId,
      digest: digestFor(extensionId),
      conversationId: S.activeConvId || null,
      onAction: async (action, input) => {
        const envelope = await dispatchAction(extensionId, action, { input });
        // A view refetches after its own action even without an explicit
        // invalidate: the package just changed something it is displaying, and
        // making it declare that separately would be a footgun with no upside.
        if (envelope) await load();
      },
      onSweep: (action, unclassifiedKey, report) =>
        runLibrarySweep(extensionId, action, unclassifiedKey, report, controller.signal),
      onSaveState: async (updates) => {
        try {
          const envelope = await api.put(`/extensions/${encodeURIComponent(extensionId)}/state`, {
            updates,
            conversation_id: S.activeConvId || null,
          });
          await applyEffects(envelope);
          toast("Saved");
          await load();
        } catch (e) {
          toast(errorText(e), true);
        }
      },
    });
  };
  _openViews.set(key, { container, load, controller });
  await load();
  return () => disposeView(key);
}

/** Drop one mounted view and abort whatever it still has in flight. */
function disposeView(key) {
  _openViews.get(key)?.controller.abort();
  _openViews.delete(key);
}

function digestFor(extensionId) {
  return (S.extensionCatalog || []).find((entry) => entry.id === extensionId)?.active_digest || "";
}

function refreshOpenView(extensionId, viewId) {
  _openViews.get(viewKey(extensionId, viewId))?.load();
}

/** Open a view as a host modal workspace. */
export function openWorkspace(extensionId, viewId) {
  const entry = (S.extensionCatalog || []).find((item) => item.id === extensionId);
  showModal('<h2 id="xc-workspace-title"></h2><div id="xc-workspace-body" class="xc-workspace"></div>');
  $("xc-workspace-title").textContent = entry?.name || extensionId;
  const body = $("xc-workspace-body");
  void mountView(body, extensionId, viewId, { instanceId: "workspace" });
  // Every dismissal path — the Close button, Escape, a backdrop click — lands
  // in `closeModal`, so the disposal hangs off that rather than off one button.
  setModalCloseCallback(() => disposeView(viewKey(extensionId, viewId)));
}

export function closeWorkspace() {
  closeModal();
}

/**
 * Drop everything keyed to a revision that is no longer published.
 *
 * Called after a catalog replacement. Open views whose extension vanished are
 * emptied rather than left showing a revision's UI after that revision is gone,
 * and form drafts for departed digests are discarded so a reinstall does not
 * inherit half-typed values from a package the user removed.
 */
export function disposeStaleViews() {
  const live = new Set((S.extensionCatalog || []).map((entry) => entry.active_digest).filter(Boolean));
  disposeStaleDrafts(live);
  const published = new Set(enabledExtensions().map((entry) => entry.id));
  for (const [key, mounted] of [..._openViews]) {
    const extensionId = key.split(":")[0];
    if (published.has(extensionId)) {
      void mounted.load();
    } else {
      mounted.container.replaceChildren();
      disposeView(key);
    }
  }
}
