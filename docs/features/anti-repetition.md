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

**Subject fixation** cuts repeated descriptions and incidental mentions from
narration across **20 subject categories**, such as eyes, hair, hands, and scenery.

It is **off by default**. Enable **Agent**, **Output Auditor**, and its **Subject
fixation** option. Under **Local ML**, download and enable **Subject Analyzer**;
the download includes **2 required models**.

### Detection rules

The history windows and probability thresholds are fixed:

| Check | History | Minimum model scores |
| --- | --- | --- |
| **Repeated descriptions** | At least **2 of the last 8 replies**. | Description probability **≥ 50%** in the draft and 2+ previous replies; repeated-detail probability **≥ 60%** against 2+ previous replies. |
| **Recurring subjects** | **All 4 previous replies**. | Presence probability **≥ 70%** in the draft and each of those 4 replies. Actions and descriptions both count. |

Description repeats must concern the same character, object, or scene feature;
paraphrases count. Recurring subjects can trigger despite new wording or details.

History uses assistant replies only, limited to the current speaker in group
chats. Regeneration excludes the reply being replaced.

### What changes

The Editor makes small narration edits, preserving dialogue, key actions, and new
information:

- Repeated description: `Her copper braid gleams as she opens the door` →
  `She opens the door`.
- Recurring gesture: `She drums her fingers while waiting` → `She waits`.

The reply stays unchanged if no useful cut is possible.
