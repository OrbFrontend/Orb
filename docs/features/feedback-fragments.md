# Feedback Fragments

A feedback fragment produces a short out-of-character note for you after the
reply. It does not change the reply or give instructions to the Writer.

Use feedback fragments for suggestions, reminders about open threads, pacing
comments, or other information that would not belong in the scene.

## Set one up

1. Create or edit an [interactive fragment](director.md#interactive-fragments).
2. Set **Field type** to **Feedback**.
3. Set its Description and optional Injection label.
4. In **Settings → Agents**, enable **Editor Feedback**.
5. Enable the fragment.

Orb runs all enabled feedback fragments after the Writer and Editor finish. The
notes appear in the **Inspector**. Each fragment becomes a separate note.

The global **Agent** toggle must be on. Feedback uses the existing prompt context
and adds a separate model step.
