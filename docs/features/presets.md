# Presets

A **preset** is a named generation setup you switch between from the picker under
the Orb title. Each preset holds:

- the pipeline switches: Agent, tools, Length Guard, agentic lorebook, individual
  Director fragments, Prevent prompt overrides, reasoning, and the Editor's audit
  checks
- the model each lane uses: the Writer's and the Agent's endpoint and model, and
  the Judge's endpoint and model
- which mood and Director fragments are on
- its own [Prompt Layout](prompt-layout.md)

Changes save to the active preset as you make them; there is no Save button.
Switching presets loads the other preset's setup in place.

Sampler values (temperature and the rest) belong to the model configuration, so
every preset that picks that model shares them. Your name, persona, display
options, Document mode, the assistant, workflows and Local ML are not part of a
preset.

## Manage presets

Next to the picker:

| Button | Action |
|---|---|
| **+** | A new preset with the current setup and the built-in layout |
| **Duplicate** | A copy of the current setup and the active preset's layout |
| **Rename** | Rename the active preset |
| **×** | Delete the active preset and its layout; Orb switches to the first one left |

The last preset cannot be deleted.

If the Writer's or the Agent's endpoint or model a preset uses is deleted,
switching to that preset leaves that lane as it is. A preset without a Judge, or
whose Judge endpoint was deleted, turns the Judge off. A fragment created after
the preset was last used keeps its current switch.
