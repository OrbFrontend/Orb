# Length Guard

Length Guard limits the size of assistant replies. It checks word count after the
Writer finishes and asks the **Editor** to rewrite an overlong reply.

## Configure it

Open the tools panel and enable **Length Guard**. Set:

- **Max words**: the word limit for a reply
- **Max paragraphs**: the paragraph limit used by the rewrite
- **Enforce**: asks the Writer to stay within both limits before the Editor runs

The guard is off by default and requires the global **Agent** toggle. The default
limits are 240 words and 4 paragraphs.

When a reply exceeds **Max words**, the Editor rewrites the whole reply while
preserving its main story beats and voice. It does not truncate the text.
