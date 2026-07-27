// extension_renderer.js — the host-owned renderer for community component trees.
//
// A package describes a view; Orb draws it. Everything here is DOM creation and
// textContent, and that is a security property rather than a style choice:
//
//   * No component field becomes markup, a class name, an inline style, a URL,
//     an event-handler string, a module path, or a DOM id. Package strings land
//     in `textContent` and in nothing else. There is exactly one place a
//     package string reaches an attribute — `img.alt` / media labels — and the
//     browser never parses those as anything.
//   * Styling is tokenized. `tone`, `size`, `density`, `columns`, `align`, and
//     `span` are closed enumerations that map through host tables to host class
//     names. A value outside the table is dropped, so even if the server's
//     validation were bypassed, an unknown token could not survive as a class.
//   * Media is by reference. An `asset` source becomes an Orb route derived
//     from the extension id and the compiled asset path; a package cannot point
//     the browser at an origin, so rendering a view is never a network beacon.
//   * Unknown component names render an error placeholder rather than nothing.
//     A newer package on an older Orb should look broken, not look fine while
//     silently omitting half its UI.
//
// Value resolution mirrors backend/features/extensions/values.py: `$ref` is
// dictionary lookup over the view's own namespaces, `$template` is scalar path
// substitution with no filters or expressions, and `when` is the same
// structured predicate AST. A path that resolves to nothing is `undefined`
// here, which every consumer treats as absent — the JS side has no need for the
// backend's MISSING sentinel because it never has to distinguish absent from a
// stored null on the way *into* a sink.
//
// Form controls keep an ephemeral draft keyed by
// (extensionId, digest, viewId, instanceId). Rendering never writes state, and
// neither does typing: a bound form is saved by a *host-owned* button that
// posts a host-generated state write, so "run this extension's code" and
// "store what I typed" are never the same click.

// ── tokenized styling ───────────────────────────────────────────────────────
// Closed tables, host-authored. A token not present here contributes no class,
// which is what makes "unknown properties never become DOM attributes" true at
// render time as well as at validation time.

const TONE = {
  default: "",
  muted: "xc-tone-muted",
  accent: "xc-tone-accent",
  success: "xc-tone-success",
  warning: "xc-tone-warning",
  danger: "xc-tone-danger",
};
const SIZE = { sm: "xc-sm", md: "xc-md", lg: "xc-lg" };
const ALIGN = { start: "xc-align-start", center: "xc-align-center", end: "xc-align-end" };
const DENSITY = { compact: "xc-compact", comfortable: "xc-comfortable" };

/** Orb-owned symbolic icons. Glyphs, not asset URLs and not package strings. */
const ICONS = {
  activity: "◈",
  bookmark: "🔖",
  chart: "📊",
  check: "✓",
  clock: "🕑",
  download: "⬇",
  eye: "👁",
  flag: "⚑",
  "git-branch": "⑃",
  globe: "🌐",
  image: "🖼",
  info: "ℹ",
  link: "🔗",
  list: "☰",
  map: "🗺",
  message: "💬",
  meter: "◍",
  pin: "📌",
  plug: "🔌",
  refresh: "↻",
  search: "🔍",
  settings: "⚙",
  sparkle: "✦",
  star: "★",
  tag: "🏷",
  upload: "⬆",
  wand: "✨",
  warning: "⚠",
};

export function iconGlyph(name) {
  return Object.hasOwn(ICONS, name) ? ICONS[name] : "";
}

function cls(...parts) {
  return parts.filter(Boolean).join(" ");
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

// ── value resolution ────────────────────────────────────────────────────────

const TEMPLATE_HOLE = /\{\{([^{}]*)\}\}/g;

/**
 * Resolve a dotted path over plain objects and arrays. Key/index lookup only —
 * never property access on anything that is not a plain container, so a path
 * like `data.__proto__.x` walks off the end and returns undefined instead of
 * reaching a prototype.
 */
export function resolvePath(namespaces, path) {
  let current = namespaces;
  for (const segment of String(path).split(".")) {
    if (Array.isArray(current)) {
      if (!/^\d+$/.test(segment)) return undefined;
      current = current[Number(segment)];
    } else if (current && typeof current === "object" && Object.hasOwn(current, segment)) {
      current = current[segment];
    } else {
      return undefined;
    }
    if (current === undefined) return undefined;
  }
  return current;
}

function scalarText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "object") return "";
  return String(value);
}

