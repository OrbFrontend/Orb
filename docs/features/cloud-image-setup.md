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
3. Open **Connections** and select **Add connection**.
4. Select your **Provider**.
5. Paste your **API key**.
6. Select **Test connection**.
7. Under **Styles**, open the style you want to render there and set its
   **Connection** to this provider.
8. Select a **Model**, or leave the provider's default, and a **Resolution**.
9. Select **Save**, and accept the privacy confirmation.

A connection holds the key and nothing else. What each image looks like — the
model, the resolution, the quality, whether a reference image goes with it — is
chosen per style. Two styles can therefore share one key and one bill while
rendering with different models, which is the usual reason to have more than one.

**Test connection** asks the provider for its model list and nothing else. It
never submits a generation, so it never costs anything. A successful test also
turns each linked style's **Model** field into a dropdown of the models that key
can actually reach.

Where a provider hosts more than images, the dropdown lists only the models that
generate them — Together AI publishes one catalogue of 271 models, of which 29
make images, OpenRouter publishes 337 of which 11 declare an image output, OpenAI
publishes 125 of which 6 make images, and NanoGPT publishes its 202 image models in
a different place from its 653 text ones. If a provider stops labelling its
catalogue, the dropdown falls back to the full list rather than showing you nothing.

OpenAI is the one that labels nothing at all: its catalogue entry carries a name, a
date and an owner, and no field that says what a model does. So the name is what
Orb reads, and a future image model called something unexpected would show up in
the longer fallback list rather than not at all.

On most providers, asking for the model list is also what proves your key works.
NanoGPT and OpenRouter hand out their catalogues to anyone, so **Test connection**
checks the key against a second free endpoint first — NanoGPT's usage endpoint and
OpenRouter's `/key`. Still free, still not a render.

Orb stores one key per provider. Switching provider keeps the key for the one you
left, so you can move back and forth without pasting keys again.

## Supported providers

All of these speak the same OpenAI-shaped `POST /v1/images/generations` contract.

| Provider | Verified | Notes |
|---|---|---|
| **xAI (Grok)** | Yes | Probed against the live API, including reference images. Default model `grok-imagine-image`. |
| **OpenAI** | Yes | Probed against the live API, including reference images. No seed and no negative prompt. Reports tokens rather than a cost. Default model `gpt-image-1`. |
| **OpenRouter** | Yes | Probed against the live API. One broker key across Google and OpenAI image models. No seed, no negative prompt, and no reference images on any model. Default model `google/gemini-2.5-flash-image`. |
| **Together AI** | Yes | Probed against the live API. Accepts a seed and arbitrary resolutions (any multiple of 16 up to 1792px). Reference images on its Kontext models only. Default model `black-forest-labs/FLUX.1-schnell`. |
| **NanoGPT** | Yes | Probed against the live API. 202 image models, including uncensored ones. Accepts a seed, a negative prompt, and any resolution. Reference images on many models but not all. Default model `cyberrealistic-xl`. |
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
  rehydrated. **Render details** says *Seed: not used*.
- **No steps, CFG, sampler, or scheduler.**
- **Aspect ratios, not exact pixels.** Orb picks the nearest ratio or size the
  provider accepts and notes it on the image when the match is more than about 2%
  off. Providers that take exact pixels — Together AI — get the resolution you
  asked for, snapped to the grid they accept. NanoGPT and OpenRouter are sent the
  resolution verbatim and pick the nearest size the chosen model supports — ask
  OpenRouter for 1024×576 and a Gemini model answers 1344×768 — which is why
  **Render details** records what actually came back rather than what was asked
  for.

Style prompts, the character appearance prompt, the camera, and the resolution
all still apply.

### When the model, not the provider, is the limit

A capability is declared per provider, but some models on a provider ignore a
field the provider itself supports. Together AI accepts a negative prompt, yet
its distilled models — `FLUX.1-schnell` (the default) and
`Juggernaut-Lightning-Flux` — run without CFG and have nothing to apply one
with. They return the identical image whether you send one or not.

Orb still sends the field, and notes on the image that the model ignored it, so
a negative style prompt that is doing nothing says so instead of leaving you to
wonder. To have negative prompts take effect, pick a non-distilled model.

Together AI also honours a seed without guaranteeing it reproduces: the same
seed usually returns the same image, but not always. Treat it as influence over
the result rather than as a reproducibility guarantee.

NanoGPT is the same story across a much larger catalogue: `hidream-i1-fast`
returns the identical image whether you send a negative prompt or not, while
`cyberrealistic-xl`, `wai-illustrious-sdxl` and `rev-animated` honour both the
negative prompt and the seed exactly. Its resolution menu is per model too — a
size the chosen model does not list still renders, at the nearest size it does,
and on some models at a different price.

