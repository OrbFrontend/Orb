# Getting Started

## Requirements

- Python 3.11+
- OpenAI-compatible LLM backend with prompt-caching support
- A model with strong tool/function calling (recommended: Gemma 4)

## Installation

1. Clone the repo:
   ```
   git clone https://github.com/OrbFrontend/Orb.git
   ```
2. Verify Python 3 is installed: `python3 --version`
3. Enter `Orb` folder and start the app:
   - Linux/Mac: `./run_unix.sh`
   - Windows: `run_windows.bat`

## First Run

1. Open the **Endpoints** sidepanel and configure your Writer and Agent LLM endpoints.
   - The same model can serve both roles (suitable for local hosting).
   - Two separate models give better results at higher token cost.
   - Endpoints use a tree structure: each endpoint may have many models, each model has its own params and custom prompts.

2. Create or import a character in the **Characters** tab.

3. Click the character and send your first message.

## Coming from SillyTavern

`scripts/migrate_sillytavern.py` copies an existing SillyTavern install into Orb. Stop Orb first,
then run it from the repo root with the project's virtualenv active:

```
python scripts/migrate_sillytavern.py --st-dir /path/to/SillyTavern --dry-run
python scripts/migrate_sillytavern.py --st-dir /path/to/SillyTavern
```

`--dry-run` does the entire migration and then throws it away, so you can read the report before
committing to anything. The real run copies your database first, and every id it writes is derived
from the source file — so running it twice imports nothing twice, and an interrupted run picks up
where it stopped.

What comes over:

| SillyTavern | In Orb |
|---|---|
| `characters/*.png` | Characters, with the card PNG as the avatar |
| A character's expression sprite folder | That character's expressions |
| A lorebook embedded in a card | A World, linked to that character |
| `worlds/*.json` | Worlds — enabled only if SillyTavern had them globally selected |
| `chats/**/*.jsonl` | Conversations, keeping their original dates |
| Swipes | Message branches, with the one you had selected still live |
| Personas | Personas (name and description) |
| `groups/` + `group chats/` | Group scenes, with their cast and per-speaker attribution |

What does not, because Orb has nowhere to put it: prompts, context templates, instruct sequences and
generation presets; endpoints and API keys; themes and backgrounds; persona avatar images; reasoning
traces and token counts; author's notes; SillyTavern's tag list; and the lorebook features on the
"deliberately not in Orb" list in [Lorebooks](features/lorebooks.md). Chats whose character card was
deleted are skipped unless you pass `--include-orphans`.

`--help` lists the rest of the flags: `--only` to migrate one kind of thing at a time, `--db` to
target a database other than the default, and `--limit` to try it on a handful of chats first.
