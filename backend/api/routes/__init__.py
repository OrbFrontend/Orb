"""HTTP route modules and router registration."""

from __future__ import annotations

from . import (
    characters,
    conversations,
    decisions,
    documents,
    endpoints,
    fragment_state,
    fragments,
    library,
    local_ml,
    messages,
    misc,
    personas,
    phrase_bank,
    presets,
    settings,
    stats,
    storage,
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
    decisions.router,
    worlds.router,
    phrase_bank.router,
    personas.router,
    stats.router,
    storage.router,
    conversations.router,
    fragment_state.router,
    characters.router,
    # Library-wide maintenance tools. /api/library/* collides with no other
    # pattern; placed beside characters because it is the same modal's surface.
    library.router,
    presets.router,
    messages.router,
    workflows.router,
    local_ml.router,
    documents.router,
]
