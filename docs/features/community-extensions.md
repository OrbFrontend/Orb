# Community Extensions

Orb can be extended by third-party packages that are **data, not code**. A
package is a manifest, some declarative flows, some component trees, and a few
safe assets. Orb reads them, validates them against a frozen contract, and
interprets them itself. Nothing in a package is ever imported, evaluated, or
executed — there is no JavaScript entry point, no Python hook, no install
script, no build step.

This page is for extension **authors**. If you want the design rationale, the
trust boundary, and the invariants the host enforces, read
[Community Extensions — Architecture](../architecture/community-extensions.md)
and [Community Writer Tools](../architecture/community-writer-tools.md) instead.

!!! note "Installing one"
    Settings → Extensions. Install from a local `.orbext` archive or from an
    HTTPS Git URL. Orb inspects the package first and shows you exactly what it
    will be allowed to do; nothing is installed until you approve that list.

---

## The shape of a package

```text
orb-extension.json      ← the only mandatory file
flows/
  score-scene.json
ui/
  inspector.json
assets/
  icon.webp
README.md
LICENSE
```

Only files the manifest **references** are compiled, stored, or served. You can
keep TypeScript sources, a `package.json`, or a build directory in the
repository; Orb never materializes them.

Package it by zipping the directory:

```sh
zip -r my-extension.orbext my-extension/
```

One wrapping directory is stripped, so both `my-extension/orb-extension.json`
and a root-level `orb-extension.json` work.

### Paths

Referenced paths are relative, normalized, UTF-8, and contained by the package
root. They are **case-sensitive** even on a case-insensitive filesystem, and two
paths that differ only by case are rejected for the whole package.

### Content digest

Every revision has a SHA-256 content digest over the selected files. It is the
revision's identity in the content store, and it is what an update compares
against. JSON is hashed in Orb's canonical encoding (sorted keys, no
whitespace), so reformatting a flow file does not change your package's identity
— but changing a value does.

---

## The manifest

```json
{
  "extension_api": 1,
  "id": "scene-meter",
  "name": "Scene Meter",
  "version": "1.0.0",
  "author": "Example Author",
  "description": "Tracks and displays scene tension.",
  "homepage": "https://example.invalid/scene-meter",
  "requires": {
    "operations": ["model.structured", "state.set", "ui.invalidate"],
    "components": ["meter", "stack", "text"]
  },
  "permissions": [
    { "capability": "context.read", "field": "draft" },
    { "capability": "model.call", "lane": "agent" },
    { "capability": "state.read", "scope": "conversation" },
    { "capability": "state.write", "scope": "conversation" },
    { "capability": "ui.contribute", "slot": "inspector" }
  ],
  "hooks": {
    "post_pipeline": { "flow": "flows/score-scene.json", "stage": "observe" }
  },
  "views": {
    "inspector": { "source": "ui/inspector.json" }
  },
  "placements": [
    { "slot": "inspector", "view": "inspector" }
  ]
}
```

`id` uses the lowercase grammar `[a-z0-9][a-z0-9_-]{0,63}`. It is your
namespace: your state, your config, your views, and your purge target all live
under it. It may not collide with a built-in workflow or another installed
package.

### `extension_api`

The compatibility boundary. `1` is the frozen base contract. `2` adds exactly
one thing: a Writer-tool contribution (see below). Declare the lowest version
that covers what you use — a v1 manifest declaring a v2 slot is rejected rather
than silently upgraded, and an older Orb reports a v2 package as *"needs a newer
Orb"* rather than as malformed.

### `requires` is checked, not trusted

The compiler walks your flows and views and derives the real requirement set
itself. Your `requires.operations`, `requires.components`, and `permissions`
must **cover** that derived set:

- Under-declaring is a validation error at install time. You will be told
  exactly which operation, component, or grant you left out.
- Declaring something unknown to this Orb build leaves the package installed but
  *unavailable*, with a diagnostic — that is the feature-detection mechanism.
