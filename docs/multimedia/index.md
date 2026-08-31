# Multimedia

Orb can create an image for a reply and read character dialogue aloud. Both are
optional and require a configured backend.

## Images

Orb generates images on demand. The Agent model reads the conversation up to the
selected reply and writes an image prompt. The image backend receives the final
positive and negative prompts, not the conversation or character card.

| Backend | You need | Billing |
|---|---|---|
| **External ComfyUI** | A ComfyUI server, checkpoint, and imported workflow | Your hardware and electricity |
| **Cloud API** | A provider account and API key | Provider charges per image |

Orb does not install ComfyUI or image models. Start with [ComfyUI Setup](comfyui-setup.md)
or [Cloud Image Setup](cloud-image-setup.md), then follow [Image Generation](image-generation.md).

Each style points to its own connection. You can use several local and cloud
styles in one conversation.

### Image workflow

1. Select **Visualize reply** on an assistant reply.
2. Orb chooses the camera from the global camera setting or the local POV classifier.
3. The Agent model composes the scene prompt.
4. The selected style adds its prompts and settings.
5. The chosen ComfyUI server or cloud provider renders the image.
6. Orb attaches the image as a variant and records its render details.

**Regenerate** writes a new prompt. **Reroll** reuses the stored prompt with a new
seed when the backend supports seeds.

## Speech

Orb extracts quoted dialogue from a character reply and sends that dialogue to a
text-to-speech backend. Narration and inner monologue are not spoken. Select the
speaker icon to read a message, select a quoted line to read only that line, or
enable auto-speak for new replies.

Microsoft Edge TTS is included and needs no API key. Kokoro-82M and Fish Speech
run locally. OpenAI-compatible services and ElevenLabs are cloud backends. Each
character has its own voice in the character editor's **Voice** tab.

See [Text-to-Speech](tts.md) for backend setup and voice options.

!!! warning
    Cloud providers and remote ComfyUI servers receive the data needed for their
    service. This can include scene prompts, reference images, or dialogue. Orb
    asks for confirmation before sending data to a third party.
