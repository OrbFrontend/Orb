# Character Expressions

Character Expressions change a character's avatar to match the emotion detected
in the latest reply. The result is a still image, not an animation.

## Set up expressions

1. Open **Settings → Expression Playback** and download or enable **Character Expressions**.
2. Open the character editor and select **Advanced**.
3. In **Expression Images**, select **Upload .zip**.
4. Upload images whose filenames match supported emotion labels.

Filenames are case-insensitive and folders inside the ZIP are ignored. Uploading
another ZIP replaces the current set. **Clear** removes all expression images.

Orb accepts PNG, JPG/JPEG, WebP, and GIF files. A ZIP may contain up to 200 files;
each image may be 5 MB and the ZIP may be 50 MB.

## Supported labels

Orb uses the standard 28-label emotion set:

`admiration`, `amusement`, `anger`, `annoyance`, `approval`, `caring`, `confusion`,
`curiosity`, `desire`, `disappointment`, `disapproval`, `disgust`, `embarrassment`,
`excitement`, `fear`, `gratitude`, `grief`, `joy`, `love`, `nervousness`, `optimism`,
`pride`, `realization`, `relief`, `remorse`, `sadness`, `surprise`, `neutral`

You can upload any subset. Orb uses the matching image first, then `neutral.png`,
then the character's normal avatar.

## View expressions

A character with an expression pack has a halo around its avatar. Select the
character avatar in the chat header to open the expressions popup. While it is
open, Orb checks the latest reply about once per second, including while a reply
is streaming.

In a group chat, select the group avatar. The popup follows the member currently
speaking, or the last member who spoke when the chat is idle. A member without an
expression pack uses the normal avatar.

## Text rendering

In **Settings → Expression Playback**, choose **Classic** (the default) to keep
live text streaming and the popup's periodic expression updates.

Choose **Expression-based** to buffer generated replies until they are saved,
including any Editor rewrites. Orb classifies each sentence and groups adjacent
sentences that resolve to the same expression image. The first part appears
automatically. Select **Next · Space** or press **Space** outside text inputs to
reveal the next part and change the popup image together. Revealed text stays
visible. In group chats, playback advances through speakers in reply order.
The final expression holds until the next turn.

The full reply is saved normally; playback only controls its display. The mode
requires the expression model to be downloaded and enabled. Missing expression
images resolve to neutral or the normal avatar, so they do not create extra
pauses. Characters without an expression pack, structured HTML cards and fenced
code appear as one part. If classification fails, the complete reply appears.
Changing conversations, starting another turn, or editing a reply ends playback.
