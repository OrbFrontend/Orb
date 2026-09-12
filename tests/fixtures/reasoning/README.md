# Reasoning render corpus

Captured on 2026-09-12 from the user's llama.cpp installation. Each `.jinja` is
the complete `/props` chat template; its companion JSON records the model,
server build/capabilities, exact `/apply-template` inputs, and rendered prompts.
Only synthetic messages were used. Models were loaded sequentially with Jinja
enabled, a 4096-token context, and one slot.

- `qwen`: Qwen3.8-27B-Q4_K_M; the generation prompt pre-opens `<think>`.
- `gemma`: gemma-4-31B-it-qat-UD-Q4_K_XL; the model opens its thought channel.
- `muse`: Muse-Glimmer-30B-IQ4_XS; reasoning/replies are routed messages.

`tests/unit/test_reasoning_format.py` replays these renders through profile
discovery, prompt preparation, and stream splitting. To refresh a fixture, load
the named model and submit the four stored requests to `/apply-template`, then
inspect the resulting boundaries before updating expected behavior. The Muse
template includes a date; it is intentionally frozen in these recordings.

The JSON also contains five live streamed completions per model: reasoning on,
off, seeded reasoning, forced JSON with reasoning on, and forced JSON with it
off. Requests used temperature 0, seed 42, and a 384-token limit. Each recording
keeps the prepared prompt, parser start state, original chunks, and reviewed
reasoning/content split. Regression tests also replay each stream one character
at a time. The seeded recording contains only generated reasoning; the client's
separate prefill echo is covered by the transport tests.

Source templates are kept for provenance and cache-invalidation tests. Tests do
not interpret or search their Jinja source to determine a reasoning protocol.
