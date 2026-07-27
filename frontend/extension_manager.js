// extension_manager.js — the Orb-owned management surface for community
// extensions: inspect, consent, install, enable, update, rollback, uninstall,
// and purge.
//
// Everything here is built with DOM creation and textContent. That is not a
// stylistic departure from the HTML-string rendering elsewhere in the app — it
// is the point. Package-authored strings (name, author, description, permission
// copy, diagnostics, blocked entry points) flow through this module, and the
// design's rule is "never render package-provided consent text as markup". A
// `esc()` call that someone forgets during a later edit is a stored XSS in a
// dialog whose entire job is to be trusted; a `textContent` assignment cannot
// be forgotten into unsafety.
//
// Two more invariants ride along:
//
//   * No inline `on*=` handlers and no `window.*` bridge. Every control gets its
//     listener from `addEventListener` here, so a package id never becomes part
//     of a handler string.
//   * The consent round trip is opaque. The server hands back a staging token
//     and normalized permission *values*; we echo exactly those values back. The
//     UI never reconstructs a permission from the label it displayed, so
//     rewording a consent line cannot widen a grant.
//
// This module still renders no component tree itself. A package's `config` view
// is mounted through extension_commands.js -> extension_renderer.js, the one
// path that draws package UI; the manager's own controls stay Orb-authored DOM.

import { api } from "./api.js";
import { applyEffects, disposeStaleViews, mountView, renderCommandSlots } from "./extension_commands.js";
import { closeModal, showModal } from "./modal.js";
import { S } from "./state.js";
import { broadcastExtensionMutation, setExtensionMutationCallback } from "./tabLock.js";
import { $, toast } from "./utils.js";

// ── DOM helpers ─────────────────────────────────────────────────────────────

/**
 * Create an element. `text` is always assigned via textContent, never parsed.
 * @param {string} tag
 * @param {{cls?: string, text?: string, title?: string, style?: string}} [opts]
 * @param {Node[]} [children]
 */
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.cls) node.className = opts.cls;
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.title) node.title = opts.title;
  if (opts.style) node.setAttribute("style", opts.style);
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

function button(label, onClick, cls = "btn btn-sm") {
  const node = el("button", { cls, text: label });
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function replaceChildren(host, ...nodes) {
  host.replaceChildren(...nodes.filter(Boolean));
}

// ── catalog state ───────────────────────────────────────────────────────────

/**
 * Fetch the catalog and replace the declarative model wholesale.
 *
 * A response carrying a generation lower than the one already applied is
 * discarded, not merged: a newer generation has already replaced the model it
 * was computed against, and merging it would resurrect a command or a package
 * the user just removed.
 */
export async function loadExtensionCatalog() {
  let body;
  try {
    body = await api.get("/extensions");
  } catch (e) {
    console.error("Failed to load extension catalog:", e);
    return;
  }
  if (body.runtime_generation < S.extensionRuntimeGeneration) return;
  S.extensionRuntimeGeneration = body.runtime_generation;
  S.extensionCatalog = body.extensions || [];
  S.extensionOrphanedData = body.orphaned_data || [];
  renderExtensionSidebar();
  // Wholesale replacement, then disposal: commands are rebuilt from the new
  // catalog and anything keyed to a revision that is gone (an open view, a form
  // draft) is dropped rather than left describing a package the user removed.
  renderCommandSlots();
  disposeStaleViews();
  if ($("ext-manager-root")) renderManagerBody();
}

/**
 * The workflow manifest carries each extension's load status, so it goes stale
 * on every lifecycle mutation too. Refetched as data only — no module is
 * imported as a result, because a community entry is never a trusted module.
 */
async function refreshWorkflowManifest() {
  try {
    S.workflowManifest = await api.get("/workflows");
  } catch (e) {
    console.warn("workflow manifest refresh failed:", e);
  }
}

// ── sidebar ─────────────────────────────────────────────────────────────────

export function renderExtensionSidebar() {
  const host = $("extensions-list");
  if (!host) return;
  const nodes = [];
  if (S.extensionCatalog.length === 0) {
    nodes.push(el("div", { cls: "empty-hint", text: "No extensions installed" }));
  }
  for (const item of S.extensionCatalog) {
    nodes.push(sidebarRow(item));
  }
  nodes.push(button("Manage Extensions", () => showExtensionManagerModal(), "btn btn-block btn-sm"));
  replaceChildren(host, ...nodes);
}

function sidebarRow(item) {
  const name = el("div", {
    text: item.name || item.id,
    style: "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis",
  });
  const row = el("div", { cls: "fragment-item" }, [name, statusChip(item), enableToggle(item)]);
  row.style.cursor = "pointer";
  row.addEventListener("click", () => showExtensionManagerModal(item.id));
  return row;
}

/**
 * The one on/off control for an installed extension, in the one place an
 * extension is listed. Same markup as a mood fragment's toggle so the sidebar
 * reads as a single kind of list.
 *
 * Built with nodes rather than the fragments' inline-handler template: this
 * module's invariant is that no package-authored string reaches an attribute,
 * and the id in `onchange="...('<id>')"` would be exactly that.
 *
 * Never disabled from elsewhere: this checkbox is the whole truth about whether
 * the extension runs. The Secondary master used to gate it too, which meant a
 * row could read "on" while nothing worked, with the reason in another panel.
 */
function enableToggle(item) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(item.enabled);
  box.addEventListener("change", () => void setEnabled(item.id, box.checked));

  const label = el("label", { cls: "tog" }, [box, el("span", { cls: "tog-slider" })]);
  const wrap = el("div", { cls: "frag-toggle-wrapper" }, [label]);
  // The row opens the manager on click; the toggle is not that click.
  wrap.addEventListener("click", (e) => e.stopPropagation());
  return wrap;
}

