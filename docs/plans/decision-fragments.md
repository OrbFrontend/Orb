# Decision fragments — implementation plan

Status: design, ready for a bounded prototype. Existing synthetic probes support
trying the feature; usefulness on actual Orb conversations remains a release gate.

A **decision** is an interactive fragment that asks one question about the scene,
resolves the answer, and supplies its own authored guidance. The first release
supports yes/no questions before the Director, with either a threshold or a
weighted roll. The Director plans around the resolved outcome, and the Writer
receives the same guidance.

The authoring flow is: **write a question, choose its input, write guidance for
each answer, choose a fallback, and enable the fragment.**

## Scope and invariants

- One fragment contains one question. No nested decisions, dependencies between
  decisions, expression language, or references to other fragment IDs.
- A decision controls only its own guidance. An empty output means no injection.
- Decision fragments never become Director tool properties. Their results appear
  in trailing context, preserving the stable tool schema and shared prefix.
- Pipeline placement is explicit. `sort_order` orders fragments within a stage;
  it does not position the Director or Writer in a fragment list.
- Every decision at one stage reads a frozen input snapshot. Results from that
  stage cannot affect another decision in the same stage.
- Classifier responses, random draws, and authored outputs have separate
  lifecycles. Response caching does not implement regeneration replay.
- Disabled fragments and unapproved card fragments have no effect and make no
  request. Enabled fragments that cannot evaluate use their authored fallback.
- Existing fragments retain their API, ordering, cooldown, and SSE behavior.

The first release includes solo and group chat, card import/export, fallback
handling, regeneration replay, and basic Inspector support. Document Mode,
`choice`, `score`, after-Director decisions, and after-Writer actions are outside
that release.

## Provider contract

Use a small classifier configuration referencing an existing OpenRouter endpoint
row for credentials. Do not put classifier settings in `model_configs`:
chat-generation parameters and Writer/Agent roles do not apply.

The repository's probes use this OpenRouter route:

```text
POST https://openrouter.ai/api/alpha/decisions
Authorization: Bearer <configured OpenRouter endpoint key>
```

An illustrative first-release request is:

```json
{
  "model": "typesafe/jev-1.13",
  "state": "Alric attempts to force Maren back from the doorway.",
  "questions": {
    "outcome": {
      "type": "noul",
      "instructions": "Does Alric prevail in this exchange?",
      "criteria": {
        "true": "Alric ends the exchange in control of the doorway.",
        "false": "Alric is driven back or forced to disengage."
      }
    }
  }
}
```

The response contains `answers.outcome.noul`, a finite number in `[0, 1]`.
Normalize the response in the adapter and retain the returned model identifier,
usage, and provider request metadata when available. Missing, malformed, or
out-of-range answers are failures for the affected question, never implicit false.
Do not coerce booleans or numeric strings into valid probability values.

