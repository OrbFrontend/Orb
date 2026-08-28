"""A supervised ``llama-server`` child: binary, process, transport, lifecycle.

Everything a feature needs to run continuously-batched generation against its
own GGUF, and nothing about any particular feature's prompts, sampling or
selection policy. A caller builds a :class:`LaunchProfile` from its own closed
allowlist, owns a :class:`ManagedLlamaServerHost`, and asks it for a client.

ORB'S ONLY MANAGED SUBPROCESS lives here. ``manager.shutdown_all()`` is
registered in the FastAPI lifespan; without it an orphaned child holds the GPU
after Orb exits.
"""

from __future__ import annotations

from . import binary, client, host, manager, process
from .binary import (
    LlamaServerMissing,
    bin_bytes,
    bin_dir,
    fetch,
    find_binary,
    runtime_ok,
    supports_flag,
)
from .client import BOOT_TIMEOUT, LaunchProfile, LlamaServerClient
from .host import ManagedLlamaServerHost
from .process import Child, spawn

__all__ = [
    "BOOT_TIMEOUT",
    "Child",
    "LaunchProfile",
    "LlamaServerClient",
    "LlamaServerMissing",
    "ManagedLlamaServerHost",
    "bin_bytes",
    "bin_dir",
    "binary",
    "client",
    "fetch",
    "find_binary",
    "host",
    "manager",
    "process",
    "runtime_ok",
    "spawn",
    "supports_flag",
]
