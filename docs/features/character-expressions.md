# Character Expressions

Character Expressions shows an image that matches the emotion of a reply.
You supply the images for each character.

## Set up expressions

1. Open **Settings → Expression Playback**. Download **Character Expressions**
   and turn it on.
2. Make a ZIP of images named after emotions. For example:

   ```text
   neutral.png
   joy.png
   anger.webp
   sadness.jpg
   ```

3. Open the character editor → **Advanced → Expression Images → Upload .zip**.

Use any subset of the labels below. Missing emotions use your `neutral` image,
then the normal avatar if you have no neutral image.

PNG, JPG/JPEG, WebP, and GIF are supported. Names ignore case; folders are
ignored. Uploading a new ZIP replaces the set. **Clear** removes it.

Limits: 200 files, 5 MB per image, 50 MB per ZIP.

## Supported labels

Use these names before the file extension:

`admiration`, `amusement`, `anger`, `annoyance`, `approval`, `caring`, `confusion`,
`curiosity`, `desire`, `disappointment`, `disapproval`, `disgust`, `embarrassment`,
`excitement`, `fear`, `gratitude`, `grief`, `joy`, `love`, `nervousness`, `optimism`,
`pride`, `realization`, `relief`, `remorse`, `sadness`, `surprise`, `neutral`

## View expressions

A halo around the avatar means the character has expression images. Select the
avatar in the chat header and keep the popup open to see expressions change.

In group chats, select the group avatar. The popup follows the current speaker,
or the last speaker while idle.

## Text rendering

Choose **Text rendering** in **Settings → Expression Playback**:

- **Classic** (default): uses normal text streaming. Expressions change while
  the popup is open.
- **Expression-based**: waits for the finished reply, including Editor changes,
  then reveals it in parts with matching images. Sentences using the same image
  stay together.

The first part appears automatically. Select **Next · Space**, or press **Space**
outside text inputs, to reveal more. Earlier text stays visible. Group replies
play in speaker order, and the last expression stays until the next turn.

Expression-based rendering keeps **Hide until baked** on. The full reply is
saved even while parts are hidden.

Characters without expression images and replies containing HTML or fenced code
appear as one part. If emotion detection fails, the whole reply appears.
Switching chats, starting a turn, or editing the reply ends playback.
