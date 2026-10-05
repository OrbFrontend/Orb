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
You do not need to activate the environment for normal use.

## Network access

Orb listens on port 8899 on every network interface, so a phone or another
computer on the same network can open `http://<this computer's IP>:8899`.

!!! warning "Set a password before sharing a network"
    Without a password, anyone who can reach port 8899 can use Orb: read and
    change your chats, characters, and settings, and read the API keys saved in
    **Endpoints**. See [Password](#password).

Set the `ORB_HOST` environment variable to choose the address Orb listens on:

| `ORB_HOST` | Who can connect |
|---|---|
| Not set, or `0.0.0.0` | This computer and every device on its networks |
| `127.0.0.1` | This computer only |
| One of this computer's addresses, such as `192.168.1.20` | Devices that reach that address |

=== "Linux/macOS"

    ```bash
    ORB_HOST=127.0.0.1 ./run_unix.sh
    ```

=== "Windows"

    ```bat
    set ORB_HOST=127.0.0.1
    run_windows.bat
    ```

    In PowerShell, set it with `$env:ORB_HOST = "127.0.0.1"`.

When `ORB_HOST` is a specific address, the launcher opens that address instead
of `localhost`. Orb listens on one address at a time; to allow some networks and
block others, keep the default and use a firewall rule.

### Password

Set a password in **Settings → Password**. Every page, file, and API request
then needs it, from this computer as well as from other devices. A browser
without a valid session gets only a plain sign-in page, whatever address it
asks for. That page and its headers do not name Orb, so a network scanner
cannot tell which app is answering.

- A browser stays signed in for up to 400 days.
- Setting a new password signs out every other browser. The browser you set
  it from stays signed in.
- Setting an empty password removes it.
- After five wrong passwords, a device must wait a minute before it can try
  again.
- Presets and backups never contain the password, and restoring one keeps
  your current password.

Orb serves plain HTTP, so the password crosses the network unencrypted. On a
network you do not trust, put Orb behind an HTTPS reverse proxy.

### Forgotten password

Orb stores the password, hashed, in its database at `backend/data/app.db`, in the
`access_password` table. Deleting that table's row removes the password:

1. Stop Orb.
2. From the Orb folder, run:

    === "Linux/macOS"

        ```bash
        .venv/bin/python -c "import sqlite3; c = sqlite3.connect('backend/data/app.db'); c.execute('DELETE FROM access_password'); c.commit()"
        ```

    === "Windows"

        ```bat
        .venv\Scripts\python -c "import sqlite3; c = sqlite3.connect('backend/data/app.db'); c.execute('DELETE FROM access_password'); c.commit()"
        ```

    With the `sqlite3` command-line tool installed, this does the same:
    `sqlite3 backend/data/app.db "DELETE FROM access_password;"`

3. Start Orb. It opens without a password; set a new one in **Settings**.

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
