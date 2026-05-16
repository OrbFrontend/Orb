# Customizability

Customizable prompt injection that's automatically used by the Director model.

### Mood Fragments

These are basically SillyTavern's user-defined macros, except they will be automatically managed by the Director agent. The Director reads the room and decides the moods for the next reply. The final output will be heavily affected by this feature.

Currently the default moods only have writing styles, though the user may have other ideas.

### Director Fragments

These can be compared to the status tracking blocks you'd see in some sophisticated character cards. But rather than reflecting what already happened, they act as a forward-looking game plan that shapes what the Writer produces.

Fragments can be reordered; precedence runs top-down and the Director tries to follow this ordering strictly.

Data types:

- **Single**: A plain text value.
- **List**: A collection of plain text values.
- **Progressive**: A text value that persists across turns. Both the Director and Writer can see the previous turn's value. Useful for incremental stat tracking.