/**
 * Flip one extension, then resync everything that renders off the catalog.
 *
 * The same route `settings.js` uses for a built-in workflow -- the server sends
 * a community id down the extension lifecycle, so this is one write path, not
 * two. It is called from here rather than by importing `toggleWorkflowEnabled`
 * because that module reaches the whole app: pulling it in would drag chat,
 * panels, and the model settings into a module whose isolation is the point (and
 * into the test that guards it).
 *
 * No Secondary-panel repaint is needed -- an extension is not listed there any
 * more. `loadExtensionCatalog` covers the rest, including message-level slots:
 * `renderCommandSlots` rebuilds every `[data-ext-slot]` host from the extensions
 * that are still enabled, so a disabled package's placements clear with it.
 */
async function setEnabled(extensionId, enabled) {
  try {
    const res = await api.post(`/workflows/${encodeURIComponent(extensionId)}/enabled`, { enabled });
    if (res && typeof res.workflow_enabled === "object") S.settings.workflow_enabled = res.workflow_enabled;
  } catch (e) {
    toast(errorText(e), true);
  }
  // Refetched either way: on success it carries the new state, and on failure it
  // restores the checkbox from the server's unchanged view rather than leaving
  // the click showing a flip that did not happen.
  await loadExtensionCatalog();
  broadcastExtensionMutation({ generation: S.extensionRuntimeGeneration });
}

/**
 * `installed`, `enabled`, and `available` are three independent axes, so the
 * chip names which one is off rather than collapsing them into "broken".
 *
 * Enablement is *not* one of the axes it reports: the row's own toggle owns and
 * displays that, right next to it. A chip restating it is a second element for
 * one fact, and the two drift the moment either renders stale. What is left is
 * the state no toggle can show -- this package cannot run, or can only partly
 * run, whatever the switch says.
 */
function statusChip(item) {
  if (item.load_status !== "available") {
    return el("span", {
      cls: "chip chip-warn",
      text: item.load_status.replace("_", " "),
      title: item.diagnostic || "",
    });
  }
  if (item.blocked_entry_points?.length) {
    return el("span", { cls: "chip chip-warn", text: "limited", title: item.diagnostic || "" });
  }
  return null;
}

// ── manager modal ───────────────────────────────────────────────────────────

let _selectedId = null;

export function showExtensionManagerModal(extensionId = null) {
  _selectedId = extensionId;
  showModal('<h2>Extensions</h2><div id="ext-manager-root"></div>');
  renderManagerBody();
  void loadExtensionCatalog();
}