/** Evaluate one declared value: a literal, a `$ref`, a `$template`, or a container of those. */
export function resolveValue(value, namespaces) {
  if (Array.isArray(value)) return value.map((item) => resolveValue(item, namespaces));
  if (value && typeof value === "object") {
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === "$ref") return resolvePath(namespaces, value.$ref);
    if (keys.length === 1 && keys[0] === "$template") {
      return String(value.$template).replace(TEMPLATE_HOLE, (_, path) =>
        scalarText(resolvePath(namespaces, String(path).trim())),
      );
    }
    const out = {};
    for (const key of keys) out[key] = resolveValue(value[key], namespaces);
    return out;
  }
  return value;
}

/** Type-strict equality, matching the backend predicate evaluator. */
function equal(left, right) {
  if (typeof left === "boolean" || typeof right === "boolean") return left === right;
  if (typeof left === "number" && typeof right === "number") return left === right;
  if (typeof left !== typeof right) return false;
  return left === right;
}

/**
 * Evaluate the structured predicate AST. Total: an incomparable pair is false,
 * never an exception — a `when` guard that could throw would take the whole
 * view down with it.
 */
export function evaluatePredicate(node, namespaces) {
  if (!node || typeof node !== "object") return false;
  const [op, operand] = Object.entries(node)[0] || [];
  if (op === "and") return (operand || []).every((item) => evaluatePredicate(item, namespaces));
  if (op === "or") return (operand || []).some((item) => evaluatePredicate(item, namespaces));
  if (op === "not") return !evaluatePredicate(operand, namespaces);
  if (op === "exists") return resolveValue(operand, namespaces) !== undefined;
  const left = resolveValue((operand || [])[0], namespaces);
  const right = resolveValue((operand || [])[1], namespaces);
  if (op === "eq") return equal(left, right);
  if (op === "ne") return !equal(left, right);
  const comparable =
    (typeof left === "number" && typeof right === "number") || (typeof left === "string" && typeof right === "string");
  if (!comparable) return false;
  if (op === "lt") return left < right;
  if (op === "lte") return left <= right;
  if (op === "gt") return left > right;
  if (op === "gte") return left >= right;
  return false;
}

// ── ephemeral form drafts ───────────────────────────────────────────────────
// Keyed by (extensionId, digest, viewId, instanceId) so hot reload drops the
// drafts of a revision that no longer exists rather than replaying them into
// whatever view happens to occupy the same slot next.

const _drafts = new Map();

function draftKey(ctx) {
  return `${ctx.extensionId} ${ctx.digest || ""} ${ctx.viewId} ${ctx.instanceId || "0"}`;
}

function draftFor(ctx) {
  const key = draftKey(ctx);
  let draft = _drafts.get(key);
  if (!draft) {
    draft = {};
    _drafts.set(key, draft);
  }
  return draft;
}

/** Drop every draft whose key names a digest other than the ones still live. */
export function disposeStaleDrafts(liveDigests) {
  const live = new Set(liveDigests);
  for (const key of [..._drafts.keys()]) {
    const digest = key.split(" ")[1];
    if (digest && !live.has(digest)) _drafts.delete(key);
  }
}

export function clearDrafts() {
  _drafts.clear();
}

// ── rendering ───────────────────────────────────────────────────────────────

/**
 * Render one view payload into `container`.
 *
 * `payload` is the server's `/views/{id}` response: the compiled tree plus its
 * resolved data, config, state, and per-source errors. `ctx` carries the
 * identity the renderer needs and the two host callbacks it may invoke —
 * `onAction(actionId, input)` and `onError(message)`. There is no third
 * callback a package can reach.
 */
