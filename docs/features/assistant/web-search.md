# Web Search

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

!!! warning "Pages can carry instructions"
    The assistant is told that page text is not from you and never to follow
    instructions in it, but nothing enforces that. After a request that read the
    web, check what it changed at once, and review its proposed edits as usual.
