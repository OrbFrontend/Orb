# Image Generation

Orb can generate an image of the current scene. You request each image on demand.

Orb uses a ComfyUI server to render the image. Orb does not install ComfyUI or
image models. For a quick, out-of-the-box setup, see [ComfyUI Setup](comfyui-setup.md).
For hardware requirements and advanced options, refer to the
[official ComfyUI documentation](https://docs.comfy.org/installation/system_requirements).

## Before you start

Make sure that you have these items:

- A working LLM endpoint in Orb
- A running ComfyUI server
- At least one checkpoint in ComfyUI
- Network access from Orb to the ComfyUI server

Orb's Agent-lane LLM reads the conversation through the reply you selected and
writes the scene portion of the image prompt. It also sees the saved style and
character prompt blocks so it can avoid duplicating or contradicting them. It
does not see the generated image.

For **Tags** and **Hybrid**, Orb starts the final positive prompt with booru
character-count tags, then combines the saved style prompt, visible character
profile, and composed scene. For **Prose**, Orb omits booru count tags entirely
and describes the visible cast naturally. It assembles the negative prompt from
the character exclusions, scene-specific exclusions, and style exclusions. The
image model receives only those final prompt strings; it does not receive the
conversation, character card, or scene analysis.

## Enable image generation

1. Open **Workflow**.
2. Select the **Secondary** tab.
3. Turn on **Image Generation**.

The **Image Generation** card shows the saved ComfyUI URL and the current
configuration status.

## Connect Orb to ComfyUI

First, this is what the UI look like (at least part of it):

![The Image Generation settings modal, showing a style's fields, Character appearance, and Imported ComfyUI workflows](../assets/screenshots/imagegen-settings-modal.png)

Now, time to connect:

1. Start ComfyUI.
2. In Orb, Open **Workflow** and select the **Secondary** tab.
3. In the **Image Generation** card, select **Settings** (or **Finish Setup** if first time).
4. Enter the ComfyUI URL.
5. Enter an API key if your server uses a Bearer token.
6. Select **Test connection**.
7. Make sure that the result is **Connected**.
8. Select **Save**.

Use `http://127.0.0.1:8188` when Orb and ComfyUI use the same computer and the
ComfyUI port is `8188`.

A connection is not enough to render. Orb has no built-in workflow: each style
renders through a ComfyUI workflow that you import and assign. Until you do, the
status stays **Import a ComfyUI workflow**.

!!! note
    **Test connection** checks every style that has a workflow assigned. Each of
    those workflows must be valid, and a checkpoint is required when the workflow
    lets Orb override the model.

## Import a ComfyUI workflow

Orb renders through ComfyUI workflows that you import. The ComfyUI server must
have all nodes and models that the workflow uses.

Orb accepts these files:

- An API-format JSON workflow
- A ComfyUI output PNG that contains workflow metadata

If you previously followed the tutorial in [ComfyUI Setup](comfyui-setup.md), import 
the .png file here.

A normal ComfyUI workflow JSON file is not an API-format file. In ComfyUI,
turn on the developer options. Then use **Save (API Format)** or **Export
(API)**. The exact label depends on your ComfyUI version. ComfyUI documents the
developer switch under [Comfy settings](https://docs.comfy.org/interface/settings/comfy).

To import the workflow, do these steps:

1. Open **Image Generation** settings.
2. Open **Imported ComfyUI workflows**.
3. Select the API-format JSON file or the ComfyUI PNG file.
4. Enter a name for the workflow.
5. Check the selected prompt, seed, image output, and model slots.
6. Select **Confirm slots and add workflow**.
7. In a style, select the imported workflow.
8. If your workflow is complex, review the nodes - make sure Orb points to the right node numbers.
9. Select a checkpoint if Orb must replace the model in the workflow.
10. Select **Test connection** to validate everything works.
11. Select **Save**.

If a PNG does not contain workflow metadata, export an API-format JSON file
from ComfyUI instead.

## Make an image

1. Open **Workflow** and select the **Secondary** tab.
2. In the **Image Generation** card, select a style.
3. Find the assistant reply that you want to show as an image.
4. Select the image button. Its tooltip is **Visualize reply**.
5. Wait for Orb and ComfyUI to complete the image.

Orb shows the current phase. The phase can be prompt composition, queue wait,
or rendering. If another render is before your render, Orb shows the number of
renders in front of it.

Select the image button again to cancel an active render.

## Use image variants

The image has two action buttons:

| Action | Result |
|---|---|
| **Reroll** | Orb uses the stored prompt and settings. It uses a new seed. See [Edit the prompt manually](#edit-the-prompt-manually). |
| **Regenerate** | Orb writes a new prompt. It uses the current style and character settings. |

Orb keeps each result as a variant. Use the left and right arrows to view the
variants. The counter shows the active variant.

Open **Render details** to see the style, seed, prompt, and negative prompt.
Select the style name in these details to edit that style.

Use the delete button in the image header to remove a variant or its variant
group. Read the confirmation message before you continue.

## Edit the prompt manually

The prompt that the prompter model wrote is not final. You can correct it and
render the correction.

1. Open **Render details** under the image.
2. Select the pencil next to **Prompt** or **Negative**.
3. Edit the text.
4. Select outside the field. Orb shows **Prompt edited — reroll to render**.
5. Select **Reroll**.

Orb renders your text as written. The prompter model does not run again.

**Reroll** also takes the style that is selected in the **Image Generation**
card at that moment, not the style that made the original image. The style
supplies the checkpoint and the ComfyUI workflow. Set the style you want before
you reroll.

Style tags are already baked into the stored prompt text, so a new style does
not reword it. When the style changes, Orb adds a note to **Render details** to
say that the prompt text still carries the previous style's wording. Edit the
tags in the prompt field yourself if you want them to match.

The edit stays with the attachment until you replace it, so a failed reroll does
not lose your text. **Regenerate** ignores the edit: it writes a new prompt.

## Set the character tags (Optional)

Allow user-defined appearance tags to always come with the character. A common use case 
is name of a non-OC character, some image models do better with canon character names.

1. Open a conversation with the character.
2. Open **Image Generation** settings.
3. Find **Per-character Prompt**.
4. Enter comma-separated appearance tags in **Positive prompt**.
5. Enter unwanted character features in **Negative prompt**.
6. Select **Save**.

Use fixed and visible details. For example, specify hair, eyes, body shape, and
usual clothes. Do not add a character-count tag such as `1girl`. Orb gets the
character count from the scene.

Negative tags apply only to this character profile. Style and scene negative
tags are separate.

## Change a style

Orb ships **Realistic** and **Anime** styles out of the box. A style contains these items:

| Item | Function |
|---|---|
| **Name** | Sets the name in the style list. |
| **Prompt format** | Chooses tags, hybrid tags and clauses, or prose for the scene portion. Tags and Hybrid use `1girl`/`1boy` count tags; Prose never does. Match this to the text encoder in the imported workflow. |
| **Positive style tags** | Adds visual properties near the start of the image prompt. |
| **Negative style tags** | Appends properties that ComfyUI must avoid. |
| **Extra instructions** | Gives composition or emphasis guidance to the prompter model. This is not copied into the image prompt. |
| **Checkpoint** | Selects the model file on the ComfyUI server. |
| **Workflow** | Selects the ComfyUI workflow for this style. |

The **Realistic** and **Anime** rows are seeded with starting tags that you can
edit or clear like those of any other style. Empty tag fields add no style tags.

To add a style, do these steps:

1. Open **Image Generation** settings.
2. Select **Add style**.
3. Enter a name and the style tags.
4. Select a checkpoint and a workflow.
5. Select **Test connection**.
6. Select **Save**.

You must keep at least one style.

## Set the camera (POV)

The camera decides whether the image looks *through* the user's eyes or *at* the
scene from outside. Orb decides it before it writes the prompt, then gives the
prompter one set of instructions written for that camera only.

Set it in the **Image Generation** card, next to the style picker. The choice is
global, like the style: it applies to every conversation until you change it.
Leave it on **Auto** if your chats are written in different persons.

| Mode | Result |
|---|---|
| **Auto** | The local POV classifier reads the reply. First- and second-person narration give the first-person camera; third-person narration gives the third-person camera. |
| **First-person** | Always through the user's eyes. The user is not drawn. |
| **Third-person** | Always from outside. Every person in frame is drawn, including the character the user plays. |

Orb decides the camera in this order. The first match wins:

1. The **First-person** or **Third-person** mode on the picker.
2. The classifier, in **Auto** mode. If a reply is too short or too mixed to read,
   Orb reads the previous assistant replies until one is clear.
3. Third-person.

A camera tag such as `first_person` in the character's **Positive prompt** no
longer sets the camera — use the picker.

Each image records which camera it used and which of the three levers chose it.
Open **Render details** under the image and read the **Camera** row. It tells you
where to go to change a camera you did not want: the POV picker or the classifier.

### Turn on the POV classifier

**Auto** needs a small local model. Without it, **Auto** falls back to
third-person and the picker says `Auto (classifier off)`.

1. Install the machine-learning extras. See [Character Expressions](character-expressions.md).
2. Open **Settings** and find **Local ML**.
3. Select **Download** on **Image POV**. The model is about 20 MB.
4. Leave the toggle on.

The model runs on the CPU inside Orb. It sends nothing to a server.

## Analyze a complex scene

Turn on **Analyze complex scenes** when a scene has multiple characters or
important positions. Orb first identifies the visible characters, clothes, and
positions at the final visible instant. Orb then converts that structured scene
to the selected prompt format. The second pass treats the extracted scene as
data, so dialogue or instructions inside the roleplay do not become prompt
instructions.

This option makes one additional LLM call for each new image or regenerated
image. A reroll uses the stored prompt and does not make this additional call.

## Enable prompter thinking

Turn on **Enable prompter thinking** when the Agent model benefits from reasoning
before it analyzes the scene or writes the diffusion prompt. The setting applies
to both prompt calls: the optional complex-scene analysis and the always-on prompt
composition call.

The prompter always uses Orb's Agent model lane. In single-model mode this is the
same endpoint and model as the Writer. In dual-model mode it uses the configured
Director/Editor endpoint, model, system prompt, and reasoning-effort setting.

Changing thinking mode can reduce prompt-cache reuse on providers that keep
thinking and non-thinking requests in separate cache lanes. Keeping the setting
stable gives the two prompter calls the best chance to reuse one another. Matching
the Editor setting may also improve reuse when an Editor-side call ran recently,
but provider behavior and the prompter's separate tool schemas mean this is not
guaranteed.

## Solve common problems

| Problem | Action |
|---|---|
| The image button is not shown. | Turn on the workflow. Use an assistant reply without an image. Make sure that this tab has write control. |
| The status says **Import a ComfyUI workflow**. | Import a workflow under **Imported ComfyUI workflows**. Assign it to each style. |
| The status says **Choose a checkpoint**. | Open each style whose workflow overrides the model. Select a checkpoint. |
| The connection test fails. | Make sure that ComfyUI is running. Check the URL, port, firewall, and API key. |
| Orb cannot find a checkpoint. | Add the checkpoint to ComfyUI. Restart or refresh ComfyUI. Test the connection again. |
| ComfyUI rejects the workflow. | Check that the server has all required nodes. Check the selected checkpoint and imported slots. |
| The render times out. | Check the ComfyUI queue. Increase **Render timeout**. The allowed range is 10 to 900 seconds. |
| ComfyUI completes without an image. | Select a valid image-output node in the imported workflow. |
| Orb cannot write an image prompt. | Check the Orb LLM endpoint. Use a model that can make tool calls. |
| An old image shows **Bytes evicted**. | Select **Rehydrate**. Orb uses the stored prompt, settings, and seed to make the image again. |

The ComfyUI queue can continue a submitted job after the browser disconnects.
If you cancel a render, check the ComfyUI queue before you start another render.
