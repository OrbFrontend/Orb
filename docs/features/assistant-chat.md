# Assistant Chat

Open **Assistant** beside a chat, document, or lorebook to ask questions about
your library or request changes in plain language. It uses the **Agent** model
and keeps its conversation separate from the story chat.

Text edits and deletions pause the assistant immediately for your review.
Choose **Apply & continue** (or **Delete & continue**) to commit the change and
resume, or **Skip & continue** to leave it out. The assistant receives your
decision immediately and continues the remaining work. **End request** skips
the proposal and ends the remaining work. You can also edit the proposed text
before applying, or type feedback to ask for a revised proposal.
Settings changes, newly created items, and other actions can take effect
immediately.

**Select text** in a chat message or a document before you ask, and the request is about that passage: "tighten
this", "what does she mean here?". The start of the selection shows under the input while it is attached.

In Document mode the assistant can create documents and propose edits to the one you have open. An applied edit lands
in the editor as one undo step.

Click a tool line in the log to see what the assistant called it with, such as the query it ran or the page it read.

In the panel's gear menu, **turn off tool groups you don't use** to reduce the
prompt size and save cache space. Keep that selection stable across requests
for better cache reuse: changing it makes the next request process the
conversation again. You can turn a group back on when you need it.

## Web search

The assistant can search the web and read pages, for requests like "look up
Misato from Evangelion and make a card". Searching goes through
[SearXNG](https://docs.searxng.org/), a free search server on your own machine.
Reading a page whose address you paste works without it.

With [Docker](https://www.docker.com/products/docker-desktop/) installed, run one
line on the machine Orb runs on:

=== "macOS / Linux"

    ```bash
    curl -fsSL https://raw.githubusercontent.com/OrbFrontend/Orb/main/scripts/run_searxng.sh | bash
    ```

=== "Windows"

    ```powershell
    powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/OrbFrontend/Orb/main/scripts/run_searxng.ps1 | iex"
    ```

The first run sets SearXNG up on `http://127.0.0.1:8888` and points Orb at it;
reload Orb afterwards. If Orb was not running, set **SearXNG URL** in the gear
menu yourself. SearXNG then starts with Docker. Run the same line again to start
it if it is stopped, or with `ORB_SEARXNG_UPDATE=1` to update it. From the Orb
folder, `./scripts/run_searxng.sh` (Windows: `scripts\run_searxng.ps1`) does the
same.

| Variable | Default | Use |
|---|---|---|
| `ORB_SEARXNG_UPDATE` | unset | `1` pulls the latest SearXNG and recreates it |
| `ORB_SEARXNG_PORT` | `8888` | Another port, when it is first set up |
| `ORB_URL` | `http://127.0.0.1:8899` | Where Orb runs |

Set one before the command: `curl … | ORB_SEARXNG_UPDATE=1 bash`, or in
PowerShell `$env:ORB_SEARXNG_UPDATE = "1"; irm … | iex`.

To use a SearXNG you already run, add `json` to `search.formats` in its
`settings.yml` and set its address as the **SearXNG URL**. Public instances
almost never allow JSON.

To remove it, run `docker rm -f orb-searxng` and delete `~/.config/orb-searxng`
(Windows: `%LOCALAPPDATA%\orb-searxng`).

Some sites refuse automated readers or show nothing without JavaScript; the
assistant then reads another result. It never reads `localhost` or addresses on
your home network.
