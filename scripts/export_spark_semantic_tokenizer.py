#!/usr/bin/env python3
"""Export and verify Spark-TTS's semantic tokenizer as one ONNX model.

The graph maps a 16 kHz mono waveform ``(1, samples)`` to BiCodec semantic
token indices ``(1, frames)`` at 50 per second — what upstream's
``BiCodecTokenizer.tokenize`` computes for a prompt clip, in one graph:

1. wav2vec2's zero-mean / unit-variance input normalization,
2. wav2vec2-large-xlsr-53 truncated to its first 16 transformer layers, since
   upstream reads only hidden states 11, 14 and 16 (a third of the weights are
   never used),
3. the mean of those three states through BiCodec's encoder and factorized
   quantizer.

Verification compares every export against upstream's own pipeline — the
feature-extractor processor and the untruncated model's ``hidden_states`` —
not against the wrapper it was traced from, so a wrong truncation index fails
here rather than producing plausible tokens.

Weights are stored as float16 and cast back to float32 when the session loads,
so the file is half the size while the arithmetic stays float32. Semantic
tokens are an argmax over 8192 cosine similarities, which makes them sensitive
to weight precision: measured on three clips, float16 storage kept 99.8–100% of
upstream's tokens, while dynamic int8 kept 62–76% (68–72% per-channel) and is
not offered. The script reports the agreement and refuses an export below
``--min-agreement``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

DEFAULT_LOCAL_NAME = "spark-semantic-tokenizer.onnx"
SAMPLE_RATE = 16000
#: wav2vec2 hidden states upstream mixes (``audio_tokenizer.py``).
MIXED_STATES = (11, 14, 16)


def _default_out() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "backend", "data", "models", DEFAULT_LOCAL_NAME)


def _store_float16(src: str, dst: str) -> None:
    """Store large float32 initializers as float16, each behind a Cast back.

    Not a float16 conversion of the graph: every op still computes in float32,
    which is what ONNX Runtime's CPU provider has kernels for on every
    platform. ONNX Runtime folds the casts once when the session loads.
    """
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    model = onnx.load(src)
    graph = model.graph
    initializers, casts = [], []
    for init in graph.initializer:
        if init.data_type == TensorProto.FLOAT and int(np.prod(init.dims)) >= 1024:
            half = numpy_helper.from_array(numpy_helper.to_array(init).astype(np.float16), f"{init.name}__fp16")
            initializers.append(half)
            casts.append(helper.make_node("Cast", [half.name], [init.name], to=TensorProto.FLOAT, name=f"{init.name}__cast"))
        else:
            initializers.append(init)
    nodes = list(graph.node)
    del graph.initializer[:]
    graph.initializer.extend(initializers)
    del graph.node[:]
    graph.node.extend(casts + nodes)
    onnx.save(model, dst)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkpoint", required=True, help="Spark-TTS-0.5B directory (BiCodec/ and wav2vec2-large-xlsr-53/)")
    parser.add_argument("--spark-tts", required=True, help="upstream Spark-TTS repo, for `sparktts.*`")
    parser.add_argument("--out", default=_default_out(), help=f"output path (default: data/models/{DEFAULT_LOCAL_NAME})")
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="audio to verify against (repeatable); default: the repo's example clip",
    )
    parser.add_argument("--fp32", action="store_true", help="keep float32 weights (twice the size)")
    parser.add_argument(
        "--min-agreement",
        type=float,
        default=0.99,
        help="minimum fraction of tokens that must match upstream (default 0.99)",
    )
    args = parser.parse_args()

    sys.path.insert(0, args.spark_tts)
    import numpy as np
    import torch
    from sparktts.models.bicodec import BiCodec
    from sparktts.utils.audio import load_audio
    from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2Model

    codec = BiCodec.load_from_checkpoint(os.path.join(args.checkpoint, "BiCodec")).eval()
    w2v_dir = os.path.join(args.checkpoint, "wav2vec2-large-xlsr-53")
    processor = Wav2Vec2FeatureExtractor.from_pretrained(w2v_dir)
    wav2vec2 = Wav2Vec2Model.from_pretrained(w2v_dir).eval()
    if not wav2vec2.config.do_stable_layer_norm:
        # The truncation below assumes pre-norm layers whose hidden state k is
        # the raw output of layer k-1, with the final norm applied only after
        # the last layer. A post-norm checkpoint would need a different graph.
        print("FAIL: expected a stable-layer-norm wav2vec2", file=sys.stderr)
        return 1

    class SemanticTokenizer(torch.nn.Module):
        """Map ``(1, samples)`` 16 kHz audio to ``(1, frames)`` semantic ids."""

        def __init__(self) -> None:
            super().__init__()
            self.conv = wav2vec2.feature_extractor
            self.projection = wav2vec2.feature_projection
            self.pos_conv = wav2vec2.encoder.pos_conv_embed
            self.layers = torch.nn.ModuleList(wav2vec2.encoder.layers[: max(MIXED_STATES)])
            self.encoder = codec.encoder
            self.quantizer = codec.quantizer

        def forward(self, wav: torch.Tensor) -> torch.Tensor:
            mean = wav.mean(dim=-1, keepdim=True)
            var = ((wav - mean) ** 2).mean(dim=-1, keepdim=True)
            x = (wav - mean) / torch.sqrt(var + 1e-7)
            hidden, _ = self.projection(self.conv(x).transpose(1, 2))
            hidden = hidden + self.pos_conv(hidden)
            mixed = torch.zeros_like(hidden)
            for index, layer in enumerate(self.layers, start=1):
                hidden = layer(hidden)[0]
                if index in MIXED_STATES:
                    mixed = mixed + hidden
            z = self.encoder((mixed / len(MIXED_STATES)).transpose(1, 2))
            return self.quantizer.tokenize(z)

    def upstream(wav: np.ndarray) -> list[int]:
        """Upstream's tokenization, verbatim: processor, full model, hidden states."""
        values = processor(wav, sampling_rate=SAMPLE_RATE, return_tensors="pt", padding=True).input_values
        with torch.no_grad():
            out = wav2vec2(values, output_hidden_states=True)
            mix = sum(out.hidden_states[i] for i in MIXED_STATES) / len(MIXED_STATES)
            z = codec.encoder(mix.transpose(1, 2))
            return codec.quantizer.tokenize(z).flatten().tolist()

    tokenizer = SemanticTokenizer().eval()
    references = args.reference or [os.path.join(args.spark_tts, "example", "prompt_audio.wav")]
    clips = []
    for path in references:
        wav = load_audio(path, sampling_rate=SAMPLE_RATE, volume_normalize=True).astype(np.float32)
        wav = wav[: SAMPLE_RATE * 20]  # a prompt excerpt is far shorter; keep verification quick
        want = upstream(wav)
        with torch.no_grad():
            traced_from = tokenizer(torch.from_numpy(wav)[None]).flatten().tolist()
        if traced_from != want:
            print(f"FAIL: wrapper disagrees with upstream on {os.path.basename(path)}", file=sys.stderr)
            return 1
        print(f"reference {os.path.basename(path)}: {len(wav) / SAMPLE_RATE:.1f}s -> {len(want)} tokens {want[:8]} …")
        clips.append((os.path.basename(path), wav, want))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fp32_path = args.out if args.fp32 else args.out + ".fp32.tmp"
    started = time.perf_counter()
    torch.onnx.export(
        tokenizer,
        (torch.from_numpy(clips[0][1])[None],),
        fp32_path,
        input_names=["wav"],
        output_names=["semantic_tokens"],
        dynamic_axes={"wav": {1: "samples"}, "semantic_tokens": {1: "frames"}},
        opset_version=17,
        do_constant_folding=True,
    )
    print(f"exported in {time.perf_counter() - started:.1f}s ({os.path.getsize(fp32_path) / 1e6:.1f} MB)")
    if not args.fp32:
        _store_float16(fp32_path, args.out)
        os.remove(fp32_path)
        print(f"float16 storage -> {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")

    import onnxruntime as ort

    session = ort.InferenceSession(args.out, providers=["CPUExecutionProvider"])

    def run(wav: np.ndarray) -> list[int]:
        return session.run(["semantic_tokens"], {"wav": wav[None]})[0].flatten().tolist()

    failures = 0
    for name, wav, want in clips:
        got = run(wav)
        if len(got) != len(want):
            print(f"  {name}: {len(got)} tokens, upstream {len(want)}  MISMATCH")
            failures += 1
            continue
        agreement = sum(a == b for a, b in zip(got, want, strict=True)) / len(want)
        ok = agreement >= args.min_agreement
        failures += not ok
        print(f"  {name}: {agreement:.2%} of tokens match upstream  {'ok' if ok else 'BELOW THRESHOLD'}")

    wav = clips[0][1]
    for seconds in (1.0, 3.3, 9.7):
        probe = wav[: int(SAMPLE_RATE * seconds)]
        frames = len(run(probe))
        expected = len(upstream(probe))
        ok = frames == expected
        failures += not ok
        print(f"  dynamic length {seconds:>4}s       {frames} frames {'ok' if ok else f'MISMATCH (upstream {expected})'}")
    first = run(wav)
    again = run(wav)
    failures += again != first
    print(f"  deterministic across runs  {'ok' if again == first else 'MISMATCH'}")

    if failures:
        print(f"{failures} check(s) failed; NOT keeping {args.out}", file=sys.stderr)
        os.remove(args.out)
        return 1
    print("\nall checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
