# Image Generation

Orb creates an image for an assistant reply when you request one. It uses either
an external ComfyUI server or a cloud image provider.

| Backend | You need | Cost |
|---|---|---|
| **External ComfyUI** | A running server, checkpoint, and imported workflow | Your hardware and electricity |
| **Cloud API** | A provider account and API key | Provider charges for each image |

Start with [ComfyUI Setup](comfyui-setup.md) or
[Cloud Image Setup](cloud-image-setup.md) if you have not connected a backend.

## Before you start

You need:

- A working LLM endpoint in Orb
- A reachable ComfyUI server with a checkpoint and workflow, or a cloud provider
  connection

The Agent model reads the conversation up to the selected reply and writes the
scene part of the image prompt. It also uses the selected style and visible
character appearance settings. The image backend receives the final positive and
negative prompts, not the conversation, character card, or scene analysis.

## Enable image generation

1. Open **Workflow**.
2. Select **Secondary**.
3. Turn on **Image Generation**.
4. Select **Settings** in the Image Generation card.

## Connect an image backend

Image settings contain **Connections** and **Styles**. A connection describes how
Orb reaches a backend. A style describes how the image should look and points to a
connection. Each style can use a different connection.

### ComfyUI

1. Open **Connections** and select **ComfyUI**.
2. Enter the server URL, normally `http://127.0.0.1:8188`.
3. Enter a Bearer-token API key if the server requires one.
4. Import a workflow and assign it to each style.
5. Select a checkpoint for styles whose workflow allows Orb to replace the model.
6. Select **Test connection**, then **Save**.

The connection test checks each assigned workflow and its required nodes, models,
and image files. A ComfyUI connection without an imported workflow cannot render.

### Cloud provider

1. Open **Connections → Add connection**.
2. Choose a provider and enter its API key.
3. Select **Test connection** to load the provider's models.
4. Assign the connection to a style.
5. Choose a model, resolution, and any supported quality or reference-image options.
6. Select **Save** and accept the privacy confirmation.

Testing does not generate an image or charge for one. The provider's model,
resolution, quality, and reference-image settings belong to the style, so styles
can share one connection while using different settings.

Cloud providers may ignore negative prompts, seeds, steps, CFG, samplers, or
schedulers. They may also map the requested resolution to a supported size.
**Render details** shows what the provider used and any cost it reported.

!!! warning
    A cloud provider receives the prompt and any reference images you enable. A
    remote ComfyUI server receives uploaded reference images too. Provider
    billing and data-retention policies apply.

## Import a ComfyUI workflow

Orb accepts:

- An API-format JSON workflow
- A ComfyUI output PNG that contains workflow metadata

A regular ComfyUI workflow JSON is not the API format. In ComfyUI, enable the
developer options and use **Save (API Format)** or **Export (API)**.

1. Open **Image Generation** settings.
2. Open **Imported ComfyUI workflows**.
3. Select the API-format JSON or metadata PNG.
4. Name the workflow.
5. Check the positive prompt, negative prompt, seed, image-output, and model slots.
6. Optionally map matching `width` and `height` inputs.
7. If the workflow has **Load Image** nodes, review the reference-image slots.
8. Select **Confirm slots and add workflow**.
9. Assign the workflow to a style and choose its checkpoint.
10. Set the style's **Reference image** option when the workflow needs an image.
11. Select **Test connection**, then **Save**.

### Resolution mapping

By default, the workflow controls its own output size. Map both `width` and
`height` when you want the style's **Resolution** setting to control a plain latent
node, such as **Empty Latent Image**.

Leave both unmapped when the workflow gets its size from a reference image,
aspect-ratio node, resolution helper, or a checkpoint-specific setup. Orb only
offers inputs literally named `width` and `height`, and both must be mapped for
the style's Resolution field to appear.

## Reference images

Reference images are off by default. Set the source on a style.

| Source | Cloud provider | ComfyUI |
|---|---|---|
| **Previous image, else character references** | Latest image in the chat; otherwise character references | Latest image in the chat; otherwise the reply speaker's character reference |
| **Previous image in the chat** | Latest image in the chat | Latest image in the chat |
| **Character references** | One image per character in the scene | The reply speaker's reference copied to each `Load Image` node |
| **Character references and the previous image** | Character references, then the latest chat image | Speaker's reference, then the previous image as fallback |

### Character reference images

1. Open a conversation with the character.
2. Open **Image Generation** settings and select **This Character Only**.
3. Choose a PNG, JPEG, or WebP under **Reference image**.
4. Select **Save**.

The limit is 10 MB. If no reference is saved, Orb uses the character card's
avatar.

### ComfyUI reference inputs

Orb fills the **Load Image** nodes already present in the workflow. It does not
add nodes. Every Load Image node receives the same source image, so a workflow
that needs two different inputs must express that distinction itself.

With **Reference image** set to **Off**, ComfyUI uses the filenames stored in the
workflow. Those files must exist on the ComfyUI server.

### References in a group chat

For a cloud provider, Orb sends at most one reference per character in the current
round, in cast order. The reply's speaker is included first. The provider may
limit the number of images; remaining characters are described in the prompt.

For ComfyUI, every Load Image node receives the reply speaker's reference. Orb
does not guess which node represents which character.