function renderManagerBody() {
  const host = $("ext-manager-root");
  if (!host) return;

  const actions = el("div", { cls: "btn-row", style: "gap:8px;margin-bottom:10px" }, [
    button("⬆ Install from file", () => triggerExtensionInstall()),
    button("Close", () => closeModal()),
  ]);

  const list = el("div", { cls: "ext-list" });
  if (S.extensionCatalog.length === 0) {
    list.appendChild(el("div", { cls: "empty-hint", text: "No extensions installed." }));
  }
  for (const item of S.extensionCatalog) list.appendChild(managerCard(item));

  const nodes = [actions, list];
  const orphaned = orphanedSection();
  if (orphaned) nodes.push(orphaned);
  replaceChildren(host, ...nodes);
}

function managerCard(item) {
  const open = _selectedId === item.id;
  const header = el("div", { cls: "ext-card-header" }, [
    el("div", { style: "flex:1;min-width:0" }, [
      el("div", { cls: "ext-card-name", text: `${item.name || item.id}${item.version ? ` ${item.version}` : ""}` }),
      el("div", { cls: "ext-card-author", text: item.author ? `by ${item.author}` : "" }),
    ]),
    statusChip(item),
  ]);
  header.style.cursor = "pointer";
  header.addEventListener("click", () => {
    _selectedId = open ? null : item.id;
    renderManagerBody();
  });

  const card = el("div", { cls: "ext-card" }, [header]);
  if (!item.enabled) card.style.opacity = "0.5";
  if (item.description) card.appendChild(el("div", { cls: "ext-card-desc", text: item.description }));
  if (item.diagnostic) card.appendChild(el("div", { cls: "ext-diagnostic", text: item.diagnostic }));
  if (open) card.appendChild(cardDetail(item));
  return card;
}

function cardDetail(item) {
  const body = el("div", { cls: "ext-card-detail" });

  body.appendChild(
    el("div", {
      cls: "ext-field",
      text: `Source: ${item.source_kind}${item.source_url ? ` — ${item.source_url}` : ""}`,
    }),
  );
  body.appendChild(el("div", { cls: "ext-field ext-digest", text: `Revision: ${item.active_digest.slice(0, 16)}…` }));

  if (item.blocked_entry_points?.length) {
    body.appendChild(el("div", { cls: "ext-field", text: "Not published (missing permissions):" }));
    const blocked = el("ul", { cls: "ext-blocked" });
    for (const name of item.blocked_entry_points) blocked.appendChild(el("li", { text: name }));
    body.appendChild(blocked);
  }

  // The banner is derived server-side from the *approved* grant set, so it
  // says "this package can do this now", not "this package asked for this".
  if (item.combination_warning) {
    body.appendChild(el("div", { cls: "ext-banner", text: item.combination_warning }));
  }

  body.appendChild(permissionList(item));

  if (item.config_view) body.appendChild(configSection(item));
  const telemetry = telemetrySection(item);
  if (telemetry) body.appendChild(telemetry);

  // No Enable/Disable button here on purpose: the sidebar row's toggle is the
  // one enablement control. The dimming above is a readout, not a second switch.
  const row = el("div", { cls: "btn-row ext-footer" }, [
    button("Update from file…", () => triggerExtensionUpdate(item.id)),
    item.can_rollback ? button("Roll back", () => startRollback(item.id)) : null,
    el("div", { cls: "ext-footer-danger" }, [
      button("Uninstall", () => uninstall(item.id), "btn btn-sm btn-danger"),
      button("Purge data…", () => startPurge(item.id), "btn btn-sm btn-danger"),
    ]),
  ]);
  body.appendChild(row);
  return body;
}

/**
 * Live grant editing. Each row's checkbox carries the normalized permission
 * *value* on the element itself, so submitting collects values rather than
 * parsing the labels back out of the DOM.
 */
function permissionList(item) {
  const wrap = el("div", { cls: "ext-permissions" });
  if (!item.permissions?.length) {
    wrap.appendChild(el("div", { cls: "empty-hint", text: "This extension requests no permissions." }));
    return wrap;
  }
  wrap.appendChild(el("div", { cls: "ext-section-title", text: "Permissions" }));
  const boxes = [];
  for (const permission of item.permissions) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!permission.granted;
    box._permissionValue = permission.value;
    boxes.push(box);
    wrap.appendChild(permissionRow(permission, box));
  }
  wrap.appendChild(
    el("div", { cls: "ext-actions" }, [
      button("Save permissions", async () => {
        const approved = boxes.filter((b) => b.checked).map((b) => b._permissionValue);
        await run(() => api.put(`/extensions/${encodeURIComponent(item.id)}/permissions`, { permissions: approved }));
      }),
    ]),
  );
  return wrap;
}