TypeSafe defines Noul as the probability that a proposition is true, with optional
true/false criteria. Orb requires both descriptions to make each outcome explicit.
Using that probability as a narrative roll weight is an Orb policy, not a claim
that fictional outcomes have empirically calibrated odds.
[Source: Noul](https://docs.typesafe.ai/primitives/noul).

Questions in a request are evaluated independently against one shared state.
This is the basis for batching identical inputs.
[Source: TypeSafe introduction](https://docs.typesafe.ai/introduction).

Before implementation depends on the adapter, verify the selected OpenRouter
model, accepted request shape, question-count and payload limits, usage fields,
and behavior when one question is invalid. Save sanitized request/response
fixtures. The connection test sends a synthetic scene, not conversation content.
Prefer a verified version-specific model ID when supported. Record the actual
returned version even when the requested name is an alias.

## Fragment definition

Keep authoring fields on `interactive_fragments`, with matching API schemas and
card serialization. These fields are nullable for other fragment types and
validated together for `field_type = 'decision'`.

| Field | First-release contract |
|---|---|
| `decision_type` | `noul` |
| `decision_placement` | `before_director` |
| `decision_state_template` | Nonempty template using the supported decision macros |
| `decision_instructions` | Nonempty question text |
| `decision_criteria` | JSON object with exactly `true` and `false`, both nonempty descriptions |
| `decision_outputs` | JSON object with exactly `true` and `false`, both strings; either may be empty |
| `decision_default` | Required outcome key: `true` or `false` |
| `decision_resolution` | `threshold` or `roll` |
| `decision_threshold` | Finite number in `[0, 1]` for threshold mode; null for roll mode |

Reuse `id`, `label`, `injection_label`, `enabled`, `sort_order`, and
`cooldown_turns`. `description` remains an optional author-facing explanation.
The question has its own field. A reusable definition contains no random seed,
provider credential, evaluation result, or local import approval.

JSON columns are decoded at database read boundaries. Database flags remain
integers. Update schema definitions, migrations, row models, query write lists,
API contracts, seeds where applicable, and card validation together.

Unknown decision variants are rejected by the authoring API and skipped when
reading untrusted cards. They must not be converted into ordinary string fields.
Preserve existing behavior for unrelated fragment types.

## Input construction

A decision-scoped renderer receives explicit turn data from the pipeline. It does
not give `core/macros.py` access to `PipelineContext`. Keep its supported macro
set explicit and validate availability both when saving and at execution time.

| Macro | Meaning before the Director |
|---|---|
| `{{last_message}}` | Current user request; empty on a continuation without new user text |
| `{{last_assistant_message}}` | Latest assistant message on the input branch, with a speaker label in groups |
| `{{recent_history}}` | Last four completed messages on the input branch, excluding the current request, oldest first and role/speaker labelled |
| `{{user}}` | Persona name under the same precedence as the main prompt |
| `{{char}}` | Solo character name, or group title for the exchange-level stage |
| `{{cast}}` | Group roster names; empty in solo chat |
| `{{description}}` | Solo character description; unavailable at the group exchange stage |

The default template is concrete and requires no summarizer:

```text
Previous reply:
{{last_assistant_message}}

Current request:
{{last_message}}
```

These are excerpts of existing narrative, not extracted facts. Missing prior
history renders as an empty previous reply. With the default template, if both
message inputs are empty, use the fallback with an `empty_input` reason. Custom
templates may supply an explicit situation without these messages. Additional
steering supplied by Magic Rewrite or super-regenerate is included in the current
request projection and its fingerprint; it must not disappear from the
classifier's view.

Use the existing prompt-channel message projection for names, card scripts, and
speaker attribution. Do not send attachment binaries; use existing textual
attachment annotations. Decisions have access only to the explicitly supplied
snapshot, not the filesystem, arbitrary database fields, or hidden member sheets.

Resolve authored template tokens once and insert message bodies as opaque values.
Do not recursively execute macro-like text inside messages or descriptions.
Instructions, criteria, and output text may use `{{user}}`, `{{char}}`, and
`{{cast}}`; resolve these before fingerprinting or injection. Random, clock, and
date macros are not supported in decision-authored fields in the first release.
Backticked macro examples remain literal, following the existing macro convention.

Templates referencing `{{scene_guidance}}` or `{{draft}}` are invalid for
`before_director`. A solo fragment using `{{description}}` in a group uses its
fallback with `unavailable_context`; never infer an owning or speaking member.
The editor explains this restriction before enabling the fragment in that scope.

Show a preview of the actual rendered state and its size. Initial application
limits are 16 KiB of UTF-8 rendered state and 8 KiB for one rendered question
including criteria. Oversized inputs use the fallback and a visible reason;
never silently truncate potentially decisive facts. These are conservative Orb
limits, to be verified against the adapter before release.

## Stage placement and consumers

The first-release path is:

```text
prepare context → decisions → Director → Writer → Editor
```

Run `before_director` after context, branch history, persona, and applicable
fragments are resolved, but before the Director's first request. Freeze the input
snapshot, evaluate all eligible decisions, then publish their outputs together.

Sort within the stage using the existing global/card merge and stable fragment
ordering. Card ordering remains local to its stage, including the existing offset
that places card fragments after globals. Reordering decisions changes guidance
order and budget priority, not another decision's input.

For each nonempty selected output, append a labelled resolved outcome and its
authored guidance to the Director's trailing request and to the Writer's Scene
Guidance. Empty output suppresses both injections. Keep this contribution
separate from Director-returned fields so parsing the Director's response cannot
overwrite it. If the Director is disabled, the Writer still receives the guidance.

Do not put probabilities, random draws, or provider errors in model prompts.
The Inspector carries those details. A rolled outcome is presented as an authored
story constraint. A threshold outcome is presented as the selected guidance;
neither grants the fragment authority to change system instructions.

Follow [prompting](../architecture/prompting.md) and
[KV-cache](../architecture/kv-cache.md) contracts. Stable tool schemas prevent
schema-driven invalidation; dynamic trailing guidance still has prompt cost.

### Group scope

Before-Director decisions run once per exchange, alongside the group Director.
Their state is scene-wide and has no selected speaker. Respect the public context
projection in Private and Swap modes; a card contributing a decision does not
make that card's private sheet visible to the stage.

Carry resolved guidance into every reply in the exchange without reevaluation or
rerolling. Copy the exchange evaluation records onto each persisted reply so any
reply remains independently inspectable and regenerable. Preserve the shared
occurrence IDs and identify the original input branch anchor in those records.
Advance decision cooldowns once for the exchange, not for each speaker.

A same-speaker regeneration uses the target's original exchange input anchor to
reconstruct decision state, including current steering where supplied. It must
not substitute later speakers' messages for the original exchange input. Follow
[group chat](../architecture/group-chats.md) scope and privacy contracts.

## Resolution and fallback

For threshold mode, resolve to `true` when `p >= decision_threshold`, otherwise
`false`. The equality rule is part of the contract and is tested explicitly.

For roll mode, draw one uniform value `u` in `[0, 1)` per decision occurrence and
resolve to `true` when `u < p`. Persist `u` and the result. A new occurrence gets a
fresh draw even when its classifier response comes from cache. Probability zero
always fails; probability one always succeeds.

The author must choose a fallback outcome. The selected fallback output can be
empty. Use it for missing configuration, unavailable context, oversized input,
budget exhaustion, transport failure, timeout, or an invalid/missing answer.
Do not fabricate a probability for fallback results.

A disabled or unapproved fragment is skipped rather than resolved to fallback.
A fragment on cooldown is also skipped: no request, outcome injection, fallback,
or reuse of an earlier turn's guidance. User cancellation stops work; it is not a
provider failure and must not trigger fallback guidance or continue generation.

## Batching, cache, and budgets

At each stage:

1. Apply enablement, card approval, and cooldown checks.
2. Render and validate each decision independently against the frozen snapshot.
3. Reuse matching target evaluation records on regeneration.
4. Look up raw classifier responses for the remaining questions.
5. Group misses by stage/scope, endpoint/model configuration, and identical
   rendered state. Pack questions within verified provider and Orb limits.
6. Issue requests within the remaining budget, resolve valid answers, and apply
   per-question fallbacks. Publish outputs in fragment order.

Adjacency is neither required nor sufficient for batching. Never concatenate
unrelated states to force a batch. Independent groups run sequentially in the
first release for simple cancellation and budget accounting. All stage results
remain invisible until the stage finishes, regardless of request completion order.

The raw-response cache key includes adapter contract version, endpoint/provider
identity, configuration revision, requested model, exact rendered state, question
type, rendered instructions, and criteria. Canonical serialization must preserve
meaningful ordering. Do not key only on state or criteria, and do not include
credentials. Do not normalize prose whitespace or case.

Cache only validated raw answers, never fallback results or rolls. Store the
returned model version with the answer. Keep the cache bounded, process-local,
and short-lived: initially 512 entries with a ten-minute TTL. Changing classifier
configuration clears its namespace. An alias remains subject to upstream changes;
a cache hit deliberately reuses the recorded answer within that lifetime.
Persisted evaluation replay is independent of cache retention.

Initial server-side budgets, to be validated during the prototype:

| Budget | Value |
|---|---|
| Eligible decisions per exchange | 32, including at most 8 from any one card |
| Questions per outbound request | At most 16, or the verified provider limit if lower |
| Serialized request body | At most 64 KiB, within the provider's verified token limit |
| Outbound request attempts per exchange | 4 |
| Timeout per request | 3 seconds, capped by remaining exchange decision time |
| Total time spent in decision evaluation per exchange | 6 seconds |

Count actual requests after grouping and packing. Keep question and input limits
as well as request limits. Deterministic ordering decides which eligible
fragments fit; over-budget enabled fragments use their defaults and appear in the
Inspector. Apply hard schema/import bounds before render or logging so large
card payloads cannot bypass these budgets.

No automatic retry or split-and-retry occurs in the interactive path. A failed
batch falls back for its unanswered questions. Local per-question validation
prevents known bad definitions from reaching the provider; a partially valid
response can still supply its valid answers. Connection failures and rate limits
must not create unbounded extra attempts.

Measure latency over whole decision stages. Summing sampled p95 values is not a
measured end-to-end percentile. Two sequential 3-second timeouts can consume the
full 6-second budget; observed successful calls do not establish a worst case.

## Persistence and regeneration

Store versioned `decision_evaluations` JSON on messages and include evaluations in
conversation logs. Define the row shape in `database/models.py`. An evaluation
contains:

- fragment identity and source, placement, scope, occurrence ID, and input branch
  anchor;
- rendered state, rendered question and criteria, raw-request fingerprint,
  resolution-policy fingerprint, and applicable authored output snapshot;
- requested and returned model identifiers, validated raw answer, probability,
  random draw when applicable, resolved outcome, and rendered guidance;
- source (`live`, `cache`, `replay`, or `fallback`), fallback/skip reason where
  applicable, request correlation ID, elapsed time, and available usage/cost.

Record skipped decisions for Inspector diagnostics without inventing an outcome.
Batch usage belongs to the shared request record; do not count the full batch
cost once per fragment. Never store API keys in these records.

The raw-request fingerprint covers classifier input. The resolution-policy
fingerprint covers placement/scope, renderer contract version, threshold or roll
mode, threshold value, and fallback outcome. Labels and output guidance are not
classifier inputs. Each new record snapshots the actual guidance used.

On regeneration:

1. Restore branch cooldown baselines from the state before the target evaluation.
2. Load the target reply's own evaluation records explicitly. The previous
   assistant message's baseline does not contain the target's outcomes.
3. Recheck current enablement, local card approval, and cooldown eligibility.
4. Re-render input at the target's evaluation scope. If fragment identity, raw
   request, and resolution policy match, reuse the target's raw answer, draw,
   and outcome without a request. Preserve fallback outcomes on a matching
   replay as well; regeneration alone does not retry a failed classification.
5. If input or resolution policy changed, create a new occurrence. A matching raw
   cache answer may still be reused, but roll mode gets a new draw.
6. Resolve the current output mapping for the selected outcome. Editing guidance
   or labels can change the prompt without another classifier call or reroll.

Fresh send, continue, and fork-edit operations create new occurrences even for
identical input text. Repeated regeneration of an unchanged reply preserves its
outcome. Checkpoints and branch copies retain evaluation snapshots and remap
branch anchors through the existing message-copy mapping; missing anchors must
produce an explicit invalidation reason, never select unrelated history.

Persist evaluations and decision cooldown changes atomically with a retained
reply. A stop after partial Writer output retains the decisions that produced
it. Cancellation before a reply, a Director rest result, or a generation failure
without a retained reply leaves diagnostics in logs but commits no decision
cooldown state. Retrying such an unsaved attempt creates fresh occurrences.

### Decision cooldown semantics

For decisions, `cooldown_turns = N` means **skip the next N completed exchanges
after an evaluation**, regardless of the answer or whether output was empty.
Live, cached, replayed, and fallback evaluations all start that cooldown when
their reply is retained. Disabled, unapproved, and already-resting decisions do
not start it. The editor uses this wording rather than promising a particular
API call frequency.

Keep decision cooldown snapshots separate from Director fragment cooldowns,
whose firing and group advancement rules remain unchanged. Restore the decision
snapshot from before the original exchange on a group regeneration; a later
speaker's immediate parent already carries the exchange's advanced snapshot.
Copies of a shared evaluation do not advance it again. Factor helpers only where
the state semantics are actually shared.

Age existing decision cooldowns on every completed exchange with a retained
reply, including exchanges with no eligible decisions. Keep this advancement in
the exchange persistence path so disabling all decisions cannot freeze timers.

## Editor, imports, and Inspector

Decision fragments remain visible and editable without a configured classifier.
Show the endpoint status and explain that enabled decisions use their fallback
until configuration is available. A preview may render locally; running a test
against the provider is an explicit editor action and changes no chat cooldowns
or evaluation history.

Endpoint setup explains that rendered scene text is sent to the selected external
provider. Card decisions require a local per-card approval before they can run.
Store approval outside exported card data; an imported `enabled` flag cannot
supply it. Changes to a card's decision definitions invalidate that approval.
Import validation preserves valid disabled decisions for inspection and skips
invalid definitions. Apply approval before response-cache or replay lookup.

Basic Inspector support ships with evaluation. Show the exact rendered state,
question, outcome, selected guidance, probability and draw, timing, response
source, and fallback/skip reason. Show shared request usage once and label missing
cost data as unavailable. Imported fragments must be traceable to their card.
The editor's preview and Inspector use the same rendering contract.

## Architecture and implementation touch points

Read [prompting](../architecture/prompting.md),
[KV-cache](../architecture/kv-cache.md),
[group chats](../architecture/group-chats.md), and
[SSE](../architecture/sse-stream.md) before implementation.

| Area | Responsibility |
|---|---|
| `backend/inference/jev.py` | Provider transport, request serialization, strict response normalization, raw-response cache |
| `backend/pipeline/passes/decisions/` | Input snapshots, decision-scoped rendering, eligibility, batching orchestration, budgets, resolution, replay matching |
| `backend/pipeline/orchestrator.py`, `entrypoints.py` | Solo stage and once-per-exchange group placement; explicit regeneration target records |
| `backend/pipeline/config.py`, `prompting/tool_schemas.py` | Exclude decisions from Director tool fields and unrelated fragment consumers |
| Director prompts and `prompting/scene_direction.py` | Append resolved guidance at the tail; preserve existing fragment output bytes |
| `backend/pipeline/state.py`, `persistence.py` | Carry evaluations and decision cooldown snapshots to atomic message persistence and logs |
| Database models, queries, migrations, schema | Definitions, classifier config, local card approval, evaluation/cooldown snapshots, branch-copy handling |
| `backend/api/schemas.py`, fragment/settings routes | Typed validation and configuration/preview/test contracts |
| Fragment and card editors | Definition fields, fallback, preview, endpoint status, local approval |
| `frontend/chat_inspector.js` and stream contracts | Evaluation visibility without changing existing event meanings |

Pass-specific prompt construction stays beside the pass. Only shared,
deterministic model-facing rendering belongs in `prompting/`. Database writes
remain behind query boundaries and are committed by pipeline persistence;
`inference/` does not import pipeline or database modules. Backend and frontend
layer checkers must pass unchanged.

## Evidence and validation gates

Existing experiments are in [probe_jev.py](probe_jev.py),
[probe_jev_stability.py](probe_jev_stability.py), and
[probe_jev_ladder.py](probe_jev_ladder.py). Reported synthetic results include
standard deviation around 0.006 over six identical calls, around 0.025 over five
paraphrases, and successful-call median latency around 0.6 seconds. These are
small-sample observations, not accuracy, determinism, or latency guarantees.

Some tested padding lowered the answer by roughly 0.08. This motivates testing
real narrative inputs; it does not establish that all extra prose dilutes every
answer. Context can supply decisive facts. The default template must earn its
place through evaluation rather than assuming author-written summaries exist.

Before releasing the feature:

- Pin and fixture the gateway contract, including mixed valid/invalid questions,
  missing answers, probability boundaries, payload limits, and cancellation.
- Evaluate held-out Orb scenes using the actual renderer and default template.
  Cover solo and group inputs, continuations, steering, contradictory or missing
  facts, and outcomes on both sides of thresholds. Set expected classifications
  or acceptable narrative outcomes before looking at predictions.
- Vary one fact at a time for directional checks. Repeat cases near thresholds;
  test padding at both low and high starting probabilities and multiple lengths.
- Compare story quality with decisions enabled and disabled. In roll mode,
  inspect whether the authored guidance produces coherent consequences; do not
  infer probability calibration from output spread.
- Measure whole-stage latency, timeouts, request count, and usage under realistic
  state sizes and multiple distinct templates. Save model IDs and raw sanitized
  results with the evaluation report.

Required implementation regression coverage:

| Contract | Cases |
|---|---|
| Rendering | Exact default input; no recursive macro expansion; missing scope; size fallback; branch and group privacy |
| Batching/cache | Different states split; identical states share; instructions/type/criteria/model changes invalidate; invalid sibling answer is isolated |
| Resolution | Threshold equality; `p = 0/1`; one fresh draw per occurrence; empty output; fallback without probability |
| Replay | Identical regeneration; cold cache; changed question/input/policy; output-only edit; fallback replay; branch/checkpoint copy |
| Groups/cooldowns | One exchange evaluation; no repeated draw or decrement per speaker; later-speaker regeneration; skipped versus evaluated decisions |
| Pipeline | Director enabled/disabled; guidance survives Director parsing; stable schemas; cancellation before and after partial output |
| Imports/Inspector | Local approval cannot be imported; changed definitions revoke it; malformed decisions skip; shared usage is not double-counted |

Run narrow tests while iterating, then repository formatting, lint, and tests:

```sh
./scripts/format_backend.sh
./scripts/format_frontend.sh
./scripts/lint.sh
./scripts/tests.sh all
```

Review the final diff and keep Pyright at zero errors.

## Delivery sequence

1. **Contract and input experiment.** Verify the gateway, save fixtures, implement
   the pure renderer in isolation, and test the actual default on held-out scenes.
   Resolve provider limits and input quality before exposing authoring controls.
2. **Complete first slice.** Ship configuration, Noul definitions, threshold/roll
   resolution, the before-Director stage, group scope, budgets, card approval,
   replay, cooldowns, and Inspector together. Keep incomplete authoring paths
   unavailable until the full behavior is implemented.
3. **Conversation trial.** Evaluate the first slice against the release gates.
   Tune defaults and limits from observed failures and story outcomes. Publish
   the measured limits and model version used in the trial.
4. **Broaden only after the trial.** Add `choice`/`score` or `after_director` as
   independently tested increments with their own schema and consumer contracts.

For a future `after_director` stage, freeze the completed Director output once
per exchange, make `{{scene_guidance}}` available, and inject selected output into
the Writer's tail. Earlier decisions can influence it through the Director;
decisions within the stage remain independent. Define unavailable Director output
explicitly rather than using stale guidance from a previous turn.

A future `choice` maps the returned key to authored guidance. For `score`, start
with one output per level and an explicit resolution policy. A weighted mean can
fall between levels or on a level with little probability: nearest-level mapping
and highest-probability-level selection have different semantics. Choose and test
the policy before adding editor fields; custom threshold bands are not required
by the API. [Source: Score](https://docs.typesafe.ai/primitives/score).

After-Writer decisions require a defined Editor consumer and speaker scope. They
are a separate design task; appending Writer guidance after generation cannot
edit a draft. Gating another fragment, scheduling arbitrary pipeline stages, and
introducing decision-to-decision dependencies are outside this plan.