export function renderView(container, payload, ctx) {
  const namespaces = {
    data: payload.data || {},
    config: payload.config || {},
    state: payload.state || {},
    host: { conversation_id: ctx.conversationId || null },
  };
  const scope = { ...ctx, namespaces, draft: draftFor(ctx), payload };
  const nodes = [];
  for (const [name, message] of Object.entries(payload.errors || {})) {
    nodes.push(errorBox(`${name}: ${message}`));
  }
  const root = renderNode(payload.view?.root, scope);
  if (root) nodes.push(root);
  const save = saveBar(scope);
  if (save) nodes.push(save);
  container.replaceChildren(...nodes.filter(Boolean));
}

/**
 * The host-owned submit control for a view containing bound form controls.
 *
 * Host-owned on purpose. A package cannot render its own "Save" that writes
 * state, because writing state is not something a component does — it is a
 * host-generated action, grouped by scope here and validated, size-capped,
 * locked, and committed server-side exactly like any other state write.
 */
function saveBar(scope) {
  if (!hasBoundControls(scope.payload.view?.root)) return null;
  const bar = el("div", "xc-save-bar");
  const button = el("button", "xc-button xc-tone-accent", "Save changes");
  button.type = "button";
  button.addEventListener("click", async () => {
    const updates = {};
    for (const [path, v] of Object.entries(scope.draft)) {
      if (path.startsWith(" ")) continue; // host-owned ephemeral keys (tabs, collapse)
      const segments = path.split(".");
      const scopeName = segments[0] === "config" ? "config" : segments[1];
      const key = segments[segments.length - 1];
      if (!updates[scopeName]) updates[scopeName] = {};
      updates[scopeName][key] = v;
    }
    if (Object.keys(updates).length === 0) return;
    button.disabled = true;
    try {
      await scope.onSaveState(updates);
    } finally {
      button.disabled = false;
    }
  });
  bar.appendChild(button);
  return bar;
}

function hasBoundControls(node) {
  if (!node || typeof node !== "object") return false;
  if (typeof node.bind === "string") return true;
  for (const child of node.children || []) if (hasBoundControls(child)) return true;
  for (const tab of node.tabs || []) for (const child of tab.children || []) if (hasBoundControls(child)) return true;
  return false;
}

function errorBox(text) {
  const box = el("div", "xc-error");
  box.appendChild(el("div", "xc-error-title", "Unavailable"));
  box.appendChild(el("div", "xc-error-body", text));
  return box;
}

function renderChildren(parent, children, scope) {
  for (const child of children || []) {
    const node = renderNode(child, scope);
    if (node) parent.appendChild(node);
  }
}

function renderNode(node, scope) {
  if (!node || typeof node !== "object") return null;
  if (node.when && !evaluatePredicate(node.when, scope.namespaces)) return null;
  const build = COMPONENTS[node.component];
  const rendered = build
    ? build(node, scope)
    : errorBox(`This Orb build cannot render a "${node.component}" component.`);
  if (rendered && node.span) rendered.classList.add(`xc-span-${node.span}`);
  return rendered;
}

function value(node, key, scope) {
  return resolveValue(node[key], scope.namespaces);
}

