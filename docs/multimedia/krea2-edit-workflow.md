# Krea 2 Edit Workflow

This guide adds an optional ComfyUI edit workflow that can preserve a character's
likeness. It builds on the Krea 2 setup and is only one way to use a reference
image. For how reference images behave everywhere else, see
[Reference images](image-generation.md#reference-images).

## Before you start

- Complete [ComfyUI Setup](comfyui-setup.md).
- Use the Krea 2 setup from that guide.
- Plan for at least 24 GB of VRAM.

## Install the custom node

The workflow uses a node that ComfyUI does not include:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/lbouaraba/comfyui-krea2edit
```

The workflow needs node pack v2.0.0 or newer. If you cloned it before, run
`git pull` in `ComfyUI/custom_nodes/comfyui-krea2edit` instead.

Restart ComfyUI after the clone. The node pack does not need other Python
packages. The ComfyUI console prints `[krea2edit] nodes v2.0.0 loaded` when it
is ready. If Orb reports `Krea2EditGroundedEncode` as missing, check the folder
and restart ComfyUI again.

## Download the models

Use the files from the Krea 2 section of [ComfyUI Setup](comfyui-setup.md) when
you already have them. The edit workflow adds this LoRA:

1. Open [Krea 2 Identity Edit](https://civitai.red/models/2761113/krea-2-identity-edit)
   and select version **v2.0**.
2. Download `krea2_identity_edit_v2.safetensors`. The same version also has
   `krea2_general_edit_v2.safetensors`, which this workflow does not use.
3. Put the file in `ComfyUI/models/loras/`.

Restart or refresh ComfyUI after installing the files.

## Test the workflow

1. Open [KreaEditV2_Identity.png](../assets/KreaEditV2_Identity.png) in ComfyUI.
2. In **Load Checkpoint**, select your Krea 2 checkpoint.
3. In **Load Image**, upload your source image. It replaces the example image,
   which is not on your server.
4. In **Krea2 Edit Grounded Encode (Positive)**, describe the change, such as
   `She is now sitting at a cafe table, holding a cup of coffee, full body.`
5. Select **Run** and check the result.

You can also import the included PNG directly into Orb.

## Import into Orb

Follow [Import a ComfyUI workflow](image-generation.md#import-a-comfyui-workflow).
Orb does not pick the right prompt slots for this workflow on its own. Set them
during import:

- **Positive prompt**: **Krea2 Edit Grounded Encode (Positive) — prompt**
- **Negative prompt**: **Krea2 Edit Grounded Encode (Negative) — prompt**
- **Model**: **CheckpointLoaderSimple — ckpt_name**
- **Width** and **Height**: map them to **EmptySD3LatentImage** so the style's
  **Resolution** sets the output size. Keep it at 2 MP or less. Left as **None**,
  every image is 512×768.

After assigning the workflow to a style, set that style's **Reference image** to
**Character references**. If it stays **Off**, the workflow keeps the example
filename from the exported PNG, which is not present on your server.

Continue with [Image Generation](image-generation.md).
