# Text-to-Speech

Orb can turn character replies into audio. Playback controls are global; the
voice and speech settings belong to each character.

## Start listening

1. Open **Text-to-Speech** in the tools panel and choose **Settings**.
2. Under **Playback**, choose whether new speech should play automatically and
   set the volume.
3. Under **Message interaction**, choose whether clicking speaks a whole reply,
   one block, or nothing. You can also choose whether a click plays that unit or
   the whole reply, and whether Orb highlights words as they are spoken.
4. When a reply has audio, use its speaker control to play or pause it.

If you want audio generated for every reply from one character, turn on
**Auto-generate speech for this character's replies** in that character's voice
profile. This setting generates audio; **Play new speech automatically** controls
whether generated audio starts playing on its own.

## Choose a character's voice

Open a conversation, then open **Text-to-Speech → Settings**. The **Voice
profile** section applies to the current character. Choose a backend, language,
voice, and any options it offers. Use **Preview** before **Save**.

In a group chat, choose a **Cast member** first. Each reply uses the voice of the
character who wrote it; the selected cast member only controls which profile you
are editing.

## Available backends

The backends available in your installation are shown in the Voice profile.

| Backend | What it needs |
|---|---|
| Microsoft Edge TTS | An internet connection. No API key. |
| OpenAI-compatible | An API URL, and usually an API key, model, and voice. Works with compatible hosted or local endpoints. |
| Kokoro-82M | A running Kokoro service and its API URL. |
| Spark-TTS (built-in) | Optional local model downloads. No separate server; supports cloned voices. |
| Spark-TTS (sidecar) | A running Spark-TTS service and its API URL. |
| Fish Speech | A running Fish Speech service and, if required, its API URL or key. |
| ElevenLabs | An ElevenLabs API key and a voice. |

## Clone a voice

The built-in **Spark-TTS** backend can learn a character's voice from one audio
clip. Select it in the character's Voice profile. If Orb asks for downloads,
use the setup button in that panel.

Choose a mode:

- **Basic** copies the voice's timbre from the whole clip, up to two minutes.
- **Advanced** uses a short clear excerpt and can also follow its pacing and
  accent. If Orb asks for a transcript, type exactly what the excerpt says.

For either mode, use one speaker and avoid music or background noise. WAV and
FLAC work directly; other formats may need `ffmpeg`. Uploading a clip saves the
voice to that character and enables its speech profile. Drop another clip to
replace it, or choose **Remove** to forget the cloned voice and stop speech for
that character.

## What gets spoken

Orb picks dialogue from a reply and leaves out roleplay action and other text it
recognizes as narration. It understands quoted dialogue, em-dash dialogue, and
common roleplay markup. It can also speak plainly written dialogue when the
message's style is clear.

Narration is not read out, so Orb pauses in its place. The pause grows with
the length of the narration it replaces and never runs longer than three
seconds. A beat the voice can perform, such as a sigh or a laugh, is spoken
instead on backends that support it.

When a reply is ambiguous, the split between speech and narration may not be
perfect. The generated audio is shown on the reply, so you can listen before
relying on auto-play. Voice previews read the preview text literally.

Orb keeps the audio with the reply for replay. Word highlighting follows the
text that was actually synthesized, so replay does not have to guess the
dialogue boundaries again.
