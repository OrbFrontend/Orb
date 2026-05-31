"""Pins the workflow locks onto the toolkit's public re-export surface.

Workflow authors import everything from ``backend.workflows.toolkit``,
so the three workflow locks must be reachable there to guard a read-modify-write
on any state tier without importing ``backend.locks`` directly. These assertions
fail if a lock is dropped from the re-export, omitted from ``__all__``, or rebound
to something other than the canonical ``backend.locks`` object.
"""

from __future__ import annotations

from backend import locks
from backend.workflows import toolkit

_LOCK_NAMES = (
    "workflow_state_lock",
    "workflow_character_state_lock",
    "workflow_config_lock",
)


def test_locks_exported_from_toolkit():
    for name in _LOCK_NAMES:
        assert hasattr(toolkit, name), f"{name} not importable from toolkit"
        assert name in toolkit.__all__, f"{name} missing from toolkit.__all__"


def test_toolkit_locks_are_canonical():
    for name in _LOCK_NAMES:
        assert getattr(toolkit, name) is getattr(locks, name), f"{name} is not the backend.locks object"
