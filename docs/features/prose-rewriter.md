# Prose Rewriter

Prose Rewriter is an optional local language model that revises each Writer
paragraph before the Editor checks the reply. It focuses on cadence, stock
phrasing, and other signs of machine-written prose.

It is designed for English fiction. Technical text, lists, and other languages
may get worse. Paragraphs shorter than 80 bytes or longer than 512 tokens are left
unchanged. It does not guarantee that text will pass an AI detector.

## During a turn

The order is:

```text
Director → Writer → Prose Rewriter → Editor → other workflows
```

The Editor checks the rewritten text and its diff includes the paragraph changes.
The Length Guard also measures the rewritten text. In group chats, Orb runs the
rewriter separately for each speaker.

Prose Rewriter does not require the Agent toggle. It runs locally when enabled.
Document mode has a separate Output Auditor and does not use this feature.

## Rewrite an existing reply

The rewrite button appears under a saved assistant reply when the feature is on.
Orb uses the original Writer draft when it is available; otherwise it uses the
saved reply.

- A rewrite from the Writer draft discards Editor patches from that draft.
- Editing a reply removes its stored Writer draft, so later rewrites use the edit.
- The reply stays in the same branch and updates in place.
- A pending Dynamic World proposal based on the reply becomes stale.

Orb saves the result only after the rewrite finishes. **Stop**, an error, or a
closed tab leaves the saved reply unchanged.

## Install it

Open **Settings → Local ML → Prose Rewriter**.

### Runtime

Select **Download** to install Orb's pinned `llama-server` build from the official
[llama.cpp releases](https://github.com/ggml-org/llama.cpp). Orb stores it in
`backend/data/llama-bin/`.

You can provide an existing executable with the `ORB_LLAMA_SERVER` environment
variable. A configured path must be executable. Set `ORB_LLAMA_CPP_BUILD=latest`
or a `bNNNNN` tag to override Orb's pinned build.

**Run on GPU** selects the Vulkan build during download and controls GPU layers
after installation. **Parallel batch** controls how many paragraphs are decoded
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
`ORB_PROSE_REWRITER_IDLE` in seconds to change that timeout.

If the local model fails to start or stops, Orb keeps the Writer's reply and
shows a warning.

The models and rewrite logic come from
[ProseRewriterWebUI](https://github.com/OrbFrontend/ProseRewriterWebUI). Model
files are downloaded at runtime from the Hub.