- Putting a privileged operation behind `"when": false` does not hide it. All
  reachable branches are walked conservatively.

---

## Permissions

The unit of consent is a **grant**: a `(capability, parameter)` pair.
`state.write` on `conversation` and `state.write` on `character` are two
separate grants, approved and revoked independently.

| Capability | Parameter | What it allows |
|---|---|---|
| `context.read` | `field`: `input` | Read the effective user message for the turn. |
| `context.read` | `field`: `draft` | Read the post-writer draft. |
| `context.read` | `field`: `history` | Read a bounded active-path history window. |
| `context.read` | `field`: `character` | Read an allowlisted character text projection plus its tags. |
| `context.read` | `field`: `persona` | Read the active persona's name and description. |
| `conversation.tree.read` | `field`: `structure` | Read message-node metadata for every branch. |
| `conversation.tree.read` | `field`: `preview` | Also read previews from inactive branches. Requires `structure`. |
| `library.cards.read` | — | Enumerate the card library, and resolve a card named by action input into `ctx.character`. |
| `lorebook.read` | — | Read the lorebook of the conversation's world. |
| `direction_notes.read` | — | Read the active branch's direction notes. |
| `conversation.branch.activate` | — | Change the active branch. Action flows only. |
| `prompt.context.append` | `targets`: `director`, `writer` | Add a per-turn trailing context block. |
| `draft.replace` | — | Replace the draft once, in a post-transform hook. |
| `card.write` | `field`: `tags` | Replace the tags of the one card in the invocation's context. Requires `context.read` for `character`. |
| `model.call` | `lane`: `writer`, `agent` | Make your own bounded model calls on that endpoint. Costs tokens. |
| `state.read` / `state.write` | `scope`: `config`, `conversation`, `message`, `character` | Access **your own** namespaced slot in that scope. |
| `artifact.write` | — | Emit workflow attachments. |
| `network.request` | `origin` | Reach one exact declared origin through Orb's client. |
| `ui.contribute` | `slot` | Place a command or view in one exact slot. |
| `fragment_type.contribute` | — | Register namespaced fragment types. |
| `writer.tool.contribute` | — | Contribute the one Writer tool (API 2). |

Grants can be revoked at any time from the manager. Revocation takes effect
**immediately**, including in the middle of a running flow — the next privileged
operation fails. Entry points whose requirements are no longer covered stop
being published and are listed in your package's diagnostic instead of failing
halfway through.

### UI slots

`composer.menu`, `mobile.chat_actions`, `tools`, `inspector`,
`message.toolbar`, `message.after`, `artifact.body`, `workspace`,
`library.card_actions`.

You name a slot; you never name a selector, an element, or a position.

---

## Flows

A flow is a bounded sequence of host operations. There are no user-defined
functions, imports, recursion, cycles, or loops.

```json
{
  "flow_version": 1,
  "steps": [
    {
      "id": "score",
      "op": "model.structured",
      "lane": "agent",
      "prompt": { "$template": "Rate scene tension from 0 to 100.\n\n{{ctx.draft}}" },
      "output_schema": {
        "type": "object",
        "properties": { "tension": { "type": "integer", "minimum": 0, "maximum": 100 } },
        "required": ["tension"],
        "additionalProperties": false
      }
    },
    { "op": "state.set", "scope": "conversation", "path": "tension", "value": { "$ref": "steps.score.tension" } },
    { "op": "ui.invalidate", "view": "inspector" }
  ]
}
```

### Values

- `{"$ref": "ctx.draft"}` — read a value by validated path.
- `{"$template": "…{{ctx.draft}}…"}` — substitute **scalar** path values.
- Plain JSON literals, and arrays/objects containing either of the above.

Templates are path substitution and nothing else: no filters, no property
access beyond the resolver, no calls, no loops, no expressions, no formatting
arguments. Interpolating an array fails — use `list.join`, whose separator comes
from a closed host-owned set.

