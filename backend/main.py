"""ASGI entrypoint."""

from __future__ import annotations

import logging

from .api import build_app

logging.basicConfig(level=logging.INFO)

app = build_app()


if __name__ == "__main__":
    import os
    import sys

    import uvicorn

    local_only = sys.argv[1:] == ["--local-only"]
    if local_only:
        os.environ["ORB_CLAUDE_CODE_LOCAL_ONLY"] = "1"
        os.environ["ORB_BIND_HOST"] = "127.0.0.1"
    elif len(sys.argv) > 1:
        raise SystemExit("Usage: python -m backend.main [--local-only]")
    else:
        os.environ.pop("ORB_CLAUDE_CODE_LOCAL_ONLY", None)
        os.environ.pop("ORB_BIND_HOST", None)
    uvicorn.run(app, host="127.0.0.1" if local_only else "0.0.0.0", port=8899)  # nosec B104
