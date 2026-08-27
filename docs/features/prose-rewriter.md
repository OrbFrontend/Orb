# Prose Rewriter

A small language model that runs **on your own machine** and rewrites the writer's
paragraphs before the Editor audits them. It targets *texture* — the cadence, the
stock constructions, the tells that mark a paragraph as machine-written — which is
a whole-paragraph rewrite job the Editor's surgical patching cannot do.

It works two ways: automatically, inside every turn's Editor pass, and
[on demand](#on-demand-after-the-fact) from the toolbar under a reply you already
have.

It is off by default and downloads nothing until you ask it to.

## What it actually does

The models were fine-tuned for one task: take a paragraph of LLM prose, return the
same paragraph as a human would have written it, at the same length. They were
trained on paired human/LLM fiction, whole paragraphs at a time — **dialogue
included**.

Be clear about the limits:

- **Fictional English prose only.** It has seen nothing else. Technical writing,
  other languages, and lists come back worse than they went in.
- **It will not defeat AI detectors**, and it is not meant to. It makes prose read
  better; that is the entire claim.
- Paragraphs under 80 bytes and over 512 tokens **pass through untouched**. Both
  are edges of the training distribution — asked to rewrite a two-word line, the
  model pads toward its learned length and invents.

## Where it runs in the turn

```
Director → Writer → [ Prose Rewriter → Editor audit → patches ] → workflows
```

The rewriter runs **first inside the Editor pass**, before the audit. That order is
deliberate: Orb's scanners must see the prose that will actually be saved, and the
patcher anchors its edits to byte offsets in the exact string it was given. Rewriting
afterwards would leave every finding pointing at text that no longer exists.

Consequences worth knowing:

- **The Editor's diff gets bigger.** With `Show editor diff` on, you now see the
  rewriter's changes rendered as editor changes — which is the point, but it is a
  paragraph-scale diff rather than the sentence-scale one you are used to.
- **The length guard judges the rewritten draft.** `match` mode is
  length-preserving by design, so this should be a wash.
- **Group chats run it once per speaker.** The model stays loaded across speakers,
  so only the first pays the load.
- **It is independent of the Agent toggle.** With the Director and Editor LLM
  passes off, a rewrite-only turn costs one *local* generation and makes no remote
  call at all.
- **Writing-mode documents are unaffected** — they run their own auditor and never
  enter this pass.

## On demand, after the fact

The rewriter also runs from the toolbar under any assistant reply, on the message
you already have. The button appears on any reply with text, once the feature is
switched on and a model is selected.

**Where it rewrites *from* depends on the reply.** Orb keeps each reply's Writer
output — before the in-turn rewriter, before the Editor, with inline macros
already frozen — and that retained draft is the source when there is one, which
makes the button a *redo from source* rather than a second pass on the current
text. When there is no retained draft, the reply itself is the source. The
tooltip says which: *Rewrite original Writer draft* or *Rewrite this message*.

- **Editor patches on that reply are discarded** when it rewrites from the draft.
  The audit ran against the old prose; its findings do not describe the new prose,
  and re-applying them would be patching text that no longer exists.
- **A hand edit is never discarded.** Editing a reply retires its retained draft,
  so a rewrite after an edit starts from what you wrote, not from the text you
  replaced.
- The message tree is untouched: no sibling, no branch, no new row. The reply is
  edited in place.
- Any unreviewed World proposal that was inferred from that reply goes **stale**,
  the same as if you had edited it by hand — the evidence it was drawn from just
  changed.

Paragraphs settle top to bottom while it runs, and the bubble wears an accent
rail until it finishes. The ordinary **Stop** button cancels it. Nothing is saved
until the rewrite completes: stopping it, closing the tab, or a failure all leave
the stored message exactly as it was.

Replies written before this feature shipped have no retained draft — the old
source was never stored — so they rewrite their own text instead. That is a
slightly different job than a redo from source, and it is the only honest one
available for them; refusing outright left the button dead on exactly the
conversations most people have the most of.

## Setup

Settings → **Local ML** → *Prose Rewriter*.

### 1. The llama.cpp runtime

The rewriter does not use `llama-cpp-python`; it drives a `llama-server` child
process, which is what buys continuous batching across paragraphs. Press
**Download** in the runtime box (100 MB) and Orb fetches a prebuilt binary from
the official [`ggml-org/llama.cpp`](https://github.com/ggml-org/llama.cpp) releases
into `backend/data/llama-bin/`. The box is only there while the binary is missing —
once it is installed the card stops mentioning it and the models appear.

**The models are gated on it.** A checkpoint on disk without the binary rewrites
nothing, so until the runtime is installed the card offers the runtime and
nothing else. The one exception is a variant you already downloaded: its row
stays — without a radio, since there is nothing to select it for — so its **×**
remains reachable and 5 GB is never stranded by a missing binary.

!!! note "This downloads and then runs a native binary"
    It is the only place Orb does that. The archive comes from the official GitHub
    release feed over HTTPS, behind an explicit button — but GitHub publishes no
    per-asset checksum, so there is nothing to pin the bytes against. The trust
    posture is the publisher and the transport, the same as a GGUF's.

    If you would rather supply your own, set `ORB_LLAMA_SERVER` to its path and the
    button never appears. A build that is set but not executable is a hard error,
    never a silent fallback to a different binary.

Orb pins a known-good llama.cpp build rather than always taking the newest, because
four of its behaviours are load-bearing here and "whatever shipped this morning" is
a silent-breakage channel. Override with `ORB_LLAMA_CPP_BUILD=latest` or an explicit
`bNNNNN` tag.

The **Run on GPU** checkbox is a separate axis: GPU acceleration comes from the
Vulkan build being the one that was fetched, and the checkbox flips
`--n-gpu-layers` between all and none. That is why it sits *inside* the runtime
box while the binary is missing — it picks which archive the button fetches,
which has to be decided before the download, not after. Once the runtime is in,
the same checkbox moves to the foot of the card as the model's placement switch.

**Parallel batch** controls how many paragraphs llama.cpp decodes at once. One
uses the least memory; four is fastest and remains the default. Changing it
finishes any rewrite already in progress, then reloads and pre-warms the model
with the new allocation in the background. A new rewrite waits for that reload
rather than using the old batch size.

### 2. A model

| Variant | Size | Notes |
|---|---|---|
| `1.7B · Q8_0` | 2.2 GB | Fastest, good enough. |
| `4B · Q4_K_M` | 2.7 GB | Medium quality. |
| `4B · Q8_0` | 4.7 GB | Best quality, invents the least. |

Download one and switch the feature on. The download **selects** what it fetched
when nothing usable was selected before, so the obvious path arms the feature;
the radio is there to change your mind between two you have on disk, not a step
you can forget. A live selection is never overridden — a second download leaves
your pick alone — and deleting the selected checkpoint hands the selection to
another one that is present, or clears it when that was the last. Switching the
feature on repairs a selection that points at nothing, which is the self-heal
for an install that downloaded before any of that was true.

Each downloaded variant has a **×** next to it — 9.6 GB for all three is too much
to leave with no exit but the file manager.

Selecting a variant, flipping the GPU box, or changing the parallel batch
**pre-warms** the model in the background, so it is hot by the time you leave
Settings rather than costing you a visible stall on the first turn.

## VRAM

The KV cache is allocated in full when the model loads. Each parallel paragraph
gets a 1280-token slot: roughly 0.14 GB on the 1.7B or 0.19 GB on the 4B,
**on top of** the model. The default four-slot batch is therefore roughly 0.6 GB
or 0.8 GB respectively.

| Variant | Model | + KV cache (batch 4) | Total |
|---|---|---|---|
| 1.7B Q8_0 | 2.2 GB | ~0.6 GB | ~2.8 GB |
| 4B Q4_K_M | 2.7 GB | ~0.8 GB | ~3.5 GB |
| 4B Q8_0 | 4.7 GB | ~0.8 GB | ~5.5 GB |

The child process **unloads itself after five minutes idle** and frees that memory;
the next turn reloads it. Set `ORB_PROSE_REWRITER_IDLE` (seconds) to change that.
If your writer model is also local and on the same card, that idle unload is what
keeps the two from colliding.

## When it doesn't run

Every failure mode is the same one: **you keep the writer's prose, the turn
completes, and a warning toast tells you why.** No binary, no model file, a boot
timeout, a crashed child, a GGUF renamed out from under it — all of them are
non-events for the turn. A local nicety must never cost you a generation.

## Cost

This is a real per-turn cost, not a background task. Every turn pays a local
generation (per speaker, in group chats), and a cold start additionally pays the
model load. If that is not a trade you want on every message, leave it off and turn
it on for the scenes that matter.

## Credits

The models and the prompt/repair logic come from
[ProseRewriterWebUI](https://github.com/OrbFrontend/ProseRewriterWebUI); the weights
live on the Hub as `chartreuse-verte/prose-rewriter-{1.7b,4b}-v1.2` and are
downloaded at runtime, never bundled.
