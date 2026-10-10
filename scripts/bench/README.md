# Benchmarks

Three benchmarks put a number on Orb's core claims. Each runs Orb through its public API with no application changes,
records every model request through a byte-preserving localhost recorder, and keeps the raw evidence beside its report.
No LLM judges prose quality.

| Claim | Result | Report |
| --- | --- | --- |
| KV cache keeps three passes affordable | At a 32k-token starting history, a directed, audited Gemma 4 reply takes a median 12.6 s in Orb, 35.1 s with TauriTavern handoff Profiles doing the same job, and 20.3 s with a single TauriTavern Profile. Orb's later turns prefill about 3,300 uncached tokens of a 38k prompt | [Bench 1](cache/results/sweep/REPORT.md) |
| Built for small models | 360 of 360 Orb turns on Gemma 4 and DeepSeek V4.1 Flash finished with no warning or error; the TauriTavern configurations failed 11–13% of Gemma turns natively | [Bench 1](cache/results/sweep/REPORT.md), [Bench 2](driven/results/REPORT.md) |
| The Director solves directionlessness | On open-ended user turns, 37% of Gemma 4 replies are driven with the Director on, 18% with it off (+0.18, 95% interval +0.03 to +0.33) | [Bench 2](driven/results/REPORT.md) |
| The Editor removes slop | The Editor fixes 97% of flagged slop on both models, leaves 97% of unflagged sentences byte-identical, and cuts Gemma's held-out slop, a list it never sees, by 20% | [Bench 3](slop/results/REPORT.md) |

![Bench 1: turn time and uncached input against context size](cache/results/sweep/figure.svg)

## Bench 1: Orb and TauriTavern on the same model

Orb's Director → Writer → Editor pipeline against two custom TauriTavern 2.3.0 agent configurations doing the same
task: write a scene direction, draft a reply from it, audit the draft with Orb's detectors through a shared MCP
auditor, repair it under Orb's Editor stopping rule, and save it. `tt-handoff` splits the work across three Profiles
joined by native handoffs; `tt-single` does it all in one Profile. Every arm gets the same card, persona, direction
fields, audit report and fixing rules, with cache on.

- Gemma 4 only, on one RTX 3090. Starting histories of 2k, 8k, 16k and 32k tokens, ten scripted consecutive turns,
  three repeat blocks per size and arm: 360 turns. llama-server restarts cold before every block and block order
  rotates across repeats.
- Wall time runs from the trigger to the confirmed final save. Uncached input is prompt tokens minus provider cached
  tokens, summed over every call in the turn; it matched llama-server's own timings on all 4,276 calls with usage.
- There is one frozen history per size, so results are descriptive for these configurations, not population
  intervals or a claim about every TauriTavern setup.

| Configuration | Turns meeting the task contract | Native failures | Median wall s, turns 2–10 (2k → 32k start) | Median calls per turn |
| --- | ---: | ---: | ---: | ---: |
| Orb | 115/120 | 0/120 | 9.9 → 12.6 | 4 |
| TauriTavern handoff Profiles | 101/120 | 13/120 | 24.3 → 35.1 | 16 |
| TauriTavern single Profile | 95/120 | 15/120 | 12.8 → 20.3 | 11 |

Orb's five turns that missed the contract had a Director call that left a required field empty or the mood list out.
The Orb commit Bench 2 runs keeps required fields on the `direct_scene` schema, and every one of its 120 Director
calls carried a `next_event`.

## Bench 2: Driven replies

