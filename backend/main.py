"""ASGI entrypoint."""

from __future__ import annotations

import logging
import os

from .api import build_app

logging.basicConfig(level=logging.INFO)

app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("ORB_HOST", "0.0.0.0"), port=8899)  # nosec B104 -- ORB_HOST narrows the LAN bind
