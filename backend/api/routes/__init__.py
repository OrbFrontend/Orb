"""HTTP route modules, one ``APIRouter`` per domain.

``build_app()`` in :mod:`backend.api` includes the routers listed in
``ROUTERS`` (in order) onto the FastAPI app. Adding a domain = drop a module
here exposing ``router = APIRouter()`` and append it to ``ROUTERS``.
"""

from __future__ import annotations

from . import (
    characters,
    conversations,
    endpoints,
    fragments,
    local_ml,
    messages,
    misc,
    personas,
    phrase_bank,
    presets,
    settings,
    stats,
    workflows,
    worlds,
)

# Include order mirrors today's main.py route-definition order so that
# matching against the trailing StaticFiles catch-all is unaffected.
ROUTERS = [
    misc.router,
    settings.router,
    endpoints.router,
    fragments.router,
    worlds.router,
    phrase_bank.router,
    personas.router,
    stats.router,
    conversations.router,
    characters.router,
    presets.router,
    messages.router,
    workflows.router,
    local_ml.router,
]
