# Group Chats

A group chat puts several characters in one scene. They share the conversation,
Worlds, and premise, while each character keeps its own identity and voice.

## Create a group

Select **New group chat** and choose at least two characters. Their selection order
becomes the default speaking order.

Orb may recommend a Character context mode based on the number and size of the
cards. The recommendation is optional.

You can also select **Convert to group** from a solo conversation. The converted
scene starts with **Private perspective**.

## Scene setup

Select **+ Manage cast** to open **Scene setup**.

**Group settings** contains the title, character context, reply behavior, reply
limit, premise, style instructions, sheet updates, and delete action.

**Cast** contains the members, their order, speaking permissions, per-scene
profiles and sheets, and pending sheet proposals. Save applies changes from either
tab.

## Choose who replies

The cast chips above the message box control the floor. A muted member cannot
reply.

| Reply behavior | Result |
|---|---|
| **Auto — Director chooses** | The Director chooses speakers and their order. It may choose no reply. |
| **Rotate — Cast replies in order** | The next eligible member replies. |
| **Manual — Select every reply** | You select each speaker before sending. |

**Max replies per turn** limits the number of replies in Auto mode.

When nobody is streaming, selecting a chip gives that member the floor. During an
exchange, selecting a chip queues the member next; selecting it again removes the
queued choice. A manually chosen speaker is cleared after use unless an exchange
fails or is stopped.

## Choose character context

Character context controls what each character sees from the other cards:

| Mode | What characters see |
|---|---|
| **Private perspective** | Each character sees its own full card. Other characters are represented by their public profiles. |
| **Shared dossier** | Every character sees every card in full. |
| **Classic card swap** | The speaking character's full card is active; other characters use public profiles. |

All modes use one group premise. A card's scenario and system-prompt override do
not replace the group premise. Character names, linked Worlds, and card-embedded
fragments are shared by the scene. A character's post-history instructions apply
only to that character's replies. Group style instructions apply to everyone.

The [pinned persona](persona-pinning.md) applies to the whole group chat.
[Macros](macros.md) expand `{{cast}}` to the roster names and `{{char}}` to the
group title outside a member's card text.

## Public profiles and scene sheets

Private perspective and Classic card swap use a public **Appearance / Role**
profile for the other characters. Set it under **Customize for this scene**.
Use **Draft** to generate one from a member's card, or **Draft scene profiles** to
fill empty profiles. Review the text and select **Save**.

A **scene sheet** describes what a character is like in this scene. It replaces
that character's card description and personality for this group only. The card
itself is unchanged.

To allow Agent proposals for sheets, enable sheet updates in Group settings. Orb
stages at most one proposal per member after an exchange. Review proposals under
**Manage cast**, then **Apply** or **Reject**. Editing a sheet makes a waiting
proposal stale. Turning the option off stops new proposals but leaves existing
ones available for review.

## Branches and conversations

- **Regenerate** creates another reply by the same character at the same point.
- **Fork-edit** starts a new exchange from an edited user message.
- **Checkpoint** and **Compress History** keep the entire cast.
- Removing a member keeps old messages attributed to that member. Adding the same
  card later creates a new member identity.

The sidebar has one row for the group and its conversation branches. The row's
delete action removes the group and all its conversations. Use the composer menu
to start another conversation with the same cast.

## Other features in a group

| Feature | Group behavior |
|---|---|
| Director and direction notes | Run once for an exchange and guide all replies. |
| Lorebooks and Dynamic Worlds | Apply to the whole scene. |
| Card-embedded fragments | Merge across the cast. |
| Editor checks | Use each speaker's settings for that speaker's reply. |
| Image generation | Uses the reply's speaker and other members in the current round. |
| Text-to-speech | Uses the voice of the member who wrote each reply. |
| Character expressions | Follows the member who is speaking. |
