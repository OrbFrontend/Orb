#!/usr/bin/env python3
"""Export and verify Spark-TTS's speaker encoder as an ONNX model."""

from __future__ import annotations

import argparse
import os
import sys
import time

DEFAULT_LOCAL_NAME = "spark-speaker-encoder.onnx"
REF_SECONDS = 6
HOP = 320
SAMPLE_RATE = 16000


def _default_out() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "backend", "data", "models", DEFAULT_LOCAL_NAME)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkpoint", required=True, help="Spark-TTS-0.5B directory (containing BiCodec/)")
    parser.add_argument("--spark-tts", required=True, help="upstream Spark-TTS repo, for `sparktts.*`")
    parser.add_argument("--out", default=_default_out(), help=f"output path (default: data/models/{DEFAULT_LOCAL_NAME})")
    parser.add_argument("--reference", default="", help="a 16 kHz wav to verify against; default: the repo's example clip")
    args = parser.parse_args()

    sys.path.insert(0, args.spark_tts)
    import numpy as np
    import torch
    from sparktts.models.bicodec import BiCodec
    from sparktts.utils.audio import load_audio

    codec = BiCodec.load_from_checkpoint(os.path.join(args.checkpoint, "BiCodec"))
    codec.eval()

    class Enroller(torch.nn.Module):
        """Map mel input ``(B, n_mels, T)`` to 32 speaker ids."""

        def __init__(self, speaker_encoder: torch.nn.Module) -> None:
            super().__init__()
            self.se = speaker_encoder

        def forward(self, mel: torch.Tensor) -> torch.Tensor:
            return self.se.tokenize(mel.transpose(1, 2))

    enroller = Enroller(codec.speaker_encoder).eval()

    reference = args.reference or os.path.join(args.spark_tts, "example", "prompt_audio.wav")
    wav = load_audio(reference, sampling_rate=SAMPLE_RATE, volume_normalize=True)
    ref_len = SAMPLE_RATE * REF_SECONDS // HOP * HOP
    if ref_len > len(wav):
        wav = np.tile(wav, ref_len // len(wav) + 1)
    with torch.no_grad():
        mel = codec.mel_transformer(torch.from_numpy(wav[:ref_len]).unsqueeze(0).float()).squeeze(1)
        want = enroller(mel).flatten().tolist()
    print(f"reference {os.path.basename(reference)}: mel {tuple(mel.shape)} -> {want[:8]} …")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    started = time.perf_counter()
    torch.onnx.export(
        enroller,
        (mel,),
        args.out,
        input_names=["mel"],
        output_names=["speaker_tokens"],
        dynamic_axes={"mel": {0: "batch", 2: "frames"}},
        opset_version=17,
        do_constant_folding=True,
    )
    print(f"exported in {time.perf_counter() - started:.1f}s -> {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")

    import onnxruntime as ort

    session = ort.InferenceSession(args.out, providers=["CPUExecutionProvider"])
    got = session.run(["speaker_tokens"], {"mel": mel.numpy()})[0].flatten().tolist()
    if got != want:
        print(f"FAIL: onnx {got[:8]} != torch {want[:8]}", file=sys.stderr)
        return 1
    print("matches torch on the reference clip")

    failures = 0
    rng = np.random.default_rng(0)
    for label, frames in (("51 frames", 51), ("151 frames", 151), ("601 frames", 601)):
        probe = torch.from_numpy(rng.standard_normal((1, 128, frames)).astype("float32"))
        with torch.no_grad():
            expected = enroller(probe).flatten().tolist()
        actual = session.run(["speaker_tokens"], {"mel": probe.numpy()})[0].flatten().tolist()
        ok = expected == actual
        failures += not ok
        print(f"  dynamic frames {label:12} {'ok' if ok else 'MISMATCH'}")
    batch = torch.cat([mel, mel * 0.5], dim=0)
    with torch.no_grad():
        expected = enroller(batch).reshape(2, -1).tolist()
    actual = session.run(["speaker_tokens"], {"mel": batch.numpy()})[0].reshape(2, -1).tolist()
    failures += expected != actual
    print(f"  dynamic batch              {'ok' if expected == actual else 'MISMATCH'}")

    again = session.run(["speaker_tokens"], {"mel": mel.numpy()})[0].flatten().tolist()
    failures += again != got
    print(f"  deterministic across runs  {'ok' if again == got else 'MISMATCH'}")

    if failures:
        print(f"{failures} check(s) failed; NOT keeping {args.out}", file=sys.stderr)
        os.remove(args.out)
        return 1
    print("\nall checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
