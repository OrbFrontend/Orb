# ComfyUI Setup

Orb uses a ComfyUI server to generate images. You install and run ComfyUI and its
models separately from Orb.

For hardware and driver requirements, see the
[official ComfyUI documentation](https://docs.comfy.org/installation/system_requirements).

## Before you start

- A GPU is strongly recommended.
- Reserve several gigabytes for each checkpoint.
- Install [ComfyUI Manager](https://docs.comfy.org/installation/install_comfyui#comfyui-manager)
  if possible. It makes custom-node installation easier.

## Install and start ComfyUI

=== "Windows"

    **ComfyUI Desktop**

    1. Download it from [comfy.org/download](https://www.comfy.org/download).
    2. Install and start ComfyUI.
    3. Desktop uses port `8000` by default. In **Settings → Server Config → Port**,
       change it to `8188` and restart ComfyUI.

    The portable build is for advanced users. Download it from the
    [ComfyUI releases](https://github.com/comfyanonymous/ComfyUI/releases), then
    follow ComfyUI's instructions for enabling Manager. Use `--cpu` when you do
    not have an NVIDIA GPU.

=== "macOS"

    1. Download the macOS build from [comfy.org/download](https://www.comfy.org/download).
    2. Open the DMG and move ComfyUI to Applications.
    3. Start it and change the port from `8000` to `8188` in **Settings → Server
       Config → Port**.

    The desktop build requires Apple Silicon and macOS 13 Ventura or newer.

=== "Linux"

    **comfy-cli**

    ```bash
    pip install comfy-cli
    comfy install
    comfy launch
    ```

    **Manual install**

    ```bash
    git clone https://github.com/comfyanonymous/ComfyUI.git
    cd ComfyUI
    pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130
    pip install -r requirements.txt
    pip install -r manager_requirements.txt
    python main.py --enable-manager
    ```

    The manual commands use the NVIDIA/CUDA build. See the
    [ComfyUI README](https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux)
    for AMD, Intel, or CPU-only installs. The default server URL is
    `http://127.0.0.1:8188`.

## Install a model

ComfyUI needs a checkpoint, text encoder, and VAE. The following files match the
included starter workflows:

1. Download [Anima](https://civitai.com/models/2458426/anima?modelVersionId=2945208)
   and put `anima-base-v1.0.safetensors` in `ComfyUI/models/checkpoints/`.
2. Put `qwen_3_06b_base.safetensors` in `ComfyUI/models/text_encoders/`.
3. Put `qwen_image_vae.safetensors` in `ComfyUI/models/vae/`.
4. For a realistic style, download
   [Real Dream](https://civitai.red/models/153568/real-dream?modelVersionId=3098044)
   and put `real-dream-v2-anima-bf16.safetensors` in `models/checkpoints/`.
5. Restart or refresh ComfyUI.

## Test a starter workflow

1. Open [Anima_Default.png](../assets/Anima_Default.png) in ComfyUI by dragging it
   into the window or selecting **File → Open**.
2. Select **Run** and wait for the image.
3. Save or export the result as a PNG. The PNG includes the workflow metadata.

You can import the included PNG directly into Orb without running it first. For
the realistic model, use [RealDream_Default.png](../assets/RealDream_Default.png).

### Optional models

- [MiaoMiao Harem](https://civitai.com/models/934764/miaomiao-harem?modelVersionId=3125933)
  is an anime-focused option. Its workflow is
  [MiaoMiaoHarem_Default.png](../assets/MiaoMiaoHarem_Default.png). It also needs
  [UltraSharpV2](https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2.safetensors)
  in `models/upscale_models/`.
- [Krea 2](https://civitai.red/models/2760803/dasiwa-krea2-or-turbo-or-raw?modelVersionId=3151280)
  needs at least 24 GB of VRAM for the setup described here. Use
  [Krea2_Default.png](../assets/Krea2_Default.png), the int8 checkpoint, the fp8
  [text encoder](https://civitai.red/models/2731465/qwen3-vl-4b-abliterated-comfyui-krea-2-text-encoder-bf16-fp8?modelVersionId=3070870),
  and the [Qwen VAE](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/blob/main/split_files/vae/qwen_image_vae.safetensors).

For image editing with a reference image, see the optional
[Krea 2 Edit Workflow](krea2-edit-workflow.md).

## Connect Orb to ComfyUI

For Orb and ComfyUI on the same computer, use `http://127.0.0.1:8188` in Orb.

For another computer, or when Orb uses HTTPS, start ComfyUI with network access
and CORS enabled:

```bash
python main.py --listen 0.0.0.0 --enable-cors-header --enable-manager
```

Then use `http://<server-ip>:8188` in Orb. Add a Bearer-token API key if the
server requires one.

!!! warning
    `--listen 0.0.0.0` exposes ComfyUI to your network. Use it only on a trusted
    network, or protect the server with a token or reverse proxy.

Continue with [Image Generation](image-generation.md#connect-an-image-backend).
