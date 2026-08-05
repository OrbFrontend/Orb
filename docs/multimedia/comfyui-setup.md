# ComfyUI Setup

Orb renders images with a [ComfyUI](https://www.comfy.org/) server. Orb does not
install ComfyUI or image models — you run ComfyUI yourself and point Orb at it.

This page is a quick, out-of-the-box setup that gets you to a running ComfyUI server Orb
can reach. For hardware requirements, GPU drivers, and advanced configuration,
see the [official ComfyUI documentation](https://docs.comfy.org/).

## Before you start

- A GPU is strongly recommended. Check the
  [system requirements](https://docs.comfy.org/installation/system_requirements).
- Enough disk space for a checkpoint (typically 2–7 GB each).

## Install and launch ComfyUI

It's recommended to run with ComfyUI Manager. We'll install custom nodes later more easily with it.

=== "Windows"

    **Easiest — ComfyUI Desktop**

    1. Download the installer from [comfy.org/download](https://www.comfy.org/download).
    2. Run the installer and launch **ComfyUI**.
       ComfyUI-Manager is included and enabled by default.
    3. Desktop listens on port `8000`, not `8188`. Open
       **Settings → Server Config → Port**, set it to `8188`, and restart ComfyUI.
       The server is then at `http://127.0.0.1:8188`.

    **Portable build (advanced)**

    1. Download the portable `.7z` from the
       [ComfyUI releases](https://github.com/comfyanonymous/ComfyUI/releases).
    2. Extract it. The `.bat` launchers ignore any arguments you append, so to run
       with the Manager enabled, open a terminal in the extracted folder and run:

        ```bat
        .\python_embeded\python.exe -m pip install -r ComfyUI\manager_requirements.txt
        .\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --enable-manager
        ```

        (Add `--cpu` to the second command if you don't have an NVIDIA GPU.)
        If you don't want the Manager, `run_nvidia_gpu.bat` / `run_cpu.bat` work as-is.

=== "macOS"

    **ComfyUI Desktop**

    1. Download the macOS build from [comfy.org/download](https://www.comfy.org/download).
    2. Open the `.dmg` and drag **ComfyUI** to Applications.
    3. Launch it. ComfyUI-Manager is included and enabled by default.
    4. Desktop listens on port `8000`, not `8188`. Open
       **Settings → Server Config → Port**, set it to `8188`, and restart ComfyUI.
       The server is then at `http://127.0.0.1:8188`.

    Requires Apple Silicon (M1 or later) and macOS 13 Ventura or newer — Intel Macs
    are not supported. First launch may be slow while dependencies initialize.

=== "Linux"

    **comfy-cli (recommended)**

    ```bash
    pip install comfy-cli
    comfy install
    comfy launch
    ```

    `comfy install` includes ComfyUI-Manager, and `comfy launch` enables it for you.

    **Manual install**

    ```bash
    git clone https://github.com/comfyanonymous/ComfyUI.git
    cd ComfyUI
    pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130
    pip install -r requirements.txt
    pip install -r manager_requirements.txt
    python main.py --enable-manager
    ```

    (The torch line is the NVIDIA/CUDA build; see the
    [ComfyUI README](https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux)
    for AMD, Intel, or CPU-only.)

    The server starts at `http://127.0.0.1:8188`.

Make sure ComfyUI starts up without any problems.

## Download checkpoints

ComfyUI needs at least one checkpoint (image model) to render anything.

1. Go to <https://civitai.com/models/2458426/anima?modelVersionId=2945208>
2. Download the Anima checkpoint (anima-base-v1.0.safetensors) as a `.safetensors` file and place it in `ComfyUI/models/checkpoints/`.
3. Download the text encoder (qwen_3_06b_base.safetensors) and put it in `ComfyUI/models/text_encoders/`.
4. Download the VAE (qwen_image_vae.safetensors) and put it in `ComfyUI/models/vae/`.
5. Restart ComfyUI, or refresh the UI.

Do the same for the realistic model: <https://civitai.red/models/153568/real-dream?modelVersionId=3098044>

Simply download and put real-dream-v2-anima-bf16.safetensors in `ComfyUI/models/checkpoints/`.

## Create your first ComfyUI gen

1. Download the [Anima_Default.png](../assets/Anima_Default.png) and drag it into ComfyUI. The embedded workflow loads automatically.
2. If drag and drop doesn't work, go to ComfyUI -> File -> Open, then select the image.
3. Click Run button (top right corner) and wait, your GPU will work, then an image will show up.
4. Export/Save the output image as a PNG file. This file contains the whole workflow config which we'll import into Orb later.

Or you can also just import the above default PNG workflows straight into Orb, no need to even touch ComfyUI.

For the realistic model, do [RealDream_Default.png](../assets/RealDream_Default.png)

### A great anime-only model in case you find base Anima lacking:

<https://civitai.com/models/934764/miaomiao-harem?modelVersionId=3125933>

Workflow: [MiaoMiaoHarem_Default.png](../assets/MiaoMiaoHarem_Default.png)

Download <https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2.safetensors> and put it in `ComfyUI/models/upscale_models/`

### If you're GPU-rich (24GB+ VRAM), try Krea 2:

<https://civitai.red/models/2760803/dasiwa-krea2-or-turbo-or-raw?modelVersionId=3151280>
 (Download the int8-convrot version and put it in `ComfyUI/models/checkpoints/`)

Workflow: [Krea2_Default.png](../assets/Krea2_Default.png)

<https://civitai.red/models/2731465/qwen3-vl-4b-abliterated-comfyui-krea-2-text-encoder-bf16-fp8?modelVersionId=3070870>
 (Download the fp8 version and put it in `ComfyUI/models/text_encoders/`)

<https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/blob/main/split_files/vae/qwen_image_vae.safetensors>
 (Download the full weights and put it in `ComfyUI/models/vae/`)

There are other ways to run Krea 2 with lower VRAM but won't go into that here.

To edit an existing picture instead of drawing a new one — keeping a character's
face across images — see [Reference Image Setup](reference-images.md).

## Make ComfyUI reachable from Orb

Orb talks to ComfyUI from your browser, so the server must accept requests from Orb's origin.

- **Same machine, default port:** the URL is `http://127.0.0.1:8188`. Nothing
  extra needed.
- **Different machine, or Orb served over HTTPS:** launch ComfyUI so it listens
  on the network and allows cross-origin requests:

    ```bash
    python main.py --listen 0.0.0.0 --enable-cors-header --enable-manager
    ```

    Then use `http://<server-ip>:8188` as the URL in Orb.

!!! warning
    `--listen 0.0.0.0` exposes ComfyUI on your network. Only do this on a trusted
    network, and consider a Bearer token / reverse proxy if it's reachable more
    broadly. Orb supports an API key for Bearer-token servers.

## Next step

Your ComfyUI server is ready. Head to
[Image Generation](image-generation.md#external-comfyui) to enter the URL,
pick a checkpoint per style, and test the connection.
