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
in narration. It needs the Agent, Output Auditor, and the Subject Analyzer
under Local ML. Its download holds two models, the subject tagger and the pair
comparer, and the check needs both.

The check distinguishes two cases:

- **Repeated descriptions:** the subject tagger nominates a subject the draft
  and recent replies describe, and the pair comparer reads the draft against
  each of the last eight assistant replies to confirm that it repeats a
  descriptive detail from at least two of them. Both passages must describe the
  same character, object or scene feature; paraphrases count, while a new
  detail or an action alone does not. The Editor removes the repeated detail
  and keeps useful actions and new information. For example, `Her copper braid
  gleams as she opens the door` can become `She opens the door`.
- **Recurring subjects:** the subject tagger finds a subject in the draft and
  all four previous assistant replies, whether described or used in an action.
  This case does not need a pairwise confirmation. The Editor reduces
  incidental mentions and habitual gestures, while keeping mentions needed to
  understand important actions or new events. For example, `She drums her
  fingers while waiting` can become `She waits`; catching someone's hand to
  stop an attack still matters to the scene.

The Editor uses the same subject definitions for both cases. It changes only
narration, leaves dialogue unchanged and uses small find-and-replace edits.
It may return no edits when removing a mention would lose important meaning.
In group chats, the history comes from the current speaker's own replies.
When the pair comparer reads a draft, a `subject_fixation_repeats` entry
under **Tool Calls** lists its score for each nominated subject against each
recent reply, newest first.