Readable namespaces are `ctx` (whatever your grants project), `input`
(validated action input), `host` (invocation metadata), `steps` (earlier named
steps), and `fragment` (reducer flows only). Forward references are rejected.

A path that resolves to nothing becomes a distinct `missing` sentinel rather
than `null`, so `exists` is unambiguous. The sentinel can be tested or replaced
by a `fallback`, but it can never be returned, stored, interpolated, or passed
to a host operation.

### Conditions

`when` takes a structured predicate AST: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`,
`exists`, `and`, `or`, `not`. Depth is capped at 8. Comparisons are type-strict
and total — an incomparable pair is `false`, never an error.

### Operations, and where each may run

| Operation | Allowed in |
|---|---|
| `if`, `return`, `text.concat`, `text.replace_literal`, `json.pick`, `json.merge`, `list.intersect`, `list.join`, `math.add`, `math.subtract`, `math.negate`, `math.clamp` | everywhere, including reducers |
| `state.get`, `state.set`, `state.delete`, `random.integer`, `random.choice`, `model.text`, `model.structured` | pre, post, action, recovery, writer tool |
| `http.request` | pre, post, action, recovery, writer tool |
| `ui.status`, `ui.toast`, `ui.invalidate`, `artifact.emit` | pre, post, action, recovery |
| `context.append` | pre-pipeline only |
| `draft.replace` | post-transform only |
| `card.tags.set`, `conversation.branch.activate` | actions only |

A reducer flow is the strictest profile: pure operations and `return`, nothing
else. A Writer-tool flow is next strictest: no UI, no draft, no artifacts, no
first-party writes, and no message-scoped state.

Notes worth knowing before you design around them:

- **`model.text` / `model.structured`** use the selected lane's transport,
  credentials, model, and parameters — but **not** Orb's system prompt,
  conversation prefix, attachments, or cached tools. Conversation data reaches
  your prompt only through granted `ctx` fields you interpolate yourself.
- **`state.*`** addresses only your own namespaced slot. Paths are validated
  segments, never raw SQLite JSON paths.
- **`card.tags.set`** takes no card argument. It writes `ctx.character` and only
  that card, and the host normalizes the result (trim, drop empties,
  case-insensitive dedupe, length and count caps).
- **`http.request`** goes to an exact declared origin through Orb's client,
  which validates every resolved address, follows redirects itself, and drops
  your headers on a cross-origin hop. Bodies may reference `{"$secret": name}`;
  the value is substituted inside the client and never becomes a flow value.
- **`random.*`** uses a per-invocation seeded PRNG. Regenerate replays the seed;
  reroll gets a new one.
- **`on_error`** defaults to `stop`. A step may declare `on_error: "continue"`
  only if it also declares a `fallback`.

### The invocation transaction

Everything your flow changes in Orb — state writes, draft replacement, context
blocks, attachments, branch activation, card tags, view invalidations, toasts —
is **staged** and committed only if the flow returns successfully. A failure at
any step discards all of it.

Model calls and HTTP requests are external and cannot be rolled back. They still
cannot leave a partial Orb mutation behind: a flow that stages a state write and
then gets a bad HTTP response leaves the prior state untouched.

### Hooks

- `pre_pipeline` — runs before the turn. Can add trailing context blocks and
  write state. Cannot see or replace a draft.
- `post_pipeline` with `"stage": "transform"` — sees the draft and may replace
  it once. Each successful transform feeds the next.
- `post_pipeline` with `"stage": "observe"` — sees the final immutable draft.

Community transforms run before every observer. Within a stage, built-ins go
first and community entries sort by extension id, so ordering does not depend on
install time. One extension failing discards only its own invocation; the turn
still completes.

### Actions

Named on-demand entry points, invoked by a command, a button in one of your
views, or a card action. They may declare `input_schema` and `output_schema`
from the closed local JSON Schema subset.

If an action's input schema declares a card identifier, Orb resolves that card
into `ctx.character` — and that also rebinds the `character` state scope to that
card. This is the only place package input selects an entity, which is why it
needs **both** `context.read` for `character` and `library.cards.read`.

---

## Views

A view is a component tree Orb renders. You never produce markup, a class name,
a selector, a URL, or an event handler; every package string becomes text.

Available components: `stack`, `grid`, `tabs`, `card`, `divider`, `text`,
`markdown`, `badge`, `meter`, `progress`, `table`, `list`, `tree`,
`conversation-tree`, `library-sweep`, `image`, `audio`, `video`, `button`,
`text-input`, `textarea`, `number-input`, `select`, `toggle`, `empty-state`,
`error`.

A view may read from your state, from a host resource, or from a declared
package asset. The five readable resources are `conversation.tree`,
`library.cards`, `lorebook.entries`, `direction.notes`, and `persona`, each
behind its own grant. A view inherits the requirements of everything it
references, so a view reaching an ungranted resource is blocked rather than
rendered empty.

Paginated resources hand back an **opaque cursor**. Treat it as a protocol
token: pass it back unchanged. A cursor you construct or edit is rejected.

`library-sweep` exists because the loop belongs to the host: you give it an
action id, a label, and the state key that marks a card done, and Orb owns the
page size, cursor walk, concurrency, stop condition, and progress display.

Bound form inputs collect a draft and submit through a host-generated state
write — the same grant check, lock order, and transaction a flow's `state.set`
uses.

### Convention: `views.config`

Name a view `config` and Orb shows it as your extension's settings panel in the
manager. It may bind only to the `config` state scope.

---

## Assets

Referenced assets must be inert media or text:

- Images: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`
- Audio: `.mp3`, `.ogg`, `.wav`, `.m4a`
- Video: `.mp4`, `.webm`
- Text: `.txt`, `.md`

