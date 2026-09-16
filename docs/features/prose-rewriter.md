# Prose Rewriter

Prose Rewriter is an optional secondary workflow backed by a local language
model. It revises each paragraph after the Editor finishes, focusing on cadence,
stock phrasing, and other signs of machine-written prose.

It is designed for English fiction. Technical text, lists, and other languages
may get worse. Paragraphs shorter than 80 bytes or longer than 512 tokens are left
unchanged. It does not guarantee that text will pass an AI detector.

## During a turn

The order is:

```text
Director → Writer → Editor → save draft → Prose Rewriter → other workflows
```

Orb retains the Editor's result before rewriting it. The Editor audit and Length
Guard therefore evaluate the Writer's prose, and the Prose Rewriter runs as the
first secondary text workflow. Later workflows receive its rewritten text. In
group chats, Orb runs the rewriter separately for each speaker. The retained
snapshot and final reply are committed together after the workflows finish.

Prose Rewriter does not require the Agent toggle. Its workflow toggle turns the
rewriter on; **Rewrite every reply automatically**, on by default, decides whether
generated turns run through it. Turning off the toggle or Secondary Workflows
turns the rewriter off entirely. Document mode has a separate Output Auditor and
does not use this workflow.

## Rewrite an existing reply

The rewrite button appears under a saved assistant reply when the rewriter is on
and a model is downloaded. It stays available with **Rewrite every reply
automatically** off, so you can rewrite only the replies you pick. Orb uses the
retained post-Editor draft when it is available; otherwise it uses the saved
reply.

- A rewrite from the retained draft preserves Editor patches while replacing the
  previous Prose Rewriter result and any later text-workflow changes.
- After the local rewrite, Format Consistency runs on the result just as it does
  during a generated turn. Its workflow enablement and voice setting still apply.
- Editing a reply removes its retained draft, so later rewrites use the edit.
- The reply stays in the same branch and updates in place.
- A pending Dynamic World proposal based on the reply becomes stale.

Orb saves the result only after the rewrite finishes. **Stop**, an error, or a
closed tab leaves the saved reply unchanged.

## Install it

Open **Workflow → Secondary**, turn on **Prose Rewriter**, and select **Download**
beside a model variant.

### Runtime

The first model download also installs Orb's pinned `llama-server` build (about
150 MB) from the official [llama.cpp releases](https://github.com/ggml-org/llama.cpp).
Orb stores it in `backend/data/llama-bin/`. The Spark-TTS voice model uses the
same runtime, so whichever feature you set up first fetches it.

You can provide an existing executable with the `ORB_LLAMA_SERVER` environment
variable. A configured path must be executable. Set `ORB_LLAMA_CPP_BUILD=latest`
or a `bNNNNN` tag to override Orb's pinned build.

**Run on GPU** switches between the GPU and CPU builds, which are installed
together. **Parallel batch** controls how many paragraphs are decoded
at once; larger values use more memory. The default batch is 4, with a maximum of
8.

### Model variants

| Variant | Download size | Typical use |
|---|---:|---|
| `1.7B · Q8_0` | 2.2 GB | Fastest |
| `4B · Q4_K_M` | 2.7 GB | Balanced |
| `4B · Q8_0` | 4.7 GB | Highest quality |

Select a variant after downloading it. Orb preloads the model when you change the
variant, GPU setting, or batch size.

### Memory

Approximate memory for batch 4:

| Variant | Total |
|---|---:|
| 1.7B Q8_0 | 2.8 GB |
| 4B Q4_K_M | 3.5 GB |
| 4B Q8_0 | 5.5 GB |

The local process unloads after five minutes without work. Set
`ORB_PROSE_REWRITER_IDLE` in seconds to change that timeout. Switching the
workflow off unloads it without waiting for that timeout and stops a load still
in progress; a rewrite already running finishes first.

If the local model fails to start or stops, Orb keeps the Editor's reply and
shows a warning.

The models and rewrite logic come from
[ProseRewriterWebUI](https://github.com/OrbFrontend/ProseRewriterWebUI). Model
files are downloaded at runtime from the Hub.