/**
 * The package's `config` view, rendered inside its detail panel.
 *
 * A convention rather than a slot: no placement and no `ui.contribute` grant is
 * involved, because the surface is this manager, which the user opened
 * deliberately. The tree still goes through the same host renderer every other
 * view uses — the manager does not gain a second, laxer drawing path just
 * because it owns the panel.
 */
function configSection(item) {
  const wrap = el("div", { cls: "ext-config" }, [el("div", { cls: "ext-section-title", text: "Settings" })]);
  const host = el("div", { cls: "xc-view" });
  wrap.appendChild(host);
  void mountView(host, item.id, item.config_view, { instanceId: "manager-config" });
  return wrap;
}

/**
 * Host-owned invocation counters, beside load status where they belong.
 *
 * These answer "which extension slows my turns" and never reach the package:
 * nothing here is projected into a flow's context, a view's data, or an error a
 * package can read.
 */
function telemetrySection(item) {
  const stats = item.telemetry;
  if (!stats?.invocations) return null;
  const parts = [
    `${stats.invocations} run(s)`,
    `avg ${stats.average_ms} ms`,
    `max ${stats.max_ms} ms`,
    `${stats.model_calls} model call(s)`,
  ];
  if (stats.errors) parts.push(`${stats.errors} error(s)`);
  if (stats.cancellations) parts.push(`${stats.cancellations} cancelled`);
  return el("div", { cls: "ext-telemetry", text: parts.join(" · ") });
}

/**
 * One consent line: an optional checkbox, the sentence, and its detail.
 *
 * The detail goes *inside* the sentence span. As a sibling of it, the row's
 * flex layout made it a third column, so `(lane: agent)` sat right-aligned
 * beside the wrapped sentence instead of reading as part of it — which is what
 * `permissionDetail`'s leading space was always for.
 */
function permissionRow(permission, box) {
  const label = el("label", { cls: `ext-permission${permission.emphasis === "high" ? " ext-permission-loud" : ""}` });
  if (box) label.appendChild(box);
  const sentence = el("span", { text: permission.description });
  sentence.appendChild(el("span", { cls: "ext-permission-detail", text: permissionDetail(permission) }));
  label.appendChild(sentence);
  return label;
}

function permissionDetail(permission) {
  const parts = Object.entries(permission.parameters || {}).map(
    ([key, value]) => `${key}: ${[].concat(value).join(", ")}`,
  );
  return parts.length ? ` (${parts.join("; ")})` : "";
}

function orphanedSection() {
  const orphans = S.extensionOrphanedData || [];
  if (orphans.length === 0) return null;
  const wrap = el("div", { cls: "ext-orphans" }, [
    el("div", { cls: "ext-section-title", text: "Data left behind by uninstalled extensions" }),
  ]);
  for (const orphan of orphans) {
    wrap.appendChild(
      el("div", { cls: "fragment-item" }, [
        el("div", { style: "flex:1", text: `${orphan.id} — ${orphan.records} record(s)` }),
        button("Purge", () => startPurge(orphan.id), "btn btn-sm btn-danger"),
      ]),
    );
  }
  return wrap;
}

// ── install / update consent flow ───────────────────────────────────────────

function pickFile(onPicked) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".orbext,.zip";
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    if (file) onPicked(file);
  });
  input.click();
}

export function triggerExtensionInstall() {
  pickFile(async (file) => {
    const inspection = await inspect(() => api.upload("/extensions/inspect-file", file));
    if (inspection) showConsentModal(inspection);
  });
}

function triggerExtensionUpdate(extensionId) {
  pickFile(async (file) => {
    const inspection = await inspect(() =>
      api.upload(`/extensions/${encodeURIComponent(extensionId)}/inspect-update`, file),
    );
    if (inspection) showConsentModal(inspection, extensionId);
  });
}

async function startRollback(extensionId) {
  const inspection = await inspect(() =>
    api.post(`/extensions/${encodeURIComponent(extensionId)}/inspect-rollback`, {}),
  );
  if (inspection) showConsentModal(inspection, extensionId);
}

