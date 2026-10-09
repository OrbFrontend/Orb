# Native Orb / TauriTavern comparison (Bench 1)

The harness drives Orb through its API and TauriTavern through its host Agent API, with the real prompt-assembly and chat-commit bridges. It keeps every attempt, raw model traffic, native journals and saved replies, and changes no application source.

- [360-turn sweep](results/sweep2/REPORT.md): 36 completed blocks, with a [latency and uncached-input figure](results/sweep2/figure.svg), per-turn and per-call data, [block manifests](results/sweep2/MANIFESTS.json) and a [checksum/provenance record](results/sweep2/RECORD.json).
- [Benchmark plan](../../../docs/plans/benchmarks.md).

The task contract is applied to every arm in [inspect.py](inspect.py): a direction written before the draft and used, the draft audited and re-audited after each edit batch, and the reply saved intact (TauriTavern's native whitespace cleanup counts as intact). Editor stopping, reasoning-channel output and mechanical punctuation defects are reported as observations, attributed to the draft or to editing.

Run the sweep from the committed harness on the inference host; a reportable run refuses an uncommitted `scripts/bench`:

```sh
python -m scripts.bench.cache.blocks --root "$BENCH_ROOT" --output "$BENCH_ROOT/fair/sweep"
python -m scripts.bench.cache.inspect --runs "$BENCH_ROOT/fair/sweep" --requests "$BENCH_ROOT/fair/requests" \
  --audits "$BENCH_ROOT/fair/audits" --applied "$BENCH_ROOT/pilot/orb-short/applied.json"
python -m scripts.bench.cache.report --runs "$BENCH_ROOT/fair/sweep" --output "$BENCH_ROOT/fair/reports/sweep"
```

A setup failure before any turn begins (model server, WebView session, app setup) stops the sweep and sets the block aside, so a rerun of the same command resumes it; failures once turns begin are attempts and count.