Director on against Director off on 60 contexts, one card each, every one ending on an open, passive user turn ("I
take a seat by the stove and watch her work"). Both arms run [bench.json](driven/bench.json): every Editor detector,
agentic lorebook selection and one state fragment. Gemma 4 locally and `deepseek/deepseek-v4.1-flash` pinned to one
OpenRouter upstream, reasoning off on both: 240 turns, all clean.

Jev, an OpenRouter classifier, labels each final reply `driven`, `afterthought` or `static` with a three-way question
in [jev_check.py](driven/jev_check.py); a reply counts as driven at P(driven) ≥ 0.75, a threshold pre-registered on a
blind hand-labeled pilot. A live 12-fixture preflight gates every scoring pass, and every raw Jev answer is committed so
the numbers rebuild without calling Jev.

| Model | Driven, Director on | Driven, Director off | Paired difference (95% interval) |
| --- | ---: | ---: | ---: |
| Gemma 4 26B-A4B | 22/60 | 11/60 | +0.18 (+0.03 to +0.33) |
| DeepSeek V4.1 Flash | 23/60 | 18/60 | +0.08 (−0.05 to +0.22) |

Length does not explain Gemma's effect: its arms are within 3% in words and Director-on leads in every length
tertile. DeepSeek's Writer already drives 30% of replies on its own, and its Director tends to keep the scene where it
is, so it has less to add.

## Bench 3: Slop before and after the Editor

Bench 2's 240 turns, no new generation. The Writer's streamed draft is compared with the saved reply using Orb's own
detectors (the same `backend.analysis` code and inputs the Editor had) and a held-out list the Editor never sees:
antislop-sampler's `slop_phrases_2025-04-07.json`, pinned by commit and hash, minus every entry the seeded phrase bank
already flags.

| Model | Repair, 7 audit types | Introduced per 1k words | Held-out hits, draft → reply | Unflagged sentences kept |
| --- | ---: | ---: | ---: | ---: |
| Gemma 4 26B-A4B | 97.3% (289/297) | 0.12 | 66 → 52 (−20%) | 97.1% |
| DeepSeek V4.1 Flash | 96.6% (257/266) | 0.15 | 8 → 9 | 96.9% |

Most repairs are rewrites; 13–15% of changed flagged sentences are removed outright, and Gemma's replies lose 1.7% of
their words. DeepSeek's drafts carry almost no held-out slop to cut.

## Shared setup

- **Gemma 4 26B-A4B**, QAT `UD-Q4_K_XL` (sha256 `a7c5bc71…`), on llama.cpp `edd6e2bb`, one slot, 49,152 context,
  f16 KV, flash attention, no speculative decoding, started by [serve_gemma.sh](serve_gemma.sh). RTX 3090 at 270 W.
- **DeepSeek V4.1 Flash** on OpenRouter, pinned to `sail-research/fp4` with fallbacks off; the scorer rejects a turn
  answered by any other upstream.
- **Orb** runs from a fresh git worktree per run on port 18899, so its database builds from the seeds. The harness
  applies the settings snapshot through the API, reads every value back and refuses a mismatch; a reportable run
  refuses an uncommitted harness. Thinking is off on every pass and checked on the wire.
- Orb's seeded samplers in every request: temperature 0.8, top_k 40, top_p 0.95, max_tokens 4096, no seed.

## Files

| Path | Purpose |
| --- | --- |
| [recorder.py](recorder.py) | Byte-preserving localhost recorder between the application and the model server |
| [serve_gemma.sh](serve_gemma.sh), [smoke_llama.py](smoke_llama.py) | Start llama-server with the pinned flags; check its cache reuse and prefill speed before a run |
| [auditor.py](auditor.py) | MCP auditor that runs Orb's Editor audit for TauriTavern, checked for parity with the native Editor |
| [cache/](cache/) | Bench 1: frozen histories, Orb and TauriTavern drivers, block runner, scorer, report and figure |
| [driven/](driven/) | Bench 2: corpus, settings snapshot, runner, Jev question and preflight, scorer, judge-noise re-ask, hand-label sheet |
| [slop/](slop/) | Bench 3: scorer over Bench 2's runs |

## Reproducing

Run from the project root on the inference host, with the main checkout's `.venv`. Each runner pins the Orb commit it
benchmarks and builds its own worktree.

Bench 1 reads a prepared `$BENCH_ROOT`: TauriTavern at the pinned commit with its release build in `tauritavern/`,
the frozen histories in `fixtures/` ([cache/fixture.py](cache/fixture.py)), and under `pilot/` the model hash
(`model.sha256`), Orb's applied settings from `orb_driver prepare` (`orb-short/applied.json`), and the recorder and
auditor bindings. The recorder (port 5001) and auditor (port 5002) must be running from the harness checkout, writing
to `requests/` and `audits/`; tauri-driver must be installed.

```sh
# Bench 1
python -m scripts.bench.cache.blocks --root "$BENCH_ROOT" --output "$BENCH_ROOT/sweep"
python -m scripts.bench.cache.inspect --runs "$BENCH_ROOT/sweep" --requests "$BENCH_ROOT/requests" \
  --audits "$BENCH_ROOT/audits" --applied "$BENCH_ROOT/pilot/orb-short/applied.json"
python -m scripts.bench.cache.report --runs "$BENCH_ROOT/sweep" --output "$BENCH_ROOT/reports/sweep"

# Bench 2: one run per transport (gemma, deepseek); Jev needs its key, see jev_check.py
python -m scripts.bench.driven.run --transport gemma --contexts 60 --model-sha256 MODEL.sha256 --output RUN
python -m scripts.bench.driven.score --run RUN --output SCORES
python -m scripts.bench.driven.noise --scores SCORES/turns.json.gz --run RUN --output NOISE

# Bench 3: on Bench 2's run folders, with the held-out list from the URL in slop/score.py
python -m scripts.bench.slop.score --run GEMMA_RUN --run DEEPSEEK_RUN --heldout PHRASES --output OUT
```

A setup failure before a Bench 1 block's first turn sets the block aside and a rerun resumes it; failures once turns
begin are attempts and count. `driven.run --resume` continues a stopped Bench 2 run in place.