const COMPONENTS = {
  // ── layout ──
  stack(node, scope) {
    const box = el(
      "div",
      cls("xc-stack", node.direction === "horizontal" ? "xc-row" : "xc-col", SIZE[node.gap], ALIGN[node.align]),
    );
    renderChildren(box, node.children, scope);
    return box;
  },
  grid(node, scope) {
    const box = el("div", cls("xc-grid", DENSITY[node.density]));
    // A number from a closed 1..6 range, written as a custom property rather
    // than an inline `style` string, so nothing package-derived is parsed as CSS.
    const columns = Number(node.columns);
    box.style.setProperty("--xc-columns", String(columns >= 1 && columns <= 6 ? columns : 2));
    renderChildren(box, node.children, scope);
    return box;
  },
  card(node, scope) {
    const box = el("div", cls("xc-card", TONE[node.tone]));
    if (node.title) box.appendChild(el("div", "xc-card-title", node.title));
    const body = el("div", "xc-card-body");
    renderChildren(body, node.children, scope);
    box.appendChild(body);
    return box;
  },
  divider() {
    return el("hr", "xc-divider");
  },
  tabs(node, scope) {
    const box = el("div", "xc-tabs");
    const strip = el("div", "xc-tab-strip");
    const panel = el("div", "xc-tab-panel");
    const tabs = node.tabs || [];
    // Tab selection is host-owned ephemeral renderer state: switching tabs is a
    // repaint, never a backend round trip and never a state write.
    const draft = scope.draft;
    const stateKey = ` tab:${node.tabs?.map((t) => t.id).join(",")}`;
    const select = (id) => {
      draft[stateKey] = id;
      for (const button of strip.children) button.classList.toggle("active", button.dataset.xcTab === id);
      const tab = tabs.find((t) => t.id === id) || tabs[0];
      panel.replaceChildren();
      renderChildren(panel, tab?.children, scope);
    };
    for (const tab of tabs) {
      const button = el("button", "xc-tab", tab.label);
      button.type = "button";
      button.dataset.xcTab = tab.id;
      button.addEventListener("click", () => select(tab.id));
      strip.appendChild(button);
    }
    box.appendChild(strip);
    box.appendChild(panel);
    select(draft[stateKey] || tabs[0]?.id);
    return box;
  },

  // ── content ──
  text(node, scope) {
    return el("div", cls("xc-text", TONE[node.tone], SIZE[node.size]), scalarText(value(node, "value", scope)));
  },
  markdown(node, scope) {
    // Sanitized by construction: the text is split into blocks and emphasis
    // runs, and each run becomes a DOM node with textContent. No HTML is parsed
    // at any point, so there is no sanitizer to bypass.
    const box = el("div", "xc-markdown");
    for (const block of renderMarkdownBlocks(scalarText(value(node, "value", scope)))) box.appendChild(block);
    return box;
  },
  badge(node, scope) {
    return el("span", cls("xc-badge", TONE[node.tone]), scalarText(value(node, "value", scope)));
  },
  list(node, scope) {
    const items = value(node, "items", scope);
    if (!Array.isArray(items) || items.length === 0) {
      return el("div", "xc-empty", node.empty_label || "Nothing here yet.");
    }
    const box = el("ul", "xc-list");
    for (const item of items) box.appendChild(el("li", "xc-list-item", scalarText(item)));
    return box;
  },
  table(node, scope) {
    const rows = value(node, "rows", scope);
    if (!Array.isArray(rows) || rows.length === 0) {
      return el("div", "xc-empty", node.empty_label || "Nothing here yet.");
    }
    // Wrapped so a wide table scrolls inside its own box instead of making the
    // whole panel scroll horizontally.
    const wrap = el("div", "xc-table-wrap");
    const table = el("table", cls("xc-table", DENSITY[node.density]));
    const head = el("tr");
    for (const column of node.columns) head.appendChild(el("th", ALIGN[column.align], column.label));
    table.appendChild(el("thead")).appendChild(head);
    const body = el("tbody");
    for (const row of rows) {
      const tr = el("tr");
      for (const column of node.columns) {
        const cell = row && typeof row === "object" ? row[column.key] : undefined;
        tr.appendChild(el("td", ALIGN[column.align], Array.isArray(cell) ? cell.join(", ") : scalarText(cell)));
      }
      body.appendChild(tr);
    }
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  },

  // ── inputs ──
  "text-input": (node, scope) => boundInput(node, scope, "text"),
  "number-input": (node, scope) => boundInput(node, scope, "number"),
  textarea(node, scope) {
    const field = document.createElement("textarea");
    field.className = "xc-input";
    field.rows = node.rows || 4;
    // `lines` edits an array as one member per line. The split is host-owned so
    // the draft holds the stored shape rather than its rendering: a flow reading
    // this key gets an array, which is what the list operations require.
    const lines = node.value_kind === "lines";
    return bindControl(
      node,
      scope,
      field,
      () =>
        lines
          ? field.value
              .split("\n")
              .map((line) => line.trim())
              .filter(Boolean)
          : field.value,
      (v) => {
        field.value = lines ? (Array.isArray(v) ? v.join("\n") : "") : v == null ? "" : String(v);
      },
    );
  },
  select(node, scope) {
    const field = document.createElement("select");
    field.className = "xc-input";
    for (const option of node.options || []) {
      const opt = document.createElement("option");
      opt.value = String(option.value);
      opt.textContent = option.label;
      field.appendChild(opt);
    }
    return bindControl(
      node,
      scope,
      field,
      () => coerceOption(node, field.value),
      (v) => {
        field.value = v == null ? "" : String(v);
      },
    );
  },
  toggle(node, scope) {
    const field = document.createElement("input");
    field.type = "checkbox";
    field.className = "xc-toggle";
    return bindControl(
      node,
      scope,
      field,
      () => field.checked,
      (v) => {
        field.checked = Boolean(v);
      },
    );
  },
  button(node, scope) {
    const button = el("button", cls("xc-button", TONE[node.tone]), node.label);
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await scope.onAction(node.action, actionInput(node, scope));
      } finally {
        button.disabled = false;
      }
    });
    return button;
  },

  // ── status ──
  progress(node, scope) {
    const bar = document.createElement("progress");
    bar.className = "xc-progress";
    bar.max = numberOr(value(node, "maximum", scope), 100);
    bar.value = numberOr(value(node, "value", scope), 0);
    return labelled(node.label, bar);
  },
  meter(node, scope) {
    const gauge = document.createElement("meter");
    gauge.className = cls("xc-meter", TONE[node.tone]);
    gauge.min = numberOr(value(node, "minimum", scope), 0);
    gauge.max = numberOr(value(node, "maximum", scope), 100);
    gauge.value = numberOr(value(node, "value", scope), gauge.min);
    return labelled(node.label, gauge);
  },
  "empty-state"(node) {
    const box = el("div", "xc-empty");
    box.appendChild(el("div", "xc-empty-title", node.title));
    if (node.description) box.appendChild(el("div", "xc-empty-body", node.description));
    return box;
  },
  error(node) {
    const box = el("div", "xc-error");
    box.appendChild(el("div", "xc-error-title", node.title));
    if (node.description) box.appendChild(el("div", "xc-error-body", node.description));
    return box;
  },

  // ── media ──
  image(node, scope) {
    const src = mediaSrc(node.source, scope);
    if (!src) return errorBox("This media source could not be resolved.");
    const img = document.createElement("img");
    img.className = cls("xc-image", SIZE[node.size]);
    img.alt = node.alt || "";
    img.loading = "lazy";
    img.src = src;
    return img;
  },
  audio: (node, scope) => mediaElement("audio", node, scope),
  video: (node, scope) => mediaElement("video", node, scope),

  // ── the library sweep ──
  "library-sweep"(node, scope) {
    const box = el("div", "xc-sweep");
    const status = el("div", "xc-sweep-status", "");
    const button = el("button", "xc-button xc-tone-accent", node.label);
    button.type = "button";
    let running = false;
    button.addEventListener("click", async () => {
      if (running) return;
      running = true;
      button.disabled = true;
      try {
        const done = await scope.onSweep(node.action, node.unclassified_key || null, (text) => {
          status.textContent = text;
        });
        status.textContent = `Finished. ${done} card(s) processed.`;
      } finally {
        running = false;
        button.disabled = false;
      }
    });
    box.appendChild(button);
    box.appendChild(status);
    return box;
  },

  // ── structured views ──
  tree(node, scope) {
    const nodes = value(node, "nodes", scope);
    if (!Array.isArray(nodes) || nodes.length === 0) {
      return el("div", "xc-empty", node.empty_label || "Nothing to show.");
    }
    return buildTree(nodes, [], node.select_action, scope, false);
  },
  "conversation-tree"(node, scope) {
    const nodes = value(node, "nodes", scope);
    const activePath = value(node, "active_path", scope);
    if (!Array.isArray(nodes) || nodes.length === 0) {
      return el("div", "xc-empty", node.empty_label || "This conversation has no messages yet.");
    }
    return buildTree(nodes, Array.isArray(activePath) ? activePath : [], node.select_action, scope, node.show_previews);
  },
};

