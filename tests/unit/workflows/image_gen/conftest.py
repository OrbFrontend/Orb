"""Shared fixtures for the image_gen unit tests."""

from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine import render


@pytest.fixture(autouse=True)
def waits(monkeypatch) -> list[float]:
    """Every pause the ladder takes, recorded instead of taken.

    Autouse because the pacing is real -- seconds per rung, so that a burst-sensitive
    provider reads a retry as a correction rather than as traffic -- and a suite that
    actually served it would pay for it on every degradation test.

    The recorded list is the fixture rather than a bare stub, so a test can assert the
    ladder *decided* to wait, and how long, without waiting.
    """
    taken: list[float] = []

    async def record(seconds: float) -> None:
        taken.append(seconds)

    monkeypatch.setattr(render, "_pause", record)
    return taken
