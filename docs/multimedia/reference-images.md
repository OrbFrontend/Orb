# Reference Image Setup

This guide adds a ComfyUI edit workflow that can preserve a character's likeness.
For general reference-image behavior, see
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

Restart ComfyUI after the clone. The node pack does not need other Python
packages. If Orb reports `Krea2EditGroundedEncode` as missing, check the folder
and restart ComfyUI again.

## Download the models

Use the files from the Krea 2 section of [ComfyUI Setup](comfyui-setup.md) when
you already have them. The edit workflow adds this LoRA:

1. Download [Krea 2 Identity Edit](https://civitai.red/models/2761113/krea-2-identity-edit).
2. Put `krea2_identity_edit_v1_2.safetensors` in `ComfyUI/models/loras/`.
3. Use the largest available version.

Restart or refresh ComfyUI after installing the files.

## Test the workflow

1. Open [KreaEdit_Default.png](../assets/KreaEdit_Default.png) in ComfyUI.
2. Upload a source image in the empty **Load Image** node.
3. Write an instruction describing the change, such as `she is now wearing a red coat`.
4. Select **Run** and check the result.
5. Save the output as a PNG. The PNG contains the workflow metadata.

You can also import the included PNG directly into Orb.

## Import into Orb

Follow [Import a ComfyUI workflow](image-generation.md#import-a-comfyui-workflow).
During import:

- Use the **Krea2EditGroundedEncode** node containing the positive prompt.
- Use the empty Krea2EditGroundedEncode node for the negative prompt.
- Leave **Width** and **Height** as **None**. This workflow takes its output size
  from the reference image.

After assigning the workflow to a style, set that style's **Reference image** to
**Character references**. If it stays **Off**, the workflow keeps the example
filename from the exported PNG, which is not present on your server.

Continue with [Image Generation](image-generation.md).
