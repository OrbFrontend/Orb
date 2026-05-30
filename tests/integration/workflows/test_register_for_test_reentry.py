"""Pins the ``register_for_test`` re-entry contract.

``register_workflow`` stores the same ``Workflow`` instance the test holds,
so its ``subscriptions`` list is identity-shared with the registry record.
A second ``with register_for_test(wf):`` on the same instance must not
trip ``subscribe()``'s duplicate-(workflow_id, hook_type) guard.
"""

from __future__ import annotations

from ._fixtures import make_workflow, register_for_test


async def _on_demand(_ctx, _body):
    return {}


def test_register_for_test_supports_reentry_on_same_workflow_object():
    wf = make_workflow("reentry_wf", on_demand=_on_demand)
    with register_for_test(wf):
        pass
    with register_for_test(wf):
        pass