async function inspect(request) {
  try {
    return await request();
  } catch (e) {
    toast(errorText(e), true);
    return null;
  }
}

/**
 * The consent screen. Every field comes from the server's inspection result;
 * nothing is derived from the file the user picked, and no package string
 * becomes markup.
 */
function showConsentModal(inspection, extensionId = null) {
  const verb = { install: "Install", update: "Update", rollback: "Roll back to" }[inspection.operation] || "Install";
  showModal('<h2 id="ext-consent-title"></h2><div id="ext-consent-root"></div>');
  $("ext-consent-title").textContent = `${verb} ${inspection.name} ${inspection.version}`;

  const root = $("ext-consent-root");
  const nodes = [];

  if (inspection.author) nodes.push(el("div", { cls: "ext-field", text: `Author: ${inspection.author}` }));
  if (inspection.description) nodes.push(el("div", { cls: "ext-card-desc", text: inspection.description }));
  if (inspection.homepage) nodes.push(el("div", { cls: "ext-field", text: `Homepage: ${inspection.homepage}` }));
  nodes.push(el("div", { cls: "ext-field ext-digest", text: `Revision: ${inspection.content_digest}` }));
  nodes.push(
    el("div", {
      cls: "ext-field",
      text: `${inspection.files.length} file(s), ${Math.ceil(inspection.total_bytes / 1024)} KiB`,
    }),
  );
  if (inspection.installed_version) {
    nodes.push(el("div", { cls: "ext-field", text: `Currently installed: ${inspection.installed_version}` }));
  }
  if (!inspection.compatible) {
    nodes.push(
      el("div", {
        cls: "ext-diagnostic",
        text: `This Orb build does not provide: ${inspection.unsupported.join(", ")}. It will install but stay inactive.`,
      }),
    );
  }
  // Reinstalling under an id that already owns namespaced data means the
  // package regains access to it if state.read is approved. The design calls
  // for this being said out loud rather than inferred from "install succeeded".
  const orphan = (S.extensionOrphanedData || []).find((row) => row.id === inspection.id);
  if (orphan) {
    nodes.push(
      el("div", {
        cls: "ext-diagnostic",
        text:
          `Orb still holds ${orphan.records} record(s) of data under the id "${inspection.id}". ` +
          "Installing this package gives it access to that data if you approve its storage permissions. " +
          "Purge the data first if this is a different publisher.",
      }),
    );
  }

  if (inspection.combination_warning) {
    nodes.push(el("div", { cls: "ext-banner", text: inspection.combination_warning }));
  }

  const boxes = [];
  const diff = inspection.permission_diff || { added: [], unchanged: [], removed: [] };
  nodes.push(permissionSection("Requested for the first time", diff.added, boxes, true));
  nodes.push(permissionSection("Already approved", diff.unchanged, boxes, true));
  nodes.push(permissionSection("No longer requested (will be dropped)", diff.removed, null, false));
  if (inspection.origins?.length) {
    nodes.push(el("div", { cls: "ext-field", text: `Network origins: ${inspection.origins.join(", ")}` }));
  }
  if (inspection.secrets?.length) {
    nodes.push(
      el("div", {
        cls: "ext-field",
        text: `Secrets to configure: ${inspection.secrets.map((s) => s.name).join(", ")}`,
      }),
    );
  }

  const enabledBox = document.createElement("input");
  enabledBox.type = "checkbox";
  enabledBox.checked = true;
  if (inspection.operation === "install") {
    const label = el("label", { cls: "ext-permission" });
    label.appendChild(enabledBox);
    label.appendChild(el("span", { text: "Enable after installing" }));
    nodes.push(label);
  }

  nodes.push(
    el("div", { cls: "modal-actions" }, [
      button("Cancel", () => closeModal()),
      button(
        verb,
        async () => {
          const approved = boxes.filter((b) => b.checked).map((b) => b._permissionValue);
          const path =
            inspection.operation === "install"
              ? "/extensions/install"
              : `/extensions/${encodeURIComponent(extensionId || inspection.id)}/${inspection.operation}`;
          const payload =
            inspection.operation === "install"
              ? { token: inspection.token, permissions: approved, enabled: enabledBox.checked }
              : { token: inspection.token, permissions: approved };
          const ok = await run(() => api.post(path, payload));
          if (ok) {
            closeModal();
            toast(`${inspection.name} ${inspection.operation === "install" ? "installed" : "updated"}`);
          }
        },
        "btn btn-primary",
      ),
    ]),
  );

  replaceChildren(root, ...nodes);
}