### On OpenRouter, the catalogue is not the contract

OpenRouter's image models advertise a `seed` parameter and an `image` input in
their catalogue entries. Both belong to its *chat* API. Image generation is a
separate path that silently accepts and drops anything it does not read, so the
catalogue describes a set of capabilities that endpoint does not have.

Measured rather than read: two renders at the same seed came back different on
both `gemini-2.5-flash-image` and `gpt-5-image-mini`, and a reference image sent
under all three field names OpenRouter's peers use — `image`, `images` and
`image_url` — was ignored by all three, returning a normal image billed at the
normal price. So Orb sends neither, and says so in the settings panel rather than
letting you watch a negative prompt do nothing.

What does work is the resolution and your style prompts. Cost comes back in plain
USD on every render.

The model list includes `openrouter/auto` and `openrouter/auto-beta`, which
declare an image output but have no image endpoint behind them. Choosing one
fails with OpenRouter's own *"No endpoint found"*. They are left in the list
rather than hidden, because a hand-maintained exception list rots against a
catalogue this size and the provider's own message already explains it.

### The same model can fail and then work

OpenRouter is a broker: one model id routes to several upstream providers, and
which one serves your request is decided per call. So a render can fail with an
upstream's restriction and the identical request succeed a minute later on a
different route. The one seen most often outside the US and EU is Google AI
Studio's:

> OpenRouter rejected the request (HTTP 400): Google AI Studio: User location is
> not supported for the API use.

Orb puts the upstream's name in front of the message, because OpenRouter reports
it and it is the fact that makes the failure actionable — the refusal is Google AI
Studio's, not OpenRouter's and not Orb's. If it repeats on one model, another
model in the picker routes elsewhere; `openai/gpt-5-image-mini` and the
`google/gemini-2.5-flash-image` default were both reachable when
`google/gemini-3.1-flash-image` was not. Retrying is also reasonable, and costs
nothing when the request is refused.

### On OpenAI, the resolution menu belongs to the older models

`gpt-image-1`, `gpt-image-1-mini` and `gpt-image-1.5` accept exactly three sizes —
1024×1024, 1024×1536 and 1536×1024 — and reject anything else outright, naming the
three in the rejection.

`gpt-image-2` does not. It takes any size whose edges are both divisible by 16, up
to a longest edge of 3840, above some minimum total pixel count: 1024×576 is refused
as "below the current minimum pixel budget" while 1024×1024 and 2048×2048 render.
Orb still snaps to the three-size menu on every OpenAI model, so a `gpt-image-2`
render is squarer and smaller than that model would allow. Nothing fails — the three
sizes are all valid there — but the picker is the older models' menu.

Two more things this provider does that the others do not:

- **Some models need your organization verified.** `chatgpt-image-latest` answers
  HTTP 403 *"Your organization must be verified to use the model"* until you verify
  at OpenAI's organization settings. The other five render without it.
- **No cost is reported.** OpenAI answers with token counts rather than a price, and
  a token count is not a bill, so **Render details** shows no cost row rather than a
  number Orb would have to make up a rate for. Check the usage dashboard.

`dall-e-2` and `dall-e-3` no longer appear in the catalogue at all.

### Content refusals are per model on NanoGPT

