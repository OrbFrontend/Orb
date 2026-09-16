# Built-in Spark-TTS voice cloner

This implementation is complete. This note records the small set of design
constraints that are not obvious from the code; user-facing setup lives in
[the TTS guide](../multimedia/tts.md).

## Scope

An enrolled voice is 32 BiCodec global-token indices stored in the character's
TTS profile. The uploaded audio is decoded only for enrollment and is never
stored. Existing sidecar profiles are migrated from `spark` to `spark_remote`;
new `spark` profiles use the built-in cloner.

The implementation deliberately transfers timbre only. The upstream
transcript-conditioned path would also copy the reference pacing, but requires
wav2vec2 and the BiCodec encoder (about 1.4 GB of additional weights). It is
not part of Orb's built-in feature.

## Runtime contract

- The speaker encoder consumes a 16 kHz mel spectrogram and produces the 32
  global-token indices. It is a small ONNX graph.
- The decoder consumes generated semantic-token indices plus those global
  tokens and produces 16 kHz PCM. It is also ONNX and CPU-only.
- A llama-server child runs the 0.5B GGUF to generate semantic tokens. It is
  the only part that uses the GPU switch and is released after inactivity.
- Codec and model artifacts download independently. Enrollment needs the codec
  only, so the UI installs it before the llama runtime and voice model.

The shared llama-server runtime is downloaded from the generic Local ML runtime
route. It is not owned by the Prose Rewriter even though that feature also uses
it.

## Token contract

All BiCodec tokens are GGUF control tokens. llama-server therefore receives an
integer prompt and returns generated token IDs; completion text must not be
parsed. The contiguous ranges are pinned by `test_spark_tokens.py`:

| Family | Index range | Token-ID range |
|---|---:|---:|
| global | 0–4095 | 151665–155760 |
| semantic | 0–8191 | 155761–163952 |

User text is tokenized with special-token parsing disabled. Generated IDs outside
the semantic range are ignored before the decoder runs.

`UBATCH_SIZE = 8` is a correctness limit for the pinned llama.cpp Vulkan build:
larger prompt batches can corrupt this model's prompt. Do not increase it without
an end-to-end audio regression check.

## Audio contract

The mel parameters and volume normalization match Spark-TTS. Short references
are tiled to six seconds; longer ones are kept, up to two minutes, because more
clean speech gives a more stable speaker representation. WAV, FLAC, and OGG are
handled by the optional audio dependency; other formats use `ffmpeg` when it is
available.

`scripts/export_spark_speaker_encoder.py` regenerates the speaker-encoder ONNX
artifact. Any replacement must preserve the 32-token output contract and update
the catalog checksum and enrollment golden test.