function permissionSection(title, rows, boxes, selectable) {
  if (!rows?.length) return null;
  const wrap = el("div", { cls: "ext-permissions" }, [el("div", { cls: "ext-section-title", text: title })]);
  for (const permission of rows) {
    let box = null;
    if (selectable && boxes) {
      box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box._permissionValue = permission.value;
      boxes.push(box);
    }
    wrap.appendChild(permissionRow(permission, box));
  }
  return wrap;
}

// ── other lifecycle actions ─────────────────────────────────────────────────

async function uninstall(extensionId) {
  const ok = await run(() => api.del(`/extensions/${encodeURIComponent(extensionId)}`));
  if (ok) toast(`${extensionId} uninstalled — its stored data was preserved`);
}

/**
 * Purge is two requests: a preview the user reads, then a confirmation bound to
 * that preview's token. The token is what stops a stale tab from authorising a
 * deletion broader than the counts on screen.
 */
async function startPurge(extensionId) {
  let preview;
  try {
    preview = await api.post(`/extensions/${encodeURIComponent(extensionId)}/purge-data`, {});
  } catch (e) {
    toast(errorText(e), true);
    return;
  }
  showModal('<h2 id="ext-purge-title"></h2><div id="ext-purge-root"></div>');
  $("ext-purge-title").textContent = `Purge data for ${extensionId}`;
  const rows = Object.entries(preview.counts).filter(([, count]) => count > 0);
  const list = el("ul", { cls: "ext-blocked" });
  for (const [what, count] of rows) list.appendChild(el("li", { text: `${what}: ${count}` }));
  replaceChildren(
    $("ext-purge-root"),
    el("div", { cls: "ext-card-desc", text: "This permanently deletes the data listed below. It cannot be undone." }),
    rows.length ? list : el("div", { cls: "empty-hint", text: "No stored data found for this extension." }),
    el("div", { cls: "modal-actions" }, [
      button("Cancel", () => closeModal()),
      button(
        "Purge",
        async () => {
          const ok = await run(() =>
            api.post(`/extensions/${encodeURIComponent(extensionId)}/purge-data`, { token: preview.token }),
          );
          if (ok) {
            closeModal();
            toast(`Purged ${ok.data?.total ?? 0} record(s)`);
          }
        },
        "btn btn-danger",
      ),
    ]),
  );
}

/**
 * Run one lifecycle call, apply its effects, and surface its error.
 *
 * The effect envelope goes through the *shared* handler in
 * extension_commands.js rather than a manager-local copy: an `extension.catalog`
 * effect from an install and one from an action have to mean the same thing, and
 * two mappings would eventually disagree about what a resource name refetches.
 */
async function run(request) {
  try {
    const envelope = await request();
    await applyEffects(envelope);
    await refreshWorkflowManifest();
    return envelope;
  } catch (e) {
    toast(errorText(e), true);
    return null;
  }
}

/**
 * Server errors arrive as a JSON body; show the `detail` when there is one.
 * These strings are already sanitized server-side — they name a path, a limit,
 * or a field, never package content — and they land in a toast's textContent.
 */
function errorText(e) {
  try {
    const parsed = JSON.parse(e.message);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    // Not JSON: fall through to the raw message.
  }
  return e.message || "Extension operation failed";
}

// ── boot ────────────────────────────────────────────────────────────────────

export function initExtensionManager() {
  const header = $("extensions-section-header");
  if (header) {
    header.addEventListener("click", () => {
      header.querySelector(".arrow")?.classList.toggle("collapsed");
      header.nextElementSibling?.classList.toggle("collapsed");
    });
  }
  // Another tab installed, updated, enabled, or removed something. Refetch
  // rather than trusting the broadcast payload: the message says *that* the
  // catalog moved, never what it now contains.
  setExtensionMutationCallback(() => {
    void loadExtensionCatalog();
    void refreshWorkflowManifest();
  });
}
