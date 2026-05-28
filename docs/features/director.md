# Director

Customizable prompt injection that's automatically used by the Director model.

The Director reads the room then fills in every enabled fragment on each turn via its tool call; the collected values form a **Scene Direction** block that is prepended to the Writer's context as post-history instructions.

## Mood Fragments

These are basically SillyTavern's user-defined macros, except they will be automatically managed by the Director agent.

Each mood fragment has:

- **ID** — the tool param name. Keep it short, no spaces or special characters.
- **Label** — display name shown in the UI.
- **Description** — help text that tells the Director *when* to use the mood.
- **Prompt text** — injected into the Scene Direction block when the mood is **active**.
- **Negative prompt** — injected when the mood was active last turn but has just been **deactivated**, so the Writer knows to dial it back.

You can create, edit, enable/disable, and delete moods. The defaults cover writing style, but nothing stops you from using moods for content rules or any other instruction you want toggled dynamically.

---

## Director Fragments

These can be compared to the status tracking blocks you'd see in some sophisticated character cards. But rather than reflecting what already happened, they act as a forward-looking game plan that shapes what the Writer produces.

Each director fragment has:

- **ID** — the tool param name. Keep it short, no spaces or special characters.
- **Label** — the key shown inside the Scene Direction block.
- **Description** — help text that tells the Director *how* to fill in the field.
- **Required** — if checked, the Director is instructed that it must always provide a value. Optional fragments may be left blank.
- **Injection label** — a separate label used when rendering the fragment in the Scene Direction block (defaults to the main label if left blank).

### Ordering and precedence

Fragments can be reordered; precedence runs top-down and the Director tries to follow this ordering strictly. A fragment that appears earlier impacts how latter fragments are generated, like a one-way train of thought. Drag the handle on any fragment row to change its position.

### Data types

- **Single** — a plain text value. Rendered as `Label: value`.
- **List** — a collection of plain text values. Rendered as a bullet list under the label.
- **Progressive** — a text value that persists across turns. Both the Director and Writer can see the previous turn's value alongside the new one, rendered as `Label: old value → new value`. Useful for incremental stat tracking.
