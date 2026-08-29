# Prose Rewriter

A local language model that rewrites the Writer's paragraphs before the Editor
audits them. It changes the prose's texture—cadence, stock phrasing, and other
signs of machine-written text—that the Editor's smaller patches cannot address.

It runs automatically during a turn or [on demand](#on-demand-after-the-fact)
from the toolbar under a saved reply.

It is off by default. You opt-in by downloading the runtime and the models.

## What it does

Each variant takes one paragraph of English fiction and returns a more natural
version at roughly the same length. The variants were trained on paired human and
LLM fiction, one paragraph at a time, including dialogue.

Keep these limits in mind:

- It is for English fiction only. Technical writing, other languages, and lists
  may come back worse.
- It does not defeat AI detectors. It is meant to humanize the prose.
- Paragraphs shorter than 80 bytes or longer than 512 tokens pass through
  unchanged.

## During a turn

```
Director → Writer → [ Prose Rewriter → Editor audit → patches ] → workflows
```

The rewriter runs first inside the Editor pass. The audit and patcher then work
on the rewritten draft—the text Orb will save. Rewriting later would invalidate
the patcher's byte offsets.

- The Editor diff includes the rewriter's paragraph-level changes.
- The length guard uses the rewritten draft. `match` mode is designed to keep
  the length about the same.
- Group chats run the rewriter once per speaker. The model stays loaded between
  speakers.
- The rewriter does not depend on the Agent toggle. If the Director and Editor
  LLM passes are off, it makes one local generation and no remote call.
- Writing-mode documents are unaffected. They use their own auditor.

## On demand, after the fact

The toolbar under an assistant reply shows the rewrite button when the reply has
text, the feature is on, and a variant is selected.

Orb keeps the Writer draft for new replies: the text before the in-turn rewriter
and Editor, with inline macros already frozen. When that draft exists, the button
rewrites it. Otherwise, it rewrites the saved reply. This makes the action a
rewrite from source, rather than a second pass on the current text. The tooltip
says which source it will use: *Rewrite original Writer draft* or *Rewrite this
message*.

- When it rewrites from the Writer draft, it discards that reply's Editor
  patches. Those patches describe the old text.
- A hand edit retires the Writer draft. Later rewrites use the edited reply.
- The message tree does not change. The reply is edited in place.
- Any unreviewed World proposal inferred from the reply becomes stale, because
  its source text changed.

Paragraphs update from top to bottom, and an accent rail marks the reply while it
runs. **Stop** cancels the rewrite. Orb saves nothing until it finishes, so
stopping it, closing the tab, or hitting an error leaves the saved reply alone.

Replies created before this feature existed have no Writer draft. They rewrite
their saved text instead.

## Setup

Settings → **Local ML** → *Prose Rewriter*.

### 1. Install the runtime

The rewriter uses a `llama-server` child process, not `llama-cpp-python`. This
allows it to batch paragraphs. Press **Download** in the runtime box to fetch the
100 MB prebuilt binary from the official
[`ggml-org/llama.cpp`](https://github.com/ggml-org/llama.cpp) releases. Orb stores
it in `backend/data/llama-bin/`.

!!! note "This downloads and runs a native binary"
    This is the only place Orb does that. The binary comes from the official
    GitHub release feed over HTTPS, behind an explicit button. GitHub does not
    publish a checksum for each asset, so the trust boundary is the publisher
    and the connection.

    To provide your own binary, set `ORB_LLAMA_SERVER` to its path. The download
    button will stay hidden. A configured path that is not executable is an
    error; Orb will not silently choose another binary.

Orb pins a known-good llama.cpp build because its behavior matters to this
feature. Use `ORB_LLAMA_CPP_BUILD=latest` or an explicit `bNNNNN` tag to override
the pinned build.

**Run on GPU** chooses the Vulkan build when the runtime is downloaded. After
installation, it switches the model between all and no GPU layers. Before
installation, it must stay in the runtime box because it determines which binary
Orb downloads.

**Parallel batch** sets how many paragraphs llama.cpp decodes at once. One uses
the least memory; four is the default, and machines with more memory can select
up to eight for greater throughput. If you run on CPU, my recommendation is
either the 1.7B Q8 or the 4B Q4_KM at batch 4.

### 2. Download a variant

| Variant | Size | Notes |
|---|---|---|
| `1.7B · Q8_0` | 2.2 GB | Fastest; good enough for most replies. |
| `4B · Q4_K_M` | 2.7 GB | Medium quality. |
| `4B · Q8_0` | 4.7 GB | Best quality; least likely to invent text. |

Download a variant, then select it. This is a two-step effort.

Selecting a variant, changing **Run on GPU**, or changing **Parallel batch**
pre-warms the model in the background, so it is ready when you leave Settings.

## VRAM

The model allocates its KV cache when it loads. Each parallel paragraph gets a
1280-token slot: about 0.14 GB on the 1.7B variant or 0.19 GB on either 4B
variant, **in addition to** the model.

| Variant | Model | + KV cache (batch 4) | Total |
|---|---|---|---|
| 1.7B Q8_0 | 2.2 GB | ~0.6 GB | ~2.8 GB |
| 4B Q4_K_M | 2.7 GB | ~0.8 GB | ~3.5 GB |
| 4B Q8_0 | 4.7 GB | ~0.8 GB | ~5.5 GB |

The child process unloads after five minutes without work and frees that memory.
The next turn loads it again. Set `ORB_PROSE_REWRITER_IDLE` in seconds to change
the timeout. If the Writer model is also local, this timeout helps keep both
models from competing for VRAM.

## When it does not run

Failures keep the Writer's prose, complete the turn, and show a warning with the
reason. A missing runtime or variant, a startup timeout, a crashed child process,
or a renamed model file is therefore harmless to the turn.

## Credits

The models and prompt/repair logic come from
[ProseRewriterWebUI](https://github.com/OrbFrontend/ProseRewriterWebUI). The
model files are downloaded at runtime from the Hub:
`chartreuse-verte/prose-rewriter-{1.7b,4b}-v{.*}`.
