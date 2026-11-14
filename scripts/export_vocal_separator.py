#!/usr/bin/env python3
"""Export torchaudio's Hybrid Demucs (HDEMUCS_HIGH_MUSDB_PLUS) vocal stem as an ONNX model and verify it.

ONNX has no inverse STFT, so the graph is the network between the spectral steps: it takes one 10 s stereo
segment and its normalized STFT (real and imaginary parts as channels) and returns the vocal stem's time-branch
waveform and spectrogram. ``backend/inference/local_models/vocal_separator`` does the STFT, inverse STFT and the
overlap-add around it.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

DEFAULT_LOCAL_NAME = "hdemucs-high-vocals.onnx"
SEGMENT = 441000  # 10 s at 44.1 kHz
HOP = 1024
NFFT = 4096


def _default_out() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "backend", "data", "models", DEFAULT_LOCAL_NAME)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=_default_out(), help=f"output path (default: data/models/{DEFAULT_LOCAL_NAME})")
    args = parser.parse_args()

    import torch
    from torchaudio.models import _hdemucs
    from torchaudio.pipelines import HDEMUCS_HIGH_MUSDB_PLUS

    def local_state(self, x: torch.Tensor) -> torch.Tensor:
        """_LocalState.forward with the self-reference mask as ``delta == 0``: onnxruntime has no bool EyeLike kernel."""
        B, C, T = x.shape
        indexes = torch.arange(T, device=x.device, dtype=x.dtype)
        delta = indexes[:, None] - indexes[None, :]
        queries = self.query(x).view(B, self.heads, -1, T)
        keys = self.key(x).view(B, self.heads, -1, T)
        dots = torch.einsum("bhct,bhcs->bhts", keys, queries) / math.sqrt(keys.shape[2])
        if self.ndecay:
            decays = torch.arange(1, self.ndecay + 1, device=x.device, dtype=x.dtype)
            decay_q = torch.sigmoid(self.query_decay(x).view(B, self.heads, -1, T)) / 2
            decay_kernel = -decays.view(-1, 1, 1) * delta.abs() / math.sqrt(self.ndecay)
            dots = dots + torch.einsum("fts,bhfs->bhts", decay_kernel, decay_q)
        dots = dots.masked_fill(delta == 0, -100)
        weights = torch.softmax(dots, dim=2)
        content = self.content(x).view(B, self.heads, -1, T)
        result = torch.einsum("bhts,bhct->bhcs", weights, content).reshape(B, -1, T)
        return x + self.proj(result)

    model = HDEMUCS_HIGH_MUSDB_PLUS.get_model().eval()
    vocals = list(HDEMUCS_HIGH_MUSDB_PLUS._sources).index("vocals") if hasattr(HDEMUCS_HIGH_MUSDB_PLUS, "_sources") else 3

    class Core(torch.nn.Module):
        """HDemucs.forward between ``_magnitude`` and ``_mask``, for the vocal source only."""

        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, wave: torch.Tensor, mag: torch.Tensor):
            m = self.m
            x = mag
            B, C, Fq, T = x.shape
            mean = x.mean(dim=(1, 2, 3), keepdim=True)
            std = x.std(dim=(1, 2, 3), keepdim=True)
            x = (x - mean) / (1e-5 + std)
            xt = wave
            meant = xt.mean(dim=(1, 2), keepdim=True)
            stdt = xt.std(dim=(1, 2), keepdim=True)
            xt = (xt - meant) / (1e-5 + stdt)
            saved, saved_t, lengths, lengths_t = [], [], [], []
            for idx, encode in enumerate(m.freq_encoder):
                lengths.append(x.shape[-1])
                inject = None
                if idx < len(m.time_encoder):
                    lengths_t.append(xt.shape[-1])
                    tenc = m.time_encoder[idx]
                    xt = tenc(xt)
                    if not tenc.empty:
                        saved_t.append(xt)
                    else:
                        inject = xt
                x = encode(x, inject)
                if idx == 0 and m.freq_emb is not None:
                    frs = torch.arange(x.shape[-2], device=x.device)
                    emb = m.freq_emb(frs).t()[None, :, :, None].expand_as(x)
                    x = x + m.freq_emb_scale * emb
                saved.append(x)
            x = torch.zeros_like(x)
            xt = torch.zeros_like(x)
            for idx, decode in enumerate(m.freq_decoder):
                skip = saved.pop(-1)
                x, pre = decode(x, skip, lengths.pop(-1))
                offset = m.depth - len(m.time_decoder)
                if idx >= offset:
                    tdec = m.time_decoder[idx - offset]
                    length_t = lengths_t.pop(-1)
                    if tdec.empty:
                        pre = pre[:, :, 0]
                        xt, _ = tdec(pre, None, length_t)
                    else:
                        skip = saved_t.pop(-1)
                        xt, _ = tdec(xt, skip, length_t)
            S = len(m.sources)
            x = x.view(B, S, -1, Fq, T) * std[:, None] + mean[:, None]
            xt = xt.view(B, S, -1, wave.shape[-1]) * stdt[:, None] + meant[:, None]
            return xt[:, vocals], x[:, vocals]

    core = Core(model).eval()
    wave = torch.randn(1, 2, SEGMENT) * 0.1
    z = model._spec(wave)
    mag = model._magnitude(z)
    with torch.no_grad():
        full = model(wave)[0, vocals]
        _hdemucs._LocalState.forward = local_state
        t_ref, f_ref = core(wave, mag)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    torch.onnx.export(
        core, (wave, mag), args.out, input_names=["wave", "spec"], output_names=["time", "freq"], opset_version=17, dynamo=False
    )
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.0f} MB); spec {tuple(mag.shape)} freq {tuple(f_ref.shape)}")

    import numpy as np
    import onnxruntime as ort

    session = ort.InferenceSession(args.out, providers=["CPUExecutionProvider"])
    t_onnx, f_onnx = session.run(None, {"wave": wave.numpy(), "spec": mag.numpy()})
    err = max(float(np.abs(t_onnx - t_ref.numpy()).max()), float(np.abs(f_onnx - f_ref.numpy()).max()))
    print(f"onnx vs torch max abs error {err:.2e}")
    zout = model._mask(torch.from_numpy(f_onnx)[:, None])[:, 0]
    rebuilt = torch.from_numpy(t_onnx) + model._ispec(zout, SEGMENT)
    rel = float((rebuilt[0] - full).norm() / full.norm())
    print(f"rebuilt vocals vs torch HDemucs relative error {rel:.2e}")
    return 0 if err < 1e-3 and rel < 1e-3 and math.isfinite(rel) else 1


if __name__ == "__main__":
    sys.exit(main())
