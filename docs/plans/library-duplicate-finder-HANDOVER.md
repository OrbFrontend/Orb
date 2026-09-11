# Library Duplicate Finder — handover

State as of this session. **All seven steps are implemented, tested, and the
full suite is green (3056 passed, `./scripts/lint.sh` clean).** The note remains
as a concise record of the design decisions that shaped the shipped slice.

## Done

### Step 1 — pure detection core ✅

- `backend/features/library_dedupe/matching.py` — pure, no I/O, no Pillow.
  `CardSignals`, `Pair`, `build_blocks`, `candidate_pairs`, `jaccard`,
  `hamming`, `score_pair`, `reasons_for`, `group_strong_edges`,
  `find_duplicates`. All five thresholds are named module constants.
- `backend/features/library_dedupe/fingerprint.py` — carries the Pillow
  dependency. `DEDUPE_REVISION = 1`, `normalize_field`, `field_hashes`,
  `body_hash`, `shingles`, `shingle_sketch`, `dhash_from_image_bytes`,
  `signals_for`, `signals_for_all`.
- `backend/features/library_dedupe/__init__.py` — facade with explicit `__all__`.
- `tests/unit/test_dedupe_fingerprint.py` + `tests/unit/test_dedupe_matching.py`
  — **37 tests, all passing.** Every named test from the brief is present.

Two deviations from the brief, both deliberate and commented in-code:

1. **`CardSignals` lives in `matching.py`, not `fingerprint.py`.** The brief
   requires `matching.py` to be Pillow-free; if the dataclass lived beside the
   decoder, importing the matcher would pull Pillow in. `fingerprint` imports
   `matching`, never the reverse.
2. **`MAX_BLOCK = 32` chaining.** The brief covers the *empty*-value blow-up but
   not the non-empty one: a popular copy-pasted `system_prompt` shared by 500
   cards is 124k pairs to score. Blocks over 32 members emit a chain instead of
   every pair. Sound because a block is cards agreeing *exactly*, so the
   predicate is transitive — union-find still collapses the group. Covered by
   `test_an_oversized_block_degrades_to_a_chain_instead_of_every_pair`.

**Thresholds were re-verified empirically**, not taken on trust. On synthetic
512×768 high-frequency art: JPEG q82 → 3, WebP q80 → 2, 3× downscale → 1,
2× downscale → 2. All well inside `AVATAR_MAX_DISTANCE = 8`.

The unit-test bodies are card-sized (~170 shingles) on purpose. The Jaccard
thresholds are calibrated for real card prose — a two-sentence fixture makes a
one-word edit look like a rewrite, which is what made the first draft of these
tests fail. Measured on the real fixtures: one-phrase edit → 0.94 (strong),
two-phrase → 0.86, truncated-to-two-thirds → 0.66 (possible).

### Step 2 — schema + migration ✅

- `backend/database/schema.py` — `character_cards.avatar_dhash` /
  `avatar_dhash_stamp`, and the `duplicate_dismissals` table. Both carry the
  reasoning comments from the brief.
- `backend/database/migrations/0059_library_dedupe.py` — mirrors 0058 exactly
  (`table_create_sql`, `PRAGMA table_info` guard). **Both migration gates pass.**

**One gate the brief did not anticipate:**
`tests/integration/test_preset_schema_coverage.py::test_signature_covers_every_domain_table`
forces every new table into either the preset round-trip signature or a
documented allowlist. `duplicate_dismissals` was **allowlisted** — every row
names two card ids and stamps their body hashes, so it is meaningful only
against this install's library; a preset applied elsewhere would carry
dismissals for pairs that do not exist there. Rationale is in the allowlist.

## Steps 3–7 — shipped ✅

- `backend/database/queries/library_dedupe.py` now provides the narrow scan
  cache, dismissal, impact, and relink queries; `get_card_activity()` sits with
  the general card queries and all new database functions are exported through
  the database facade.
- `/api/library/duplicates/*` provides SSE scanning, two-card comparison,
  reversible dismissals, and guarded resolve. Scans share the auto-tag run
  lock, and avatar-cache writes use an `updated_at` CAS without changing it.
- The Manager has the Duplicate finder panel, pure L1 render helpers, delegated
  actions, inline comparison, immediate dismissal undo, and the optional
  request body support needed for `DELETE /duplicates/dismiss`.
- Feature documentation is in `docs/features/library-duplicate-finder.md`,
  registered in the MkDocs App and data navigation; the completed item was
  removed from `docs/plans/plans.txt`.
- `tests/integration/test_library_duplicates.py` and
  `tests/frontend/library_dedupe_view.test.mjs` cover scan caching, dismissal
  expiry, auto-tag compatibility, guarded relinking, group-member collisions,
  result tiers, and compare rendering.

## Verification state

```
./scripts/lint.sh      # clean — ruff, pyright, both layer checks, biome, 366 node tests
./scripts/tests.sh all # 3056 passed
```
