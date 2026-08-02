# Cloud Image Setup

Orb can render images through a cloud API instead of a local
[ComfyUI](comfyui-setup.md) server. There is no GPU, no model download, and no
workflow to import — you paste an API key and pick a model.

The trade is control and cost. A cloud provider exposes a fraction of what a
ComfyUI workflow does, it moderates your prompts, and every image is billed to
your account with that provider.

## Before you start

- An account with one of the providers below.
- An API key from that provider, enabled for image generation.
- A payment method on that account. Image generation is almost never free.

!!! warning
    Your scene prompts leave this machine and go to a third-party commercial API.
    If you turn on reference images, images from your conversations and your
    character reference photo go with them. The provider may retain what you send
    under its own retention policy. Orb asks you to confirm this before it saves.

## Connect a provider

1. Open **Workflow** and select the **Secondary** tab.
2. In the **Image Generation** card, select **Settings**.
3. Under **Backend**, select **Cloud API**.
4. Select your **Provider**.
5. Paste your **API key**.
6. Select a **Model**, or leave the provider's default.
7. Select a **Resolution**.
8. Select **Test connection**.
9. Select **Save**, and accept the privacy confirmation.

**Test connection** asks the provider for its model list and nothing else. It
never submits a generation, so it never costs anything. A successful test also
turns the **Model** field into a dropdown of the models that key can actually
reach.

Orb stores one key per provider. Switching provider keeps the key for the one you
left, so you can move back and forth without pasting keys again.

## Supported providers

All of these speak the same OpenAI-shaped `POST /v1/images/generations` contract.

| Provider | Verified | Notes |
|---|---|---|
| **xAI (Grok)** | Yes | Probed against the live API, including reference images. Default model `grok-imagine-image`. |
| **OpenAI** | No | `gpt-image-1`. Uses `size` rather than aspect ratios. |
| **OpenRouter** | No | Model catalogue varies by account. |
| **Together AI** | No | The one provider in this table that accepts a negative prompt *and* a seed. |
| **NanoGPT** | No | |
| **Chutes** | No | |
| **Z.AI** | No | |
| **AI/ML API** | No | |
| **ElectronHub** | No | |
| **Custom (OpenAI-compatible)** | No | Enter your own **API base URL**. Use this for a self-hosted or proxied endpoint. |

"Verified" means someone has probed that provider's live API and corrected Orb's
declared settings against what it actually accepts. An unverified row is Orb's
reading of that provider's published documentation; the settings panel says so
under the provider picker. If one is wrong, the fix is a single row in
`backend/workflows/image_gen/engine/providers.py`.

### A custom base URL

Select **Custom (OpenAI-compatible)** and enter the base URL, including the
version path — for example `https://api.example.com/v1`.

Orb requires `https`, and refuses a URL that carries a username or password. The
one exception is a loopback host (`http://127.0.0.1:8080/v1`), for a proxy running
on this machine, where there is no network for the key to cross.

## What you give up

The settings panel states this for the provider you selected. In general:

- **No negative prompt.** Orb tells the prompter not to write one, so no model
  effort is spent on it. The negative prompt is still recorded on the image, so
  replaying it on ComfyUI later is correct.
- **No seed.** Orb still stores one, because an image with no seed can never be
  rehydrated. **Render details** says *not used by this provider*.
- **No steps, CFG, sampler, or scheduler.**
- **Aspect ratios, not exact pixels.** Orb picks the nearest ratio or size the
  provider accepts and notes it on the image when the match is more than about 2%
  off.

Style prompts, the character appearance prompt, the camera, and the resolution
all still apply.

## Cost

**Render details** shows what the provider reported, in the provider's own unit.
xAI reports `usd_ticks` and does not document what a tick is worth, so Orb prints
`1400 usd ticks` rather than a dollar figure it cannot verify. When the response
reports nothing, Orb shows no cost row rather than a zero.

Every button that produces an image spends money: **Visualize**, **Regenerate**,
**Reroll**, and **Rehydrate**. Check your usage on the provider's dashboard;
Orb has no view of your balance.

## Content refusals

Commercial providers moderate prompts, and roleplay imagery is the case they
refuse most often. When that happens Orb reports it as a refusal —
*"xAI (Grok) refused this prompt under its content policy"* — rather than as a
generic failure. There is no Orb-side setting that changes it. Reword the scene,
or use a provider whose policy fits what you write.

## Reference images

Off by default. Turn it on under **Reference images** in the **Connection**
section, and pick what Orb should feed the provider:

| Source | What Orb sends |
|---|---|
| **Previous image, else character reference** | The most recent image in the chat; if the chat has none, the character reference image. |
| **Previous image in the chat** | The most recent image in the chat. |
| **Character reference image** | The character reference image. |

Turning this on is a second, larger disclosure, and Orb asks for it separately
from the prompt-only one. Renders then route to the provider's image-edit
endpoint with the image sent inline. Orb converts it to a format the provider
accepts — generated images are stored as WebP and most providers take only PNG
and JPEG — and holds a cloud reference to 4 MB, resizing and re-compressing as
needed because the image rides base64 inside a JSON request body. A reference
that cannot be brought under the limit fails the render rather than being sent
oversized.

Not every provider in the table supports references. When the selected one does
not, the option has no effect and renders go to the plain generation endpoint.

The same is true when nothing resolves — a new conversation with no images yet
and no character reference. The render goes to the generation endpoint and the
image says so under **Render details**, so turning references on does not break
the first Visualize in every new chat.

## Switch back to ComfyUI

Select **External ComfyUI** under **Backend** and save. Your styles, imported
workflows, camera setting, and character prompts are all still there, and so is
your cloud API key for when you switch back.

Imported ComfyUI workflows stay listed while a cloud provider is selected. They
are global, not per-backend, and hiding them would make a backend switch look
destructive when it is not.
