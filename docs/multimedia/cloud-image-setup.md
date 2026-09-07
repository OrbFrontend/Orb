# Cloud Image Setup

Cloud image providers generate images without a local ComfyUI server. You need a
provider account, an API key, and billing or credits.

For general image settings, see [Image Generation](image-generation.md).

!!! warning
    The provider receives the image prompt and, when enabled, reference images.
    Its billing, moderation, and data-retention policies apply.

## Connect a provider

1. Open **Workflow → Secondary**.
2. In **Image Generation**, select **Settings**.
3. Open **Connections → Add connection**.
4. Select a provider and enter its API key.
5. Select **Test connection**.
6. Under **Styles**, assign the connection to a style.
7. Choose a **Model** and **Resolution**. Resolution offers known presets and also
   accepts a typed `widthxheight` value. Set **Quality** or **Reference images**
   when the provider supports them.
8. Select **Save** and accept the privacy confirmation.

Testing checks the key and loads the provider's models. It does not generate an
image or charge for a render.

The connection stores access details. Each style stores its own model, resolution,
quality, and reference-image settings, so several styles can share one key.

## Supported providers

| Provider | Notes |
|---|---|
| **xAI (Grok)** | Quality settings and reference images; fixed aspect ratios |
| **OpenAI** | Quality settings and reference images; no negative prompts or seeds; some models require organization verification |
| **OpenRouter** | Several vendors; no reference images, negative prompts, or seeds through its image endpoint |
| **Together AI** | Seeds and flexible sizes; reference images only with Kontext models |
| **NanoGPT** | Model capabilities vary; check the selected model |
| **AI/ML API** | No default model; image-to-image models are required for reference images. This integration is based on provider documentation. |
| **Custom (OpenAI-compatible)** | Works with a compatible image API; capabilities depend on the server |

The settings panel shows the capabilities Orb applies to the selected provider
and model. Check the first image's **Render details** as well.

## Fix a rejected request

Orb shows the message returned by the provider and does not parse that prose to
change rejected request parameters or retry a request-parameter refusal. Open the
affected style's **Compatibility** section when the provider says an optional field
is invalid:

- Turn off **Send a seed**, or enter the inclusive **Maximum seed** from the
  provider's documentation or error message.
- Turn off **Send negative prompts** while keeping the saved negative style text
  available for another model.
- Enter a supported **Resolution**, or choose one of the offered presets.

Reference images already have their own **Off — send prompts only** choice. Quality
is shown only where the provider exposes it, so neither setting is duplicated under
Compatibility.

## Custom endpoints

For **Custom (OpenAI-compatible)**, enter a base URL with its version path, such
as `https://api.example.com/v1`. Orb requires HTTPS, except for loopback URLs such
as `http://127.0.0.1:8080/v1`, and rejects URLs containing a username or password.

## References and billing

Reference images are off by default. See
[Reference images](image-generation.md#reference-images) for source choices and
limits.

Cloud APIs do not expose all ComfyUI controls. Negative prompts, seeds, steps,
CFG, samplers, and schedulers may be ignored. Providers may also map your
requested resolution to a supported size.

**Render details** records the request, returned size, and any cost reported by
the provider. The provider dashboard is the source of truth for billing.

Every action that creates an image is a new provider request, including Visualize,
Regenerate, Reroll, and Rehydrate.
