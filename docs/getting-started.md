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

The launcher creates a `.venv` virtualenv in the repo root on first run, installs
`requirements.txt` into it, and starts the server from it. You never have to activate it
yourself to run Orb — only to run the scripts below.

## First Run

1. Open the **Endpoints** sidepanel and configure your Writer and Agent LLM endpoints.
   - The same model can serve both roles (suitable for local hosting).
   - Two separate models give better results at higher token cost.
   - Endpoints use a tree structure: each endpoint may have many models, each model has its own params and custom prompts.

2. Create or import a character in the **Characters** tab.

3. Click the character and send your first message.

## Coming from SillyTavern

`scripts/migrate_sillytavern.py` copies an existing SillyTavern install into Orb. Stop Orb first,
then run it from the repo root with the project's `.venv` active — the one the launcher created:

=== "Linux/macOS"

    ```bash
    source .venv/bin/activate
    python scripts/migrate_sillytavern.py --st-dir /path/to/SillyTavern --dry-run
    python scripts/migrate_sillytavern.py --st-dir /path/to/SillyTavern
    ```

=== "Windows"

    ```bat
    .venv\Scripts\activate.bat
    python scripts\migrate_sillytavern.py --st-dir C:\path\to\SillyTavern --dry-run
    python scripts\migrate_sillytavern.py --st-dir C:\path\to\SillyTavern
    ```

    (In PowerShell, activate with `.venv\Scripts\Activate.ps1` instead.)

`--dry-run` is optional, it's a safety check before committing.

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
