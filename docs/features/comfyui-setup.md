# ComfyUI Setup

Orb renders images with a [ComfyUI](https://www.comfy.org/) server. Orb does not
install ComfyUI or image models — you run ComfyUI yourself and point Orb at it.

This page is a quick, out-of-the-box setup that gets you to a running server Orb
can reach. For hardware requirements, GPU drivers, and advanced configuration,
see the [official ComfyUI documentation](https://docs.comfy.org/).

Once ComfyUI is running, continue to [Image Generation](image-generation.md) to
connect it to Orb.

## Before you start

- A GPU is strongly recommended. Check the
  [system requirements](https://docs.comfy.org/installation/system_requirements).
- Enough disk space for a checkpoint (typically 2–7 GB each).

## Install and launch ComfyUI

=== "Windows"

    **Easiest — ComfyUI Desktop**

    1. Download the installer from [comfy.org/download](https://www.comfy.org/download).
    2. Run the installer and launch **ComfyUI**.
    3. The server starts and the UI opens at `http://127.0.0.1:8188`.

    **Portable build (advanced)**

    1. Download the portable `.7z` from the
       [ComfyUI releases](https://github.com/comfyanonymous/ComfyUI/releases).
    2. Extract it, then run `run_nvidia_gpu.bat` (or `run_cpu.bat`).

=== "macOS"

    **ComfyUI Desktop**

    1. Download the macOS build from [comfy.org/download](https://www.comfy.org/download).
    2. Open the `.dmg` and drag **ComfyUI** to Applications.
    3. Launch it. The server starts at `http://127.0.0.1:8188`.

    Apple Silicon (M-series) is supported via MPS. First launch may be slow while
    dependencies initialize.

=== "Linux"

    **comfy-cli (recommended)**

    ```bash
    pip install comfy-cli
    comfy install
    comfy launch
    ```

    **Manual install**

    ```bash
    git clone https://github.com/comfyanonymous/ComfyUI.git
    cd ComfyUI
    pip install -r requirements.txt
    python main.py
    ```

    The server starts at `http://127.0.0.1:8188`.

## Add a checkpoint

ComfyUI needs at least one checkpoint (image model) to render anything.

1. Download a checkpoint (for example, an SDXL model) as a `.safetensors` file.
2. Place it in `ComfyUI/models/checkpoints/`.
3. Restart ComfyUI, or refresh the UI, so the model appears.

!!! tip
    Start with one small, well-known checkpoint to confirm the pipeline works
    before adding more.

## Make ComfyUI reachable from Orb

Orb talks to ComfyUI from your browser, so the server must accept requests from
Orb's origin.

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

## Next steps

Your ComfyUI server is ready. Head to
[Image Generation](image-generation.md#connect-orb-to-comfyui) to enter the URL,
pick a checkpoint per style, and test the connection.
