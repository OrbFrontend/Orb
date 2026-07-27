"""Client-disconnect cancellation for plain JSON extension actions."""

from __future__ import annotations

from typing import Any, cast

from backend.api.routes.extensions import _watch_action_disconnect
from backend.inference import AbortToken


class _DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


async def test_action_disconnect_aborts_the_shared_model_token():
    token = AbortToken()
    await _watch_action_disconnect(cast(Any, _DisconnectedRequest()), token, poll_seconds=0)
    assert token.is_aborted
