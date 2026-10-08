# Anti-repetition

Anti-repetition checks the Writer's reply and recent conversation for repeated
patterns. The **Editor** asks the Agent to revise a reply when a check finds a
problem.

It can detect:

- Reused paragraph structure
- Repeated sentence templates or subject mentions
- Consecutive sentences with the same opener, such as `He A. He B.`
- Distinctive phrases repeated in the reply or recent turns

For unwanted words, contrastive negation, and dialogue echoed from the user, see
[Anti-slop](anti-slop.md).

## Subject fixation

Enable **Subject fixation** under the Output Auditor to reduce repeated focus
in narration. It needs the Agent, Output Auditor, Subject Tagger local model and
a configured Judge.

The check distinguishes two cases:

- **Repeated descriptions:** the Judge confirms that the draft repeats a
  descriptive detail from at least two of the last eight assistant replies.
  Both passages must describe the same character, object or scene feature;
  paraphrases count, while a new detail or an action alone does not. The Editor
  removes the repeated detail and keeps useful actions and new information.
  For example, `Her copper braid gleams as she opens the door` can become
  `She opens the door`.
- **Recurring subjects:** the Subject Tagger finds a subject in the draft and
  all four previous assistant replies, whether described or used in an action.
  This case does not need a pairwise Judge confirmation. The Editor reduces
  incidental mentions and habitual gestures, while keeping mentions needed to
  understand important actions or new events. For example, `She drums her
  fingers while waiting` can become `She waits`; catching someone's hand to
  stop an attack still matters to the scene.

Both prompts use the same subject definitions. The Editor changes only
narration, leaves dialogue unchanged and uses small find-and-replace edits.
It may return no edits when removing a mention would lose important meaning.
In group chats, the history comes from the current speaker's own replies.
