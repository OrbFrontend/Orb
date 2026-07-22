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
    3. The server starts and the UI opens at `http://127.0.0.1:8188`.
       ComfyUI-Manager is included and enabled by default.

    **Portable build (advanced)**

    1. Download the portable `.7z` from the
       [ComfyUI releases](https://github.com/comfyanonymous/ComfyUI/releases).
    2. Extract it, then launch with the Manager enabled:

        ```bat
        run_nvidia_gpu.bat --manager
        ```

        (Use `run_cpu.bat --manager` if you don't have an NVIDIA GPU.)

=== "macOS"

    **ComfyUI Desktop**

    1. Download the macOS build from [comfy.org/download](https://www.comfy.org/download).
    2. Open the `.dmg` and drag **ComfyUI** to Applications.
    3. Launch it. The server starts at `http://127.0.0.1:8188`.
       ComfyUI-Manager is included and enabled by default.

    Apple Silicon (M-series) is supported via MPS. First launch may be slow while
    dependencies initialize.

=== "Linux"

    **comfy-cli (recommended)**

    ```bash
    pip install comfy-cli
    comfy install
    comfy launch -- --manager
    ```

    **Manual install**

    ```bash
    git clone https://github.com/comfyanonymous/ComfyUI.git
    cd ComfyUI
    pip install -r requirements.txt
    python main.py --manager
    ```

    The server starts at `http://127.0.0.1:8188`.

Make sure ComfyUI starts up without any problems.

## Download checkpoints

ComfyUI needs at least one checkpoint (image model) to render anything.

1. Go to https://civitai.com/models/2458426/anima?modelVersionId=2945208
2. Download the Anima checkpoint (anima-base-v1.0.safetensors) as a `.safetensors` file and place it in `ComfyUI/models/checkpoints/`.
3. Download the text encoder (qwen_3_06b_base.safetensors) and put it in `ComfyUI/models/text_encoders/`.
4. Download the VAE (qwen_image_vae.safetensors) and put it in `ComfyUI/models/vae/`.
5. Restart ComfyUI, or refresh the UI.

Do the same for the realistic model: https://civitai.red/models/153568/real-dream?modelVersionId=3098044

Simply download and put real-dream-v2-anima-bf16.safetensors in `ComfyUI/models/checkpoints/`.

## Create your first ComfyUI gen

1. Download the [Anima_Default.png](../assets/Anima_Default.png) and drag it into ComfyUI. The embedded workflow loads automatically.
1b. If can't drag and drop, go to ComfyUI -> File -> Open... And select the image.
2. Click Run and wait, your GPU will work, then an image will show up.
3. Export/Save the output image as a PNG file. This file contains the whole workflow config which we'll import into Orb later.

Or you can also just import the above default PNG workflows straight into Orb, no need to even touch ComfyUI.

For the realistic model, do [RealDream_Default.png](../assets/RealDream_Default.png)

### A great anime model in case you find base Anima lacking:

https://civitai.com/models/934764/miaomiao-harem?modelVersionId=3125933

Workflow: [MiaoMiaoHarem_Default.png](../assets/MiaoMiaoHarem_Default.png)

Download https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2.safetensors and put it in `ComfyUI/models/upscale_models/`

## Make ComfyUI reachable from Orb

Orb talks to ComfyUI from your browser, so the server must accept requests from Orb's origin.

- **Same machine, default port:** the URL is `http://127.0.0.1:8188`. Nothing
  extra needed.
- **Different machine, or Orb served over HTTPS:** launch ComfyUI so it listens
  on the network and allows cross-origin requests:

    ```bash
    python main.py --listen 0.0.0.0 --enable-cors-header --manager
    ```

    Then use `http://<server-ip>:8188` as the URL in Orb.

!!! warning
    `--listen 0.0.0.0` exposes ComfyUI on your network. Only do this on a trusted
    network, and consider a Bearer token / reverse proxy if it's reachable more
    broadly. Orb supports an API key for Bearer-token servers.

## Next step

Your ComfyUI server is ready. Head to
[Image Generation](image-generation.md#connect-orb-to-comfyui) to enter the URL,
pick a checkpoint per style, and test the connection.
