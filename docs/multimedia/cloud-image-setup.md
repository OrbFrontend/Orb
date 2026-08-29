# Cloud Image Setup

Use a cloud provider when you want image generation without running ComfyUI or
downloading models. You provide an API key, choose a model, and pay the provider
for each image.

For general image-generation settings, styles, and prompt behavior, see
[Image Generation](image-generation.md).

!!! warning
    Cloud renders leave your machine. The provider receives the image prompt and,
    if enabled, a reference image. Its billing, moderation, and data-retention
    policies apply.

## Before you start

You need:

- An account with a supported provider
- An API key that can generate images
- Billing or credits on that account

## Connect a provider

1. Open **Workflow → Secondary**.
2. In **Image Generation**, select **Settings**.
3. Open **Connections** and select **Add connection**.
4. Choose a provider and paste its API key.
5. Select **Test connection**.
6. Under **Styles**, open a style and assign the new connection.
7. Choose a **Model** and **Resolution**. Set **Quality** or
   **Reference images** if the provider supports them.
8. Select **Save** and accept the privacy confirmation.

Testing a connection checks the key and loads available models. It does not
generate an image or incur a render charge.

A connection stores access details. Each style separately stores its model,
resolution, quality, and reference-image setting. This lets several styles share
one API key while using different models.

## Supported providers

| Provider | Good to know |
|---|---|
| **xAI (Grok)** | Supports quality settings and reference images. Uses fixed aspect ratios. |
| **OpenAI** | Supports quality settings and reference images. Does not use negative prompts or seeds. Some models require organization verification. |
| **OpenRouter** | Offers models from several vendors. Does not support reference images, negative prompts, or seeds through its image endpoint. The returned size may differ from the request. |
| **Together AI** | Supports seeds and flexible sizes. Reference images work only with Kontext models. Some fast models ignore negative prompts. |
| **NanoGPT** | Offers a large, changing model catalogue. Reference-image, negative-prompt, seed, and size behavior varies by model. |
| **AI/ML API** | Has no default model, so you must select one. Reference images require an image-to-image model. This integration is based on provider documentation and has not been verified against the live API. |
| **Custom (OpenAI-compatible)** | Use an image API that follows the OpenAI-compatible contract. Capabilities depend on the server. |

The settings panel shows the capabilities Orb applies for the selected provider.
When a capability varies by model, check the model description and your first
render's **Render details**.

### Custom endpoints

For **Custom (OpenAI-compatible)**, enter the base URL including its version path,
for example `https://api.example.com/v1`.

Orb requires HTTPS and rejects URLs containing a username or password. Plain HTTP
is allowed only for a loopback address such as `http://127.0.0.1:8080/v1`.

## Reference images

Reference images are off by default. For the sources, provider limits, privacy,
and reroll behavior, see [Reference images](image-generation.md#reference-images).

## Limits and billing

Cloud APIs expose fewer controls than ComfyUI. Depending on the provider, negative
prompts and seeds may be ignored, and the requested resolution may be mapped to a
supported size. Steps, CFG, sampler, and scheduler controls are not available.

**Render details** records what Orb sent, what size came back, and any cost the
provider reported. Not every provider reports a price, so use the provider's
dashboard as the source of truth.

Every action that creates an image is a new paid request, including **Visualize**,
**Regenerate**, **Reroll**, and **Rehydrate**.

Average fee: ~$0.08 per very high quality image. If a provider charges you more than this,
you're being ripped off.