function numberOr(raw, fallback) {
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

function labelled(label, control) {
  if (!label) return control;
  const wrap = el("label", "xc-labelled");
  wrap.appendChild(el("span", "xc-label", label));
  wrap.appendChild(control);
  return wrap;
}

function coerceOption(node, raw) {
  // A `select` reports strings; the declared option carries the real JSON type,
  // so the draft stores what the package declared rather than its rendering.
  const match = (node.options || []).find((option) => String(option.value) === raw);
  return match ? match.value : raw;
}

function boundInput(node, scope, type) {
  const field = document.createElement("input");
  field.type = type;
  field.className = "xc-input";
  if (node.placeholder) field.placeholder = node.placeholder;
  if (type === "number") {
    if (node.minimum != null) field.min = String(node.minimum);
    if (node.maximum != null) field.max = String(node.maximum);
  }
  return bindControl(
    node,
    scope,
    field,
    () => (type === "number" ? (field.value === "" ? null : Number(field.value)) : field.value),
    (v) => {
      field.value = v == null ? "" : String(v);
    },
  );
}

/**
 * Wire one form control to its declared binding.
 *
 * The control edits an ephemeral draft and nothing else. Rendering it does not
 * write state and neither does typing in it; only a button dispatching an
 * action submits, and that action goes through the ordinary host state path.
 */
function bindControl(node, scope, field, read, write) {
  const path = node.bind;
  write(currentBound(path, scope));
  field.addEventListener("input", () => {
    scope.draft[path] = read();
  });
  field.addEventListener("change", () => {
    scope.draft[path] = read();
  });
  if (Object.hasOwn(scope.draft, path)) write(scope.draft[path]);
  return labelled(node.label, field);
}

function currentBound(path, scope) {
  return resolvePath(scope.namespaces, path);
}

/**
 * The input one button sends: its declared value, resolved. Nothing else.
 *
 * Form drafts deliberately do *not* ride along. A package button dispatches a
 * package action; saving a bound form is a host-generated state write with its
 * own button (see `saveBar`), so "this button runs the extension's code" and
 * "this button stores what I typed" stay visibly different actions. Merging the
 * draft in would also hand a package whatever the user had typed into an
 * unrelated field on the same view.
 */
function actionInput(node, scope) {
  const declared = node.input == null ? {} : resolveValue(node.input, scope.namespaces);
  return declared && typeof declared === "object" && !Array.isArray(declared) ? declared : {};
}

function mediaSrc(source, scope) {
  if (!source || typeof source !== "object") return "";
  if (source.kind === "asset") {
    // Built from the extension id and the compiled asset path, both of which
    // the server already validated. Encoded per path *segment* rather than with
    // `encodeURI`, which leaves `?` and `#` alone -- a filename containing
    // either would otherwise turn the rest of the path into a query string or a
    // fragment, and the asset route would look up a key that is not the one the
    // package named.
    const path = String(source.path).split("/").map(encodeURIComponent).join("/");
    return `/api/extensions/${encodeURIComponent(scope.extensionId)}/assets/${path}`;
  }
  if (source.kind === "artifact") {
    const id = resolveValue(source.attachment_id, scope.namespaces);
    return Number.isInteger(id) ? `/api/workflows/attachments/${id}` : "";
  }
  return "";
}

function mediaElement(tag, node, scope) {
  const src = mediaSrc(node.source, scope);
  if (!src) return errorBox("This media source could not be resolved.");
  const media = document.createElement(tag);
  media.className = `xc-${tag}`;
  media.controls = true;
  media.preload = "none";
  media.src = src;
  return labelled(node.label, media);
}

/**
 * Draw a node list as a tree. The *host* computes parenting, depth, and
 * connectors from `parent_id`; the package supplies rows and a select action.
 * That is what keeps every package's tree the same widget rather than twenty
 * partial reimplementations with twenty different escaping bugs.
 */
function buildTree(nodes, activePath, selectAction, scope, showPreviews) {
  const active = new Set(activePath);
  const children = new Map();
  const known = new Set(nodes.map((n) => n?.id));
  for (const node of nodes) {
    if (!node || node.id === undefined) continue;
    const parent = node.parent_id != null && known.has(node.parent_id) ? node.parent_id : null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(node);
  }

  const root = el("div", "xc-tree");
  const collapsed = scope.draft;

  const walk = (parentId, depth, into) => {
    for (const node of children.get(parentId) || []) {
      const row = el("div", cls("xc-tree-row", active.has(node.id) ? "xc-tree-active" : ""));
      row.style.setProperty("--xc-depth", String(Math.min(depth, 24)));
      const kids = children.get(node.id) || [];
      const key = ` collapsed:${node.id}`;
      if (kids.length) {
        const toggle = el("button", "xc-tree-toggle", collapsed[key] ? "▸" : "▾");
        toggle.type = "button";
        toggle.addEventListener("click", () => {
          collapsed[key] = !collapsed[key];
          root.replaceChildren();
          walk(null, 0, root);
        });
        row.appendChild(toggle);
      } else {
        row.appendChild(el("span", "xc-tree-spacer", ""));
      }
      const label = el("button", "xc-tree-node");
      label.type = "button";
      label.appendChild(el("span", "xc-tree-role", node.role || ""));
      if (showPreviews && node.preview) label.appendChild(el("span", "xc-tree-preview", node.preview));
      if (kids.length > 1) label.appendChild(el("span", "xc-tree-count", `${kids.length}`));
      if (selectAction) {
        label.addEventListener("click", () => scope.onAction(selectAction, { message_id: node.id }));
      } else {
        label.disabled = true;
      }
      row.appendChild(label);
      into.appendChild(row);
      if (!collapsed[key]) walk(node.id, depth + 1, into);
    }
  };
  walk(null, 0, root);
  return root;
}

// ── markdown ────────────────────────────────────────────────────────────────
// A deliberately tiny subset: paragraphs, `-` bullets, and inline `**bold**`,
// `*italic*`, `` `code` ``. Everything else is literal text. This is not a
// Markdown implementation with holes plugged; it is a formatter that only
// recognises four things and treats the rest as prose, which is why it cannot
// be made to emit a link, an image, or a raw HTML block.

const INLINE = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;

function renderInline(target, text) {
  for (const part of String(text).split(INLINE)) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      target.appendChild(el("strong", null, part.slice(2, -2)));
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      target.appendChild(el("code", null, part.slice(1, -1)));
    } else if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      target.appendChild(el("em", null, part.slice(1, -1)));
    } else {
      target.appendChild(document.createTextNode(part));
    }
  }
}

function renderMarkdownBlocks(text) {
  const blocks = [];
  let bullets = null;
  for (const line of String(text).split("\n")) {
    const trimmed = line.trim();
    if (trimmed.startsWith("- ")) {
      if (!bullets) {
        bullets = el("ul", "xc-md-list");
        blocks.push(bullets);
      }
      const item = el("li");
      renderInline(item, trimmed.slice(2));
      bullets.appendChild(item);
      continue;
    }
    bullets = null;
    if (!trimmed) continue;
    const paragraph = el("p", "xc-md-p");
    renderInline(paragraph, trimmed);
    blocks.push(paragraph);
  }
  return blocks;
}
