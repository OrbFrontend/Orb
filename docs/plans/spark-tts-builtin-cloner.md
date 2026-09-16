# Spark-TTS as a built-in voice cloner — implementation plan

Replace the external OrbTTS sidecar (`../OrbTTS`, a FastAPI wrapper around a
PyTorch Spark-TTS checkout) with a cloner unit inside Orb, reusing the
llama-server + Vulkan runtime Orb already ships.

Workflow the user sees: **upload one audio file to a character, then that
character speaks in that voice from then on.** No sidecar, no torch, no manual
`voices.json`.

Status: **verified, no code written.** Every load-bearing assumption below has
been tested end to end on this machine — including a full synthesis through the
proposed pipeline with no torch on the path. See [Verification
results](#verification-results) for what was proved and what is still open.

## Verification results

Run 2026-09-16 against `../OrbTTS/models/Spark-TTS-0.5B`, llama.cpp build
`b10549` (the build `binary.py` pins), on macOS arm64 / Metal.

| # | Claim under test | Result |
|---|---|---|
| 1 | Speaker tokens derive from mel alone | **Confirmed** — bit-identical to the full pipeline, 259× faster |
| 2 | Decode path separable from encoder | **Confirmed** — `encoder`/`postnet`/`mel_transformer` never fire |
| 3 | GGUF preserved the audio vocabulary | **Confirmed** — 166 000 tokens, every probed ID exact |
| 4 | GGUF provenance is the official weights | **Confirmed** — sha256 identical across all four repos |
| 5 | `bicodec.onnx` matches torch | **Confirmed** — 114 dB SNR, correlation 1.00000000 |
| 6 | …across realistic lengths | **Confirmed** — 97–109 dB for 1…1500 semantic tokens |
| 7 | Speaker encoder is ONNX-exportable | **Confirmed** — exports in 2.9 s, identical output, 23.8 MB |
| 8 | llama-server can be driven by token IDs | **Confirmed** — int-array prompt + `return_tokens` both work |
| 9 | The whole pipeline produces speech | **Confirmed** — 0.55× realtime end to end |
| 10 | The clone carries the reference identity | **Confirmed** — cosine 0.916 vs 0.437 for an unrelated voice |
| 11 | Global-only loses nothing on timbre | **Confirmed** — 0.928 vs 0.936, inside seed noise |
| 12 | Global-only loses nothing on *prosody* | **Not established — still needs ears.** See Phase 0 |

### 5 — `bicodec.onnx` is numerically equivalent

`Fhrozen/Spark-TTS-0.5B-ONNX`, sha256 `a1459483cf562e9d…`, 385 417 099 bytes.
Fed the exact token pair from a real enrollment and compared against torch
`BiCodec.detokenize`:

```
max|diff|    5.901e-06      SNR          114.0 dB
mean|diff|   1.078e-07      correlation  1.00000000
```

Holding across the lengths real synthesis produces (random tokens, so these are
graph checks rather than audio checks):

| semantic tokens | audio | SNR |
|---:|---:|---:|
| 1–2 (degenerate) | 0.02–0.04 s | 108–109 dB |
| 25 (very short line) | 0.50 s | 104.2 dB |
| 100 | 2.00 s | 107.6 dB |
| 497 (the enrolled clip) | 9.94 s | 98.8 dB |
| 800 | 16.00 s | 98.0 dB |
| 1500 (at the token cap) | 30.00 s | 97.4 dB |

Its declared interface is `semantic_tokens (batch, length) int64` +
`global_tokens (batch, 1, 32) int64` → `audio float32`.

**We can ship this file rather than re-export it.** One caveat: on this machine
ONNX Runtime is *slower* than torch for decode (2.22 s vs 1.88 s, 0.84×). The
win here is deleting the torch dependency, not speed — and at 0.22× realtime
the vocoder is not the constraint either way.

### 7 — the speaker encoder exports cleanly

The one artifact that does not exist prebuilt. Wrapping
`SpeakerEncoder.tokenize` and exporting at opset 17 gives **23.8 MB** (smaller
than the 39.4 MB of raw weights — the FSQ is parameter-free and folds away), runs
in **6.8 ms**, and returns tokens identical to torch.

It generalises past the traced shape, which matters because the tracer emitted
`TracerWarning`s on the FSQ loop and on shape asserts:

| input | torch == onnx |
|---|---|
| 301 frames (the 6 s spec) | yes |
| 51 / 151 / 601 frames | yes |
| batch = 2 | yes |
| white noise, digital silence | yes |

Enrollment is also **deterministic** — the same clip yields the same 32 ints
across runs. Worth an assertion in tests.

### 8 — llama-server plumbing, and why token IDs are mandatory

Every bicodec token is **`CONTROL` type** in the GGUF (4096 global + 8192
semantic), and `llama-server`'s `--special` defaults to false. The consequence,
measured:

```
/completion {..., "return_tokens": true}
  content: ''                       <- specials hidden; upstream's regex finds nothing
  tokens : [165149, 164235, ..., 155660, 153444]   <- the real output
```

**Upstream's approach — regex `bicodec_semantic_(\d+)` out of the completion
text — returns nothing against llama-server.** Three behaviours were confirmed
on `b10549` and the design depends on all of them:

- `/tokenize` needs `"parse_special": true` to map `<|bicodec_global_0|>` to
  `[151665]`; without it the same string becomes six ordinary text tokens.
- `/completion` accepts an **int array** as `prompt`. This is the path to use:
  it skips tokenizer round-tripping entirely.
- `"return_tokens": true` returns generated token IDs, which we convert by
  arithmetic (`index = id - 155761`).

### 9–11 — end to end, and the clone actually clones

Full proposed pipeline, no torch: 32 stored ints → token-ID prompt →
llama-server → `return_tokens` → `bicodec.onnx` → WAV.

```
prompt      55 tokens for 77 chars
LLM        209 tokens in 1.41 s (147.8 tok/s, Metal), stop_type=eos
           207 semantic, 0 global emitted   <- identity came from our tokens
decode     0.85 s -> 4.14 s audio
TOTAL      2.26 s for 4.14 s speech = 0.55x realtime  (LLM 63% / vocoder 37%)
```

Identity was checked objectively rather than by ear: re-enroll the *synthesized*
audio, map tokens to the 1024-d d-vector the codec conditions on, and compare
against the reference by cosine.

| condition | mean cosine vs reference | spread (4 seeds) |
|---|---:|---|
| cloned, our pipeline | **0.9156** | 0.899 – 0.931 |
| control voice (model invents a speaker) | 0.4370 | 0.413 – 0.468 |

### 11–12 — what the expensive path actually buys

A/B on the one reference clip that ships with a transcript
(`example/prompt_audio.wav`, which is Chinese — so the English run above was
already a cross-lingual timbre transfer, and it held).

| | A: global only *(this plan)* | B: + `ref_text` *(upstream)* |
|---|---|---|
| prompt | 60 tokens | 608 tokens (**10.1× prefill**) |
| needs | mel + 24 MB | wav2vec2 + encoder, **+1.4 GB** |
| timbre (cosine) | 0.9284 | 0.9355 |
| audio for the same text | 6.6 – 6.9 s | 5.4 – 5.7 s |
| LLM time | 2.26 s | 1.98 s |

**On timbre the two are indistinguishable** — the 0.007 gap is smaller than the
seed-to-seed spread within either mode (A: 0.919–0.933, B: 0.928–0.940).

**On prosody they differ measurably.** B's output is consistently ~18% shorter
for identical text, i.e. it adopts the reference's speaking rate — an ad read,
delivered fast. That is the in-context path doing exactly what it claims. Whether
matching a reference's pace is *desirable* for roleplay dialogue is the open
question, and it is a judgement about voice, not a number. Hence Phase 0 survives.

Samples for that listen are written to the session scratchpad as
`ab_A_{42,7,99}.wav` and `ab_B_{42,7,99}.wav`, alongside
`fid2_cloned_*.wav` / `fid2_control_*.wav`.


## Why the sidecar has to go

`backend/workflows/tts/engine/spark_adapter.py` is a thin HTTP client for a
server the user must install, run, and keep running. Setting that server up
costs a 3.7 GB model download, a 664 MB virtualenv (328 MB of it torch), a
vendored upstream checkout, and hand-editing `voices/voices.json` to register a
cloned voice. The adapter's own `list_voices()` returns `[]` when the sidecar is
down, so the failure mode is a silently empty voice picker.

None of that weight is load-bearing for the feature we want.

## The finding this plan rests on

Spark-TTS cloning is not a training step. A "cloned voice" is a prompt prefix:
BiCodec encodes the reference clip into **global tokens** (speaker timbre) and
**semantic tokens** (what was said), those are stringified into
`<|bicodec_global_N|>` / `<|bicodec_semantic_N|>`, and the LLM continues the
semantic stream for the new text. The BiCodec decoder then renders audio using
the *reference's* global tokens for timbre.

Two measurements turn that into a cheap feature.

### 1. Speaker identity comes from mel alone — wav2vec2 is not involved

`BiCodec.tokenize()` looks like it needs the whole encoder stack, but the two
outputs have disjoint inputs:

```python
def tokenize(self, batch):
    feat = batch["feat"]                                    # wav2vec2 features
    mel  = self.mel_transformer(batch["ref_wav"]).squeeze(1)
    z = self.encoder(feat.transpose(1, 2))
    semantic_tokens = self.quantizer.tokenize(z)                        # needs wav2vec2
    global_tokens   = self.speaker_encoder.tokenize(mel.transpose(1, 2))  # needs ONLY mel
    return semantic_tokens, global_tokens
```

Verified by computing global tokens both ways on `example/prompt_audio.wav`:

| | full pipeline | mel only |
|---|---|---|
| global tokens | `[3363, 2367, 1591, 3625, …]` | `[3363, 2367, 1591, 3625, …]` |
| result | **bit-identical (32 int32, range `[0, 4096)`)** | |
| time | 3.597 s | **0.014 s** (259.9× faster) |
| weights touched | wav2vec2 1269 MB + encoder 122 MB + ECAPA 39 MB | **39.4 MB** (ECAPA + perceiver + FSQ) |

So enrollment needs a mel spectrogram and 39 MB of weights. **wav2vec2 (1.2 GB)
and the BiCodec encoder (122 MB) can be dropped from the product entirely** —
at the cost described under [Accepted quality
tradeoff](#accepted-quality-tradeoff).

Upstream reads a **fixed 6.00 s window** (`ref_segment_duration: 6`, 96 000
samples): it tiles a shorter clip to fill it and ignores everything past the
first 6 s of a longer one. **Orb departs from the second half** (2026-09-16):
the graph accepts any frame count, so a longer clip is fed whole, up to
`MAX_SOURCE_SECONDS`. Which 6 s you pick turns out to matter. Clones were built
from each 6 s window of one 47 s Ancestor recording (3 lines × 2 seeds each).
WavLM-SV then scored them against the real voice, where real windows score
0.97 and an unrelated voice 0.48:

| enrolled from | similarity | run-on generations |
|---|---:|---:|
| first 6 s (upstream) | 0.79 (0.88 without run-ons) | 2 / 6 |
| each other 6 s window | 0.87 – 0.94 | 0 |
| mean latent of 14 windows | 0.91 (0.95 without run-ons) | 1 / 6 |
| **whole 47 s, one pass** | **0.93** | **0 / 6** |

Longer input buys consistency rather than a higher ceiling. Pooling window
latents scored similarly but needs graph surgery; the one-pass input needs
none. **Background matters more than length.** A 120 s stretch with a music
bed under the voice scored 0.88 whether enrolled whole or from its first 6 s;
the whole clip only narrowed the spread (0.86–0.92 vs 0.79–0.93). The encoder
costs 269 ms on 120 s against 26 ms on 6 s, and decoding the upload dominates
either way.

### 2. The decode path is a separable ~385 MB subset

`BiCodec.detokenize()` calls `quantizer.detokenize`,
`speaker_encoder.detokenize` (FSQ dequant + one `Linear`), `prenet`, `decoder`.
Weight groups parsed straight out of the safetensors header:

| group | MB (fp32) | needed to decode? |
|---|---:|---|
| `decoder` (WaveGenerator) | 210.5 | yes |
| `prenet` (VocosBackbone) | 157.5 | yes |
| `speaker_encoder.project` | 16.8 | yes |
| `quantizer` | 0.4 | yes |
| `speaker_encoder` ECAPA + perceiver | 39.5 | enrollment only |
| `encoder` | 122.0 | **no** |
| `postnet` | 78.7 | **no** |
| **total** | **625.4** | **≈385.2 decode** |

A forward-hook trace over `detokenize()` confirms `encoder`, `postnet` and
`mel_transformer` never fire. (`quantizer` and `speaker_encoder` also show as
unfired, but that is a hook artifact — `detokenize()` calls their methods
directly rather than `forward()`.)

Decode cost on CPU, cold: **1.83 s for 9.94 s of audio — 0.18× realtime**, and
1.83 s to load the codec. The vocoder is not the bottleneck and does not need a
GPU. The LLM is the bottleneck, and that is exactly the part Orb's Vulkan
llama-server already accelerates.

### 3. Both artifacts already exist prebuilt

- `mradermacher/Spark-TTS-0.5B-GGUF` — full quant ladder. **Q8_0 = 545.0 MB**,
  Q4_K_M = 411.7 MB. Architecture is `Qwen2ForCausalLM` (hidden 896, 24 layers,
  14 heads / 2 KV, vocab 166 000, tied embeddings) — ordinary llama.cpp territory.
- `Fhrozen/Spark-TTS-0.5B-ONNX` — carries `bicodec.onnx` at **385.4 MB**, which
  matches our computed decode subset to within 0.2 MB. Its own test script uses
  it as `audio_detokenizer` with inputs `semantic_tokens` / `global_tokens` and
  output `audio`, and raises `NotImplementedError` on `--clone_voice` — i.e. it
  is decode-only, as the size implies.

Neither was trustworthy on sight — the ONNX repo has 0 downloads and one
commit, and the GGUF was converted from a third-party re-upload. Both were
checked: the GGUF's source weights are byte-identical to the official
`SparkAudio/Spark-TTS-0.5B` (sha256 `54825baf0a2f6076…` across all four repos,
so `prince-canuma` is a re-layout, not a re-quantization), and `bicodec.onnx`
reproduces torch to 114 dB. See [Verification results](#verification-results).

### 4. Token IDs are contiguous — no regex needed

| family | count | index range | token-id range | contiguous |
|---|---:|---|---|---|
| `bicodec_global` | 4096 | 0–4095 | 151665–155760 | yes |
| `bicodec_semantic` | 8192 | 0–8191 | 155761–163952 | yes |

Control tokens: `<|task_tts|>` 165137, `<|start_content|>` 165146,
`<|end_content|>` 165152, `<|start_global_token|>` 165150,
`<|end_global_token|>` 165156, `<|start_semantic_token|>` 165151.

Upstream regexes `bicodec_semantic_(\d+)` out of *detokenized text*, which is
fragile — llama-server does not render special tokens into `content` by
default. Because the ranges are contiguous we can work in token IDs and do
arithmetic (`index = id - 155761`), which is both robust and cheaper.

## Architecture

Two paths that share nothing but the token vocabulary.

### Enrollment — once per character, at upload

```
user uploads audio
  → decode to 16 kHz mono float          (ffmpeg-free; see Phase 1)
  → volume-normalise, take/tile 6.00 s window
  → mel spectrogram (128 mels, n_fft 1024, win 640, hop 320, slaney)
  → speaker_encoder.onnx                 (~20 MB int8)
  → 32 ints
  → character_cards.workflow_state["tts"]["speaker_tokens"]
```

Runs in ~14 ms plus decode. The reference audio itself is **not** needed again
and does not have to be retained (we keep it anyway, so re-enrollment after a
model bump is possible without asking the user for the file again).

### Synthesis — per speech block

```
prompt = [<|task_tts|>, <|start_content|>, text, <|end_content|>,
          <|start_global_token|>, *speaker_tokens, <|end_global_token|>]
  → llama-server (Vulkan GPU flavour, existing ManagedLlamaServerHost)
  → generated token ids → semantic indices (id - 155761)
  → bicodec.onnx(semantic_tokens, global_tokens) → waveform
  → existing pcm_to_wav / silence padding in engine/wav.py
```

Note what is absent: no reference audio, no wav2vec2, no BiCodec encoder, no
torch, and no per-request re-encoding. The 32 ints are read from the DB.

### Accepted quality tradeoff

Dropping semantic tokens drops upstream's `prompt_text` in-context path. With a
transcript, the model sees one demonstrated text→audio pair and matches the
reference's prosody as well as its timbre; with global tokens only, **timbre
transfers and prosody runs free.**

This is a real quality reduction and it is chosen deliberately: it is what
removes 1.4 GB of weights and the entire torch dependency. The escape hatch is
designed in but not built (Phase 5) — if A/B listening says the gap matters, the
encoder half ships as a separate opt-in download and `speaker_tokens` grows a
sibling `semantic_tokens` field. **Phase 0 exists to make that call before we
build on the assumption.**

## Disk budget

| | today (OrbTTS sidecar) | this plan |
|---|---:|---:|
| LLM | 2026 MB fp32 | 545 MB (Q8_0 GGUF) |
| BiCodec | 625 MB | 385 MB (decode ONNX) |
| speaker encoder | — (inside BiCodec) | 23.8 MB (fp32 ONNX, measured) |
| wav2vec2 | 1269 MB | **0** |
| Python runtime | 664 MB venv (328 MB torch) | 81 MB (onnxruntime, measured) |
| **total** | **~4.5 GB** | **~1.04 GB** |

Weights alone are **954 MB**; `onnxruntime` adds 81 MB. The `onnx` package
(another 73 MB) is needed only to *export* the speaker encoder, so it belongs in
`requirements-dev.txt`, not in the user's install. Both ONNX files are
download-on-demand like every other entry in `catalog.py`, so a user who never
touches TTS pays nothing.

## Phases

### Phase 0 — settle the prosody question *(blocking; do this first)*

Narrowed by verification. **Timbre is settled** — the two modes are
indistinguishable (0.9284 vs 0.9355, inside seed noise), so identity is not
what is at stake. The open question is prosody: mode B adopts the reference's
speaking rate (~18% shorter for identical text), and whether that is an
improvement for roleplay dialogue cannot be measured, only heard.

1. Listen to the six A/B samples already generated (see
   [Verification results](#1112--what-the-expensive-path-actually-buys)).
2. If that is not decisive, record 2–3 **English** reference clips with exact
   transcripts — the only in-repo reference is a fast Chinese ad read, which is
   close to a worst case for rate matching — and repeat.
3. Judge one thing: does free-running prosody sound *wrong*, or merely
   *different*? Only "wrong" justifies +1.4 GB and the whole encoder stack.

**Output:** a go/no-go note appended to this file. If B wins, Phase 5 moves to
the front.

### Phase 1 — audio decode without torch

Orb's base dependencies are FastAPI/Pillow/httpx; there is no numpy, no
soundfile, no torchaudio. Enrollment needs to turn an arbitrary uploaded file
into 16 kHz mono float.

- Accept WAV/FLAC/OGG/MP3/M4A. Python's `wave` module covers only PCM WAV.
- **Decide:** bundle `soundfile` (libsndfile, no MP3/M4A pre-1.1), shell out to
  a user-supplied `ffmpeg`, or accept WAV/FLAC only and tell the user to convert.
  Recommendation: **WAV/FLAC via `soundfile`, plus `ffmpeg` when it is on PATH**
  — the strict path has no new binary dependency, and the common case (a phone
  recording) is covered when ffmpeg happens to exist.
- Resampling: mel needs exactly 16 kHz. Polyphase resample via `scipy.signal`
  pulls in scipy (~30 MB); a windowed-sinc in numpy is ~40 lines and enough for
  a one-shot enrollment. Prefer the latter.

`backend/workflows/tts/engine/spark/audio_in.py` — pure, unit-testable, no
model dependency.

### Phase 2 — the ONNX runtime slice

- `backend/inference/local_models/onnx_runtime/` alongside `llama_server/`,
  mirroring its shape: `binary.py`-equivalent is just `import onnxruntime`,
  plus a session cache keyed by path.
- `catalog.py`: `RuntimeKind` is currently `Literal["llama_cpp", "llama_server"]`.
  Add `"onnx"`. **`assets.prune_stale()` only deletes `.gguf`** — it must learn
  `.onnx` or the files become unclaimable garbage on a model bump.
- Register two artifacts with pinned revision shas per existing convention:
  `spark_tts_llm` (GGUF, `runtime="llama_server"`) and `spark_tts_codec`
  (ONNX, `runtime="onnx"`).
- Execution provider: CPU EP. ONNX Runtime has no Vulkan EP; Orb's Vulkan is
  llama.cpp's and lands on the LLM, which is the bottleneck.

### Phase 3 — the engine

`backend/workflows/tts/engine/spark/`:

- `tokens.py` — the ID arithmetic and prompt assembly. Pure; unit tests assert
  the exact token-id sequence for a known `(text, speaker_tokens)` pair.
- `enroll.py` — audio → mel → `speaker_encoder.onnx` → 32 ints.
- `synth.py` — prompt → llama-server → semantic ids → `bicodec.onnx` → PCM.
- `host.py` — a second `ManagedLlamaServerHost(name="spark_tts", idle_timeout=…)`.
  `manager.py` already holds a *list* of hosts, so a second resident child is a
  supported pattern, not a new mechanism. Pick a short idle timeout: TTS is
  bursty and this model is small, so paying a reload is cheaper than holding
  545 MB of VRAM against the prose rewriter's 2–4 GB.

`LlamaServerClient.generate()` currently takes `prompt: str` and returns text,
and streams SSE accumulating `content`. Both halves must change, and **this is
not optional**: every bicodec token is `CONTROL` type, so `content` arrives
empty and a text-based reader silently produces nothing.

Verified working on `b10549`, so no fallback is needed:

- send `prompt` as a **`list[int]`** — no tokenizer round-trip, no
  `parse_special`, no chance of a special token being read as literal text;
- request `"return_tokens": true` and read the `tokens` array;
- convert by arithmetic: `semantic_index = token_id - 155761`, keeping only ids
  in `[155761, 163952]`.

Rather than widen `generate()`'s signature and make every existing caller pay
for a second return mode, add a sibling `generate_tokens()` on the client. The
prose rewriter's text path stays exactly as it is.

Keep the `max_new_tokens` budgeting OrbTTS already worked out — `256 + 8 *
len(text)`, capped at 3000. It exists because a degenerate generation otherwise
runs to the 3000-token cap, costing ~2.5 minutes on CPU for a line that needs
eleven seconds. Its retry-on-degenerate loop is **not** needed: that failure is
the LLM emitting no `bicodec_global_*` tokens, which can only happen on the
control path, where the model invents its own speaker. Cloning supplies them.

### Phase 4 — storage, API, UI

- **Storage.** 32 ints is ~200 bytes; it belongs inline in the profile, not in a
  new table. Add `speaker_tokens: list[int]` and `speaker_ref_name: str` to
  `PROFILE_DEFAULTS` in `backend/workflows/tts/synth.py`. `normalize_profile()`
  iterates `PROFILE_DEFAULTS` keys, so both are picked up; it must gain a guard
  that rejects a malformed `speaker_tokens` (wrong length, out of `[0, 4096)`)
  rather than passing it to the codec.
- **Reproduction record.** `_METADATA_KEYS` is the set that lets an attachment
  be re-synthesized from a context with no character state. `speaker_tokens`
  must join it, or rerolling a cloned line silently produces a different voice.
- **API.** `POST/DELETE /api/characters/{card_id}/voice-reference`, next to the
  expressions routes in `backend/api/routes/characters.py`. POST decodes and
  enrolls the upload; the compact speaker tokens are the only retained voice
  data, and the existing Preview action generates audio when requested.
- **UI.** `frontend/workflows/tts/config_panel.js` line 29 lists the fields per
  backend. Spark's built-in entry gets a file-drop + "Preview" + "Clear" control
  instead of the voice `<select>`. The `api_url` field disappears for this
  backend — there is no sidecar.
- **Migration.** Existing `backend: "spark"` profiles point at a sidecar and
  carry a `voice_id` like `spark_female_warm`. Keep the old adapter registered
  under a distinct id (`spark_remote`) so those profiles keep working, and let
  the built-in take the `spark` name only for new profiles. Do not silently
  repoint an existing profile at a model that is not downloaded yet.

### Phase 5 — optional: the in-context path *(only if Phase 0 says so)*

Add `wav2vec2` + `BiCodec.encoder` as a third, opt-in artifact (~350 MB as int8
ONNX). Enrollment gains an optional transcript field; `speaker_tokens` gains a
sibling `semantic_tokens`. Everything else is unchanged — the prompt builder
already has the branch, because upstream's does.

## Risks

**Resolved by the verification run** — risks 1–4 of the original draft
(`bicodec.onnx` unvetted, speaker encoder unexportable, GGUF vocab loss,
llama-server plumbing) are all closed; see
[Verification results](#verification-results). What remains:

1. **Everything was measured on macOS arm64 / Metal.** The Vulkan path — the one
   that motivates this plan — is untested here. Before committing, run the same
   end-to-end script against a Vulkan build on Linux and on Windows. The LLM is
   63% of wall time, so this is where the performance story lives, and
   `gpu_build_published()` already says Windows arm64 has no Vulkan asset.
   **Linux result (2026-09-16, RTX 3090 / NVIDIA 580, b10549 and b11000):
   Vulkan was silently wrong.** For any prompt batch over 8 tokens, Vulkan's
   feed-forward results for this model are wrong, for Q8_0 and F16 alike.
   The first token should be `<|start_semantic_token|>` with p=1.0. Instead
   the model emits 1–2 s of babble for every voice and every file type. It
   was fixed by capping `--ubatch-size` at 8 (`spark_tts/config.py`). That
   matches the CPU build to within 0.07 logprob at 380 tok/s, against 55 on
   the CPU. Windows is still unmeasured.
2. **`Fhrozen/Spark-TTS-0.5B-ONNX` is one person's repo with no downloads.**
   Numerically verified, but **mirror it** rather than fetching from it at
   install time, and pin the sha256 (`a1459483cf562e9d…`) the way `catalog.py`
   pins revisions. Same for the GGUF. A third-party repo can be deleted or
   force-pushed; a verified artifact we host cannot.
3. **We must export and host the speaker encoder ourselves** — it exists nowhere
   else. Keep the export script in-repo next to the artifact so it can be
   regenerated on a model bump, and assert the determinism property in tests.
4. **Two resident llama-servers.** Prose rewriter (2–4 GB) plus Spark (545 MB)
   on a 6 GB card will thrash. The idle timeout is the mitigation; measure
   before picking a number.
5. **ONNX Runtime is a new dependency with a real footprint** and no Vulkan EP.
   Decode measured *slower* than torch here (0.84×); it is still 0.22×
   realtime, so this is acceptable, but do not sell it as a speed win.
6. **Generation is sampled and not reproducible.** Enrollment is — verified
   deterministic across runs — so the reproduction record in `_METADATA_KEYS`
   restores the voice, never the exact waveform. That is already true of every
   other backend.
7. **Chinese reference, English output worked** (cosine 0.916), but this was a
   single clip. Do not promise cross-lingual cloning without more evidence.

## Non-goals

- **Control voices** (Warm Female et al.). They come free with the same LLM and
  are worth keeping as presets, but they are not what this plan is for, and
  their `rate`/`pitch` bucketing is already solved in `OrbTTS/orbtts/voices.py`
  (`level_from_multiplier`) — port it rather than redesign it.
- **Rate/pitch on cloned voices.** Impossible by construction: attribute tokens
  live on the control path, and cloning bypasses it. The panel must grey these
  out for a cloned voice rather than send values that do nothing.
- **Chinese.** Spark-TTS is bilingual and the built-in should expose it, but the
  segmentation in `backend/workflows/tts/synth.py` is English-shaped. Out of
  scope here.
- **Replacing Kokoro / Fish / Edge.** This is one more backend, and the only one
  that is built in.

## Appendix A — probe scripts

Nine scripts produced every measurement above. They live in the session
scratchpad (ephemeral) and **should be moved into `scripts/` or a test fixture
before implementation starts**, because each one is the regression test for the
claim it proved.

| script | proves |
|---|---|
| `probe_bicodec.py` | weight-group census from the safetensors header, no torch |
| `test_split.py` | decode runs from persisted ints with no wav2vec2; module trace |
| `test_enroll.py` | mel-only speaker tokens are bit-identical to the full pipeline |
| `parse_gguf.py` / `tok_type.py` | GGUF vocab integrity and `CONTROL` token typing |
| `export_speaker.py` | the speaker encoder exports to ONNX and matches torch |
| `test_frames.py` | that export honours its dynamic frames and batch axes |
| `verify_bicodec.py` | `bicodec.onnx` == torch at 114 dB |
| `bicodec_general.py` | …and holds from 1 to 1500 semantic tokens |
| `probe_server.py` | llama-server token-ID plumbing on `b10549` |
| `e2e.py` | the whole pipeline, torch-free, 0.55× realtime |
| `fidelity2.py` | the clone carries the reference identity (0.916 vs 0.437) |
| `ab_prosody.py` | what `ref_text` buys: nothing on timbre, ~18% on rate |

Two of them (`test_enroll.py`, `ab_prosody.py`) need the OrbTTS venv, which is
exactly the dependency this plan removes — they are development tools, not
shipped code. The rest need only `onnxruntime` and a running llama-server.

**A note on method.** An early version of `fidelity2.py` hard-coded the
reference's 32 speaker tokens transcribed from a truncated `[...] ...` print,
with the last 20 values invented. It reported 0% agreement for both the clone
*and* the control — the two conditions were indistinguishable because neither
matched a speaker that existed. The bug was caught only because the script also
asserted `enroll(reference) == stored_tokens`, which failed. **Keep that
assertion.** Any fixture holding speaker tokens must be generated, never
transcribed, and must be checked against a fresh enrollment at test time.

---

## Implementation status — 2026-09-16

Phases 1–4 are built and merged into the tree; Phase 5 is not, and Phase 0
remains open because it needs ears rather than code.

### What was verified against the shipped code

Everything below was measured by running Orb's own modules, not the probe
scripts — no torch, no vendored Spark-TTS, no OrbTTS on the path.

| Claim | Result |
|---|---|
| The torch-free numpy mel matches torchaudio | max rel. error 2e-6 across five signal classes |
| …and yields the *same* 32 speaker tokens | identical on all five, including tiled short clips, noise and silence |
| Enrollment reproduces the torch reference | `enroll()` on `prompt_audio.wav` == the torch-derived 32 ints, exactly |
| Enrollment is deterministic | identical across calls and processes |
| The speaker encoder exports and holds its dynamic axes | 23.8 MB, matches torch at 51/151/301/601 frames and batch 2 |
| Streaming `/completion` returns token ids | one id per chunk on b10549, `content` empty, `stop_type: eos` |
| Synthesis, warm | **0.55× realtime** (2.28 s for 4.12 s of speech), matching the plan's figure |
| Synthesis, cold | 8.6 s including the model load |
| **The clone carries the reference identity** | **cosine 0.9310**, against **0.5790** for an unrelated speaker |

The fidelity figure is the one that matters and it is an independent
confirmation: audio produced by the shipped pipeline, re-enrolled by the
shipped enrollment, compared in d-vector space. The plan's own run measured
0.916 vs 0.437 by the same method.

### Deviations from the plan, and why

- **`spark_tts_speaker` is not a third catalog entry.** `ModelSpec` gained
  `extra_files`: companions that travel WITH an artifact rather than
  alternatives a user picks between. One Local ML card, one button, both files;
  `present()` requires both and `prune_stale` claims both. Three cards for one
  feature was clutter, and a half-downloaded codec reports ready and then fails
  at the first enrollment.
- **The engine lives under `inference/local_models/spark_tts/`, not
  `workflows/tts/engine/spark/`.** The layer checker forbids a workflow plug-in
  from importing `inference` at all (rule 4). The shape is the prose rewriter's:
  model slice below, `workflows/spark_tts_host.py` bridging, three named calls
  on `toolkit`.
- **`generate()` gained a sibling, as planned** — `generate_tokens()` — plus
  `tokenize()`, since the prompt builder needs the body tokens and nothing else
  exposed `/tokenize` with `parse_special` control.
- **sha256 pinning was added to the catalog** and is verified after download, a
  file that fails being deleted rather than kept. Risk 2 asked for it; it is the
  half of "mirror it" that can be done without a mirror.

### The panel, revisited — 2026-09-16

Phases 1–4 made the feature work; a pass over the control it is used through
closed the four gaps between "works" and "one step":

- **Enrolling is the file landing.** The panel's `<input type="file">` + Upload
  pair is a drop zone that enrols on pick or drop. `data-wf-on` now takes a
  space-separated LIST of events, because a drop target is three events on one
  element — `dragover` must preventDefault or the browser never fires `drop` —
  and the zone accepts the drag even when it cannot act on it, since refusing
  hands the file to the browser, which navigates away from the open scene.
- **The control repairs itself.** When either Local ML half is missing or
  switched off, the control says which and carries the button that downloads or
  enables both, codec first. Sending a user to a Settings page to unblock a
  control they are already looking at was the last "go configure it elsewhere"
  step in the feature. The panel fetches `/local-ml/status` itself rather than
  reading the shared map, which only the Settings page publishes — an empty map
  there is indistinguishable from "nothing is downloaded".
- **Enrolling arms the character.** The upload route already selected the
  backend and the voice id; it now also sets `enabled`, and clearing unsets it.
  "Upload one file and that character speaks from then on" was otherwise untrue
  by one checkbox, and a `spark` profile with no tokens can only fail once per
  turn.
- **Preview says why.** `_preview` returned a flat "preview synthesis failed"
  over the adapters' own messages. ValueError is their contract for a refusal a
  user can act on ("no cloned voice yet", "the model is not downloaded"), so
  that text now reaches the status line; anything else is a bug and stays
  generic.

### Still open

1. **Phase 0 — the prosody listen.** Unchanged and still blocking Phase 5, not
   Phases 1–4. Samples to judge on are in `~/Desktop/orb-voice-ab/`:
   `ab_A_*.wav` / `ab_B_*.wav` are the original A/B pair on the Chinese ad read,
   and `shipped_*.wav` are five roleplay-shaped English lines through the code
   as it now stands. The question is only whether free-running prosody sounds
   *wrong* or merely *different*.
2. **Mirroring the artifacts (Risk 2) — done for the codec, open for the GGUF.**
   Both ONNX files now come from
   [`chartreuse-verte/Spark-TTS-0.5B-ONNX`](https://huggingface.co/chartreuse-verte/Spark-TTS-0.5B-ONNX),
   pinned at `4fa08a1c` and by sha256, and a clean download of both was verified
   through `assets.download` unauthenticated. The speaker encoder is published
   there for the first time — it exists in no other public repo — and
   `scripts/export_spark_speaker_encoder.py` remains the way to regenerate it on
   a model bump. The GGUF still points at `mradermacher/Spark-TTS-0.5B-GGUF`,
   pinned by commit sha and sha256 but not yet mirrored; that upload is the one
   piece of Risk 2 left.
3. **Vulkan (Risk 1).** Everything above is macOS arm64 / Metal. The Linux and
   Windows Vulkan runs are unmeasured, and that is where 63% of the wall time
   lives.