Rejected outright: `.html`, `.htm`, `.xhtml`, `.js`, `.mjs`, `.cjs`, `.css`,
`.svg`, `.pdf`, `.wasm`, and browser fonts. Type is checked by leading bytes,
not by extension, and assets are served with a sniffed allowlisted MIME plus
`nosniff`.

---

## Contributions

### Fragment types

With `fragment_type.contribute`, declare namespaced Director fragment types
under `contributions.fragment_types`. A descriptor names a `config_schema`, a
`director_schema`, an optional `prior_context` and `writer_context` renderer, a
pure `reduce_flow`, and optional `config_view` / `value_view` component trees.

The stored `field_type` becomes `<your-extension-id>:<type-id>`. If your package
is later disabled or removed, existing values stay stored and visible but inert
— they are never coerced to another type or silently dropped.

### Writer tool (`extension_api: 2`)

The one narrow exception to *"community packages do not add tools to the main
pipeline"*. A v2 package may declare **one** Writer tool:

```json
{
  "extension_api": 2,
  "permissions": [{ "capability": "writer.tool.contribute" }],
  "contributions": {
    "writer_tool": {
      "id": "resolve_outcome",
      "label": "Resolve outcome",
      "description": "Resolve an uncertain action when success or failure should not be chosen by the Writer alone.",
      "flow": "flows/resolve-outcome.json",
      "input_schema": { "…": "…" },
      "output_schema": { "…": "…" }
    }
  }
}
```

Rules that shape the design:

- You do **not** declare a provider-facing function name. Orb derives it.
- The input schema carries a semantic request only. `draft`, conversation id,
  card id, and every other host fact are supplied by Orb, not by the model.
- Availability is not activation: installing publishes the binding, but the user
  still selects at most **one** active resolver across the whole install.
- At most one successful call per turn, then one continuation with tools off.
- Only chat endpoints returning standard structured `tool_calls` are eligible.
  On a text-completion endpoint the turn takes the ordinary no-tools path and
  the manager shows a diagnostic.
