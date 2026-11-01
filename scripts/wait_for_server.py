"""Wait quietly for the launcher's server to answer, then announce it and open the browser."""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser

SLOW_AFTER = 60.0
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def answers(url: str) -> bool:
    try:
        with _DIRECT.open(url, timeout=2):  # nosec B310 -- the launcher's own http URL
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def alive(pid: int | None) -> bool:
    if pid is None or os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def has_display() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    print("Starting Orb, please wait...", flush=True)
    start = time.monotonic()
    warned = False
    while not answers(args.url):
        if not alive(args.pid):
            return 0
        if not warned and time.monotonic() - start > SLOW_AFTER:
            print("Orb is still starting. If an error is shown above, Orb could not start.", flush=True)
            warned = True
        time.sleep(1)
    print(f"\nOrb is ready: {args.url}\n", flush=True)
    if has_display():
        webbrowser.open(args.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