Orb considers the user message and replies in the current round up to the reply
you selected. It does not include later replies in the round. It looks back at
most 30 branch messages for a previous image.

Reference uploads must be PNG, JPEG, or WebP. Cloud references are limited to 4 MB
after preparation. A provider may accept fewer references or refuse to use them.

## Make an image

1. Open **Workflow → Secondary**.
2. Choose a style in the Image Generation card.
3. Find the assistant reply to visualize.
4. Select **Visualize reply**.

Orb shows whether it is composing the prompt, waiting in a queue, or rendering.
Select the image button again to cancel an active request. A submitted ComfyUI
job may continue in ComfyUI after Orb cancels it; check that queue before starting
another job.

## Variants and rerendering

Each image result is a variant of the selected reply.

| Action | Behavior |
|---|---|
| **Reroll** | Reuses the stored prompt and settings with a new seed when supported. |
| **Regenerate** | Composes a new prompt using the current style and character settings. |
| **Rehydrate** | Recreates an image whose stored bytes were evicted, using its saved prompt and seed. |

Each cloud action is a new provider request and may be billed. Providers that do
not use seeds return a new image for Reroll or Rehydrate.

Changing the selected style before a reroll uses that style's connection and
workflow. Orb keeps the stored prompt and records any backend or style change in
**Render details**. Old images keep the resolution used when they were made.

## Edit a prompt

1. Open **Render details** under the image.
2. Select the pencil beside **Prompt** or **Negative**.
3. Edit the text and click outside the field.
4. Select **Reroll**.

Orb uses your edited text and does not run the prompt-writing step again.
**Regenerate** creates a new prompt and ignores the manual edit.

## Character appearance prompts

Use **This Character Only** to add fixed appearance details for a character:

- **Positive prompt**: visible details such as hair, eyes, body shape, or usual clothes
- **Negative prompt**: character-specific features to avoid

Do not add a character-count tag such as `1girl`; Orb adds the count from the
scene. Style and scene prompts are separate from this character setting.

## Styles

Orb includes **Realistic** and **Anime** styles. Select **Add style** to make
another one. A style contains:

| Setting | Purpose |
|---|---|
| **Name** | Name shown in the style list |
| **Prompt format** | **Tags**, **Hybrid**, or **Prose** scene prompts |
| **Positive/negative style tags** | Visual details to include or avoid |
| **Extra instructions** | Guidance for the Agent prompt writer |
| **Connection** | ComfyUI or cloud connection |
| **Checkpoint / Workflow** | ComfyUI model and workflow |
| **Model / Resolution / Quality** | Cloud provider settings, where supported |
| **Reference images** | Images sent to the style's render target |

Tags and Hybrid formats use character-count tags such as `1girl` or `1boy`.
Prose does not. Match the format to the workflow's text encoder.

Switching a style's connection keeps its other saved settings. A style can retain
both its ComfyUI workflow and cloud model while you switch between them.

## Camera and point of view

The camera setting is next to the style picker and applies globally:

| Mode | View |
|---|---|
| **Auto** | A local classifier chooses first-person or third-person from the reply. |
| **First-person** | Shows the scene through the user's eyes; the user is not drawn. |
| **Third-person** | Shows the scene from outside and includes the user as a character. |

Orb uses the explicit picker choice first. In Auto mode it uses the classifier,
then falls back to third-person when the text is unclear or the classifier is off.

To enable the classifier, open **Settings → Local ML**, download **Image POV**,
and leave it enabled. It runs locally on the CPU.

## Analyze complex scenes

Enable **Analyze complex scenes** when a reply contains several characters or
important positions. Orb first identifies the visible characters, clothing, and
positions, then writes the image prompt from that result. This adds one model call
for each new image or regeneration. Reroll uses the stored prompt and skips it.

## Prompter thinking

**Enable prompter thinking** lets the Agent model reason before it analyzes a
complex scene or writes the image prompt. It applies to both prompt steps and can
increase token use. Stable thinking settings generally give better prompt-cache
reuse.

## Troubleshooting

| Problem | Try this |
|---|---|
| Image button is missing | Enable the workflow and select an assistant reply without an image. |
| **Import a ComfyUI workflow** | Import and assign a workflow to the selected style. |
| **Choose a checkpoint** | Select a checkpoint in each ComfyUI style that needs one. |
| Connection test fails | Check the URL, port, server status, firewall, API key, nodes, and models. |
| ComfyUI completes without an image | Select a valid image-output node during workflow import. |
| Render times out | Check the ComfyUI queue or increase **Render timeout** from 10 to 900 seconds. |
| Reference image is required | Create or upload an image, or set a character reference and choose a source that includes it. |
| Reference image is too large or unreadable | Use PNG, JPEG, or WebP and a smaller file. |
| Prompt generation fails | Check the LLM endpoint and use a model with tool calling. |
| **Bytes evicted** | Select **Rehydrate**. A cloud backend creates and bills a new image. |
| Provider rejects the key or request | Check the provider dashboard, account credits, model access, and the provider message. |
| Cloud image has the wrong shape | Choose a resolution closer to a ratio supported by that provider. |
| Seed says **not used** | The provider ignores seeds; this is expected. |

## Delete image data

Use the delete button in the image header to remove a variant or its variant
group. Read Orb's confirmation before continuing.
