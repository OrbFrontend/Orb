# Multimedia

Orb produces two things besides text: an image of the current scene, and spoken
audio of a character's dialogue. Both are per reply, and both are off until you
set up a backend.

## Images

Orb can turn an assistant reply into a picture of that scene. You request every
image on demand — nothing renders on its own.

Orb's Agent-lane LLM reads the conversation through the reply you picked and
writes the diffusion prompt. That prompt is all the image model receives; the
conversation, the character card, and the scene analysis stay behind.

| Backend | What it needs | What it costs | Set it up |
|---|---|---|---|
| **External ComfyUI** | A ComfyUI server you run, a checkpoint, and an imported API-format workflow | Your own hardware and electricity | [ComfyUI Setup](comfyui-setup.md) |
| **Cloud API** | An API key from a supported provider | Money, per image, billed by the provider | [Cloud Image Setup](cloud-image-setup.md) |

Orb does not install ComfyUI or image models.

The choice is per style, not global: each style names the connection it renders
on, so a local anime checkpoint and a commercial photorealistic API can sit one
dropdown apart in the same conversation. A style keeps both backends' settings,
so relinking it and relinking it back loses nothing.

[Image Generation](image-generation.md) covers styles, the camera, reference
images, variants, and manual prompt editing, and ends with a
[symptom table](image-generation.md#solve-common-problems) for renders that fail.

### How a render happens

1. You select **Visualize reply** on an assistant reply.
2. Orb settles the camera (POV) for that reply — from the picker, or from the
   local classifier in **Auto** mode.
3. The Agent model writes the scene prompt, after an optional complex-scene
   analysis pass.
4. Orb assembles the final positive and negative prompts from the style, the
   visible character profile, and the composed scene.
5. The style's connection renders it: your ComfyUI server, or the provider's API.
6. The image attaches to the reply as a variant. **Render details** records the
   style, seed, prompts, and which lever chose the camera.

Steps 3 and 4 run again on **Regenerate**. **Reroll** replays the stored prompt
with a new seed and skips them.

## Speech

Orb speaks a character's dialogue, not the whole reply: a local extractor pulls
the quoted lines out first and leaves narration and inner monologue unspoken.
Select the speaker icon on a message to hear it, select a single quoted line to
hear just that line, or turn on auto-speak for every new reply.

Microsoft Edge TTS ships in `requirements.txt` and needs no key, so speech works
out of the box. Kokoro-82M and Fish Speech run locally; OpenAI-compatible
endpoints and ElevenLabs are cloud services. Each character carries its own
voice, backend, speed, and pitch in the character editor's **Voice** tab, and the
global toggles live in **Settings**.

[Text-to-Speech](tts.md) covers each backend, the extraction pipeline, and how to
add a backend of your own.

## Start here

<div class="grid cards" markdown>

-   **[ComfyUI Setup](comfyui-setup.md)**

    You have a GPU. Install ComfyUI, grab a checkpoint, and import a working
    workflow — no cost per image.

-   **[Cloud Image Setup](cloud-image-setup.md)**

    You don't. Paste an API key and render — no GPU, no model download, billed
    per image.

-   **[Image Generation](image-generation.md)**

    Backend connected. Configure styles, the camera, reference images, and
    prompt editing, then make your first image.

-   **[Reference Image Setup](reference-images.md)**

    ComfyUI running. Add an edit workflow so a render can start from a picture
    and keep the character's face.

-   **[Text-to-Speech](tts.md)**

    Give a character a voice, pick a backend, and choose what gets spoken.

</div>

!!! warning
    Cloud providers are third-party commercial APIs, and a ComfyUI server that is
    not on this machine is a remote server. Your scene prompts, your reference
    images if you turn them on, and the dialogue sent to a cloud voice all leave
    this machine. Orb asks you to confirm before sending images or prompts to a
    third party.
