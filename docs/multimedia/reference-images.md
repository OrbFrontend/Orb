# Reference Image Setup

Tired of a different character generated every scene? Reference images are the solution.

This can be done via Cloud API or a local ComfyUI setup. But the requirement is that the
model and the workflow must be able to handle Loading Images.

This page only gets the local workflow running. For what Orb feeds it, and when, see
[Reference images](image-generation.md#reference-images).

## Before you start

- Finish [ComfyUI Setup](comfyui-setup.md) first.
- This continues from that page's local Krea 2 section, so it wants the same hardware —
  24GB+ VRAM.

## Install the custom node

The workflow uses nodes ComfyUI does not ship with. Install them:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/lbouaraba/comfyui-krea2edit
# restart ComfyUI
```

There is nothing else to install — the node pack needs no extra Python packages.

Do this before you import the workflow into Orb. Orb checks every node against
your server, if you get the error 'Krea2EditGroundedEncode', make sure you installed
the pack to the correct folder and restarted ComfyUI.

## Download the models

Three of these four files are the same ones the Krea 2 section already used. If
you did that section, you only need the LoRA.

1. **LoRA** — <https://civitai.red/models/2761113/krea-2-identity-edit>

    Put `krea2_identity_edit_v1_2.safetensors` in `ComfyUI/models/loras/`. This is
    the part that does the editing. Grab the biggest version.

2. **Checkpoint** — <https://civitai.red/models/2760803/dasiwa-krea2-or-turbo-or-raw?modelVersionId=3151280>

    If you haven't, download the int8 version and put it in `ComfyUI/models/checkpoints/`.

3. **Text encoder** — <https://civitai.red/models/2731465/qwen3-vl-4b-abliterated-comfyui-krea-2-text-encoder-bf16-fp8?modelVersionId=3070870>

    If you haven't, download the fp8 version and put it in `ComfyUI/models/text_encoders/`.

4. **VAE** — <https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/blob/main/split_files/vae/qwen_image_vae.safetensors>

    If you haven't, download the full weights and put it in `ComfyUI/models/vae/`.

Restart ComfyUI, or refresh the UI.

## Try it in ComfyUI

1. Download [KreaEdit_Default.png](../assets/KreaEdit_Default.png) and drag it into
   ComfyUI. The embedded workflow loads automatically.
2. If drag and drop doesn't work, go to ComfyUI -> File -> Open, then select the
   image.
3. The **Load Image** node is empty. Upload any picture into it — a face works
   best for seeing what the LoRA does.
4. The prompt is an instruction, not a description. Write what should change, like
   `she is now wearing a red coat`, and leave the rest unsaid.
5. Click Run, wait, and an image shows up.
6. Export/Save the output as a PNG. That file carries the whole workflow, which
   you import into Orb next.

You can also import the default PNG above straight into Orb without touching ComfyUI.

## Import it into Orb

Follow [Import a ComfyUI workflow](image-generation.md#import-a-comfyui-workflow),
with two things to watch:

- At the slot check, the positive prompt is the **Krea2EditGroundedEncode** node
  that has text in it, and the negative one is the empty one. Orb usually picks
  right — glance at the node numbers anyway.
- Leave **Width** and **Height** on **None**. This workflow takes its size from
  the reference image, so the output follows the shape of the picture you send,
  not the style's resolution. (`grounding_px` is not a resolution and Orb never
  offers it.)

Then, on the style using this workflow, set **Reference image** to character
reference. Left **Off**, each **Load Image** keeps the filename the workflow shipped
with, which doesn't exist on your server, and the render fails in this workflow. But
cloud API is more flexible and can handle this case.

In a group chat on ComfyUI, that's the member who wrote the reply you're visualizing,
and it's the one likeness a render sends: a workflow's image inputs are structural, so
Orb feeds them all the same picture rather than guessing which one wants a face. On a
cloud provider the reference field is one array, so Orb fills it with one image per
character in the scene instead. Everyone with no picture is described in words.

## Next step

[Reference images](image-generation.md#reference-images) covers the sources you
can pick, how the character reference image works, how a group chat describes the
rest of the cast, and what happens on reroll.
