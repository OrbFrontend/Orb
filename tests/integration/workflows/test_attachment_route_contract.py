from __future__ import annotations

import pytest

from ._fixtures import new_conversation

_ACTIONS = (
    ("activate", {"sibling_id": 1}),
    ("delete", {"scope": "group"}),
    ("regenerate", {}),
    ("reroll-gen", {}),
    ("rehydrate", {}),
)


@pytest.mark.parametrize(("action", "payload"), _ACTIONS)
async def test_attachment_action_unknown_conversation_returns_404(client, action, payload):
    resp = await client.post(
        f"/api/conversations/no-such/messages/1/workflow-attachments/1/{action}",
        json=payload,
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(("action", "payload"), _ACTIONS)
async def test_attachment_action_unknown_attachment_returns_404(client, action, payload):
    cid = await new_conversation(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/1/workflow-attachments/99999/{action}",
        json=payload,
    )
    assert resp.status_code == 404