- Your description ships in the Writer's prompt every turn the tool is active,
  whether or not it is called — which is why it needs its own conspicuous grant
  and is capped at 600 characters.

---

## Limits

Package:

| Limit | Value |
|---|---:|
| Downloaded Git pack / archive | 50 MiB |
| Referenced files, decompressed | 25 MiB total |
| `orb-extension.json` | 1 MiB |
| One referenced asset | 10 MiB |
| Reachable tree entries | 512 |
| Path length | 240 bytes |
| Declared flow steps | 128 |
| Permissions / actions / views / placements / commands | 64 / 32 / 32 / 32 / 32 |
| Declared origins / secrets / fragment types | 8 / 8 / 16 |
| Component nodes per view, depth | 256, 12 |

Per flow invocation:

| Limit | Value |
|---|---:|
| Executed steps, nesting depth | 128, 8 |
| Model calls / HTTP requests | 2 / 4 |
| Model output | 4096 tokens, 1 MiB |
| HTTP body / response / timeout / redirects | 1 MiB / 5 MiB / 30 s / 3 |
| State per scope | 256 KiB |
| One context block / all blocks per target | 8 KiB / 32 KiB |
| Artifacts, each | 2, 10 MiB |
| Card tag writes | 1 (32 tags × 64 bytes) |
| `list.*` input array members | 256 |
| JSON depth / members / string | 32 / 1024 / 256 KiB |

---

## Lifecycle

| State | Meaning |
|---|---|
| **Installed** | Metadata and an active content digest exist. |
| **Load status** | `available`, `incompatible`, `invalid`, or `missing_content`. |
| **Enabled** | The user's per-extension switch. This is the only enablement gate. |

All three are independent, and permission grants are stored separately again. An
entry point runs only when the package is installed, available, enabled, and its
transitive grants are currently approved.

- **Updates** are manual and atomic. Any permission expansion requires fresh
  consent. A failed update leaves the previous revision fully active.
- **Rollback** keeps exactly one prior revision, and is itself inspected —
  restoring a revision must not restore a capability you have since revoked.
- **Uninstall** removes registration and secrets but leaves your namespaced data
  inert on disk, so a reinstall picks up where it left off.
- **Purge** is the separate destructive operation. It previews the exact rows it
  will delete and confirms against that preview.

Because a namespace is an id rather than a publisher identity, installing a
package whose id already has preserved data shows a specific warning: it will
regain access to that data if `state.read` is approved. Purge first if that is
not what you want. A Git URL plus a commit is provenance, not authorship — Orb
does not claim otherwise.

---

## Reference packages

Four complete examples live in `tests/extension_packages.py`, and
`scripts/build_example_extensions.py` writes the same bytes the test suite
asserts against to `dist/extensions/`:

| Package | Demonstrates |
|---|---|
| **Conversation Map** | command placement, the full-tree resource, a workspace view, branch activation |
| **Scene Meter** | a post hook and an action, plus a contributed Meter fragment type with config and value views |
| **Tag Librarian** | the library resource, a user-managed vocabulary, the renderer-driven sweep, a first-party card write |
| **API Artifact** | origin consent, a secret header, a byte response, artifact regeneration and reroll |

```sh
./scripts/build_example_extensions.py
```

Then install one from Settings → Extensions and read its manifest beside the
running behavior.

---

## What is not available

Deliberately, and not by omission: WASM or any general-purpose package code;
Director or Editor tools; new pipeline passes; background services, timers, or
startup jobs; extension-to-extension dependencies; arbitrary DOM, CSS, canvas,
iframes, or browser storage; SSH or private Git auth; wildcard network
permissions; general regular expressions; general list iteration (`map`,
`filter`, `reduce`); arbitrary database tables, SQL, filesystem paths, or HTTP
routes; and automatic update installation.

These are not TODOs with a permissive placeholder waiting behind them. Each
would need its own threat model and its own grant.
