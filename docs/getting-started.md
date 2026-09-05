# Getting Started

## Requirements

- Python 3.11 or newer
- An OpenAI-compatible LLM endpoint with prompt-caching support
- A model that supports tool or function calling (Gemma 4 is a recommended local option)

## Install Orb

1. Clone the repository:

   ```bash
   git clone https://github.com/OrbFrontend/Orb.git
   cd Orb
   ```

2. Check that Python is available:

   ```bash
   python3 --version
   ```

3. Start Orb:

   - Linux and macOS: `./run_unix.sh`
   - Windows: `run_windows.bat`

The launcher creates `.venv`, installs `requirements.txt`, and starts the server.
You do not need to activate the environment for normal use. Activate it only
when you run one of the project scripts yourself.

## First run

1. Open the **Endpoints** panel and configure the Writer and Agent endpoints.
   The same model can fill both roles. Two models can improve results, but use
   more tokens.
2. Create or import a character in **Characters**.
3. Open the character, send a message, and continue the conversation.

Endpoints use a hierarchy: an endpoint can contain several models, and each model
has its own parameters and prompts.

## Import from SillyTavern

The migration script copies supported data from an existing SillyTavern install.
Stop Orb first, activate Orb's virtual environment, and run the script from the
repository root.

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

    In PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`.

`--dry-run` previews the migration. It does not change the database.

| SillyTavern data | Orb data |
|---|---|
| `characters/*.png` | Characters and their card avatars |
| Expression sprite folders | Character expressions |
| Embedded lorebook | A World linked to the character |
| `worlds/*.json` | Worlds, with their global enabled state |
| `chats/**/*.jsonl` | Conversations and their original dates |
| Swipes | Message branches, including the selected branch |
| Personas | Personas, descriptions and avatar images |
| Groups and group chats | Group scenes and speaker attribution |

Orb does not import prompts, context templates, instruct sequences, generation
presets, endpoints or API keys, themes, backgrounds, reasoning traces, token
counts, author's notes, or SillyTavern's tag list. Chats whose
character card was deleted are skipped unless you add `--include-orphans`.

Use `--help` to see options such as `--only`, `--db`, and `--limit`.
