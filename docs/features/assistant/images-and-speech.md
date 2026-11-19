# Images and Speech

The assistant renders through the [Image Generation](../../multimedia/image-generation.md)
and [Text-to-Speech](../../multimedia/tts.md) workflows, so set those up first.
When one is switched off, the assistant says which switch; ask it to turn it on.

## Images in the panel

Ask for an image that is not of a reply: "create an image of this
character", "draw the tavern from her description". The assistant writes the
description itself and the image appears in the log. Click it to zoom.

- Name a character and its saved appearance tags and exclusions apply, plus its
  reference image when the style uses references.
- Ask for a change to an image ("same, but at night") and the assistant renders
  again from it.
- Point at an image in a chat ("the image on #12, but at night") and the
  assistant edits it. The prompt says only what changes, so this works best
  with an edit model. The style must take reference images; the assistant says
  which do.
- Name a style, or leave it to the default. "Which styles do I have?" lists them.

Nothing is attached to a card or chat. To use an image on a card, save it
from the browser and set it in the card editor. Orb keeps the most recent 32
images until it restarts, so save the ones you want.

## Illustrate a reply

"Illustrate this reply" renders an image for a reply, as its image button does.
Add what you want it to show ("from her side of the room, at dusk") and the
image follows your direction over the reply's moment, framing, and mood. The
image is saved on the reply and shown in the log. It can also edit an image in
a chat, as above. An image from the panel cannot be used for a reply; the
assistant renders the reply again with what the image shows as its direction.

To fix one render ("same image, but her skin is honey-colored"), the assistant
edits that image. The prompt says only what changes, so this works best with an
edit model.

## Read a reply aloud

"Read the last reply aloud" generates speech for a reply, as its speaker control
does, in the character's voice.

## What the assistant sees

The assistant sees the images it makes and the chat images it looks at, so it
can check a render and fix it on request. If the Agent model takes text only,
the assistant carries on without the images.

## While it renders

A render holds the assistant until it finishes, and progress shows in the log.
**Stop** in the panel cancels it. A render you started from a reply's own button
is left alone.