NanoGPT is a broker, so the policy that refuses you is the upstream model's, not
NanoGPT's — and how clearly it is stated varies with the model. For the same
prompt, `ideogram/v4/instant` answers `content_policy_violation` while `dall-e-3`
answers a generic *"Invalid request parameters"*. Orb relays whichever you get,
verbatim (see [When a render fails](#when-a-render-fails)), so the clarity of the
message is the model's, not Orb's. Its uncensored models — the SDXL and
Illustrious checkpoints — render what the smaller commercial APIs will not.

## Cost

**Render details** shows what the provider reported, in the provider's own unit.
xAI reports `usd_ticks` and does not document what a tick is worth, so Orb prints
`1400 usd ticks` rather than a dollar figure it cannot verify. NanoGPT and
OpenRouter report plain USD. When the response reports nothing Orb shows no cost row
rather than a zero: Together AI reports no cost at all, and OpenAI reports token
counts, which are not a price.

Every button that produces an image spends money: **Visualize**, **Regenerate**,
**Reroll**, and **Rehydrate**. Check your usage on the provider's dashboard;
Orb has no view of your balance.

## When a render fails

Orb does not paraphrase a provider. A failed render shows what the provider
answered, with the HTTP status in front of it — *"NanoGPT rejected the request
(HTTP 402): Insufficient credits"* — so the reason is the provider's own words
rather than Orb's guess at which category they fell into. Credentials and
internal paths are stripped, and the message is capped. Where the provider is a
broker relaying someone else's refusal, the upstream that actually refused is
named first.

You get the same sentence whichever button produced the render — **Visualize**,
**Regenerate**, **Reroll** or **Rehydrate**. Only a genuine Orb defect is reported
as *"see server logs"*, so a message you cannot act on is a bug worth reporting.

Commercial providers moderate prompts, and roleplay imagery is the case they
refuse most often. A refusal arrives the same way, quoting the provider's policy
message. There is no Orb-side setting that changes it: reword the scene, or use a
provider whose policy fits what you write.

## Reference images

Off by default. Turn it on under **Reference images** on the style, and pick what
Orb should feed the provider:

| Source | What Orb sends |
|---|---|
| **Previous image, else character reference** | The most recent image in the chat; if the chat has none, the character reference image. |
| **Previous image in the chat** | The most recent image in the chat. |
| **Character reference image** | The character reference image. |

Turning this on is a second, larger disclosure, and Orb asks for it separately
from the prompt-only one. Renders then route to the provider's image-edit
endpoint — or, where a provider has none, carry the image on the ordinary
generation call. Orb converts it to a format the provider accepts — generated
images are stored as WebP and most providers take only PNG and JPEG — and holds
a cloud reference to 4 MB, resizing and re-compressing as needed because the
image rides base64 inside a JSON request body. A reference that cannot be
brought under the limit fails the render rather than being sent oversized.

The image is always sent inline, as a `data:` URI. Orb never uploads it
somewhere first, and never hands a provider a URL that points back into Orb.

Not every provider in the table supports references. When the selected one does
not, the option has no effect and renders go to the plain generation endpoint.
OpenRouter is the one that looks like it should and does not — see
[On OpenRouter, the catalogue is not the contract](#on-openrouter-the-catalogue-is-not-the-contract).

### On Together AI, the model decides

Together has no image-edit endpoint, but its **FLUX.1 Kontext** models (`pro` and
`max`) accept a reference on the ordinary generation call, so references work
there. Its text-to-image models — including the `FLUX.1-schnell` default — do
not, and they disagree about how to say so: FLUX.2 and Seedream reject the
request, while `FLUX.1-schnell` returns a perfectly good image that ignored the
reference entirely.

So Orb checks the model, not just the provider. Choose a Kontext model and the
reference is sent; choose one that cannot take it and Orb sends no reference,
says so on the image under **Render details**, and warns you on the style row as
soon as you turn the option on — the model and the switch sit side by side there,
which is the whole point of both living on the style. Models Orb has not
verified are treated as unable to take a reference — under-promising costs you a
note, while over-promising costs a paid render that quietly leaves the character
reference out.

One thing the resolution picker cannot control: a Kontext render takes its size
from the reference image, so a 512×512 reference returns a square whatever the
picker says. The image notes this, and **Render details** records the size that
actually came back.

### On NanoGPT, check the model yourself

Around 118 of NanoGPT's 202 image models accept a reference — every model whose
catalogue entry is marked *image-to-image* or *both*, plus a number that are not.
The rest return a perfectly good image that ignored it, and bill for it. Orb
sends the reference to whichever model you chose rather than second-guessing a
catalogue that gains models every week, so the model name in the picker is the
thing to check. Anything with `edit`, `remix` or `kontext` in its name takes one;
so do the SDXL and Illustrious checkpoints. `flux-schnell`, `z-image-turbo` and
`fal-ai/krea-2/turbo` do not.

Size behaviour varies with the model as well: a dedicated edit model like
`step-image-edit-2` returns the reference's own dimensions, while the SDXL
checkpoints keep the resolution you picked.

The same is true when nothing resolves — a new conversation with no images yet
and no character reference. The render goes to the generation endpoint and the
image says so under **Render details**, so turning references on does not break
the first Visualize in every new chat.

## Switch a style back to ComfyUI

Open the style and set its **Connection** to **ComfyUI**, then save. Its
checkpoint and workflow are still pinned where they were, and so is its cloud
model for when you switch it back. Your imported workflows, camera setting,
character prompts and API key are untouched.

Because the connection is per style, you can move one style at a time — keeping a
cloud style for scenes your local checkpoint handles badly, or the reverse.

Imported ComfyUI workflows stay listed while a cloud provider is selected. They
are global, not per-backend, and hiding them would make a backend switch look
destructive when it is not.
