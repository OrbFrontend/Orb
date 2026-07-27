"""Fixtures for the community-extension lifecycle suite.

Three process globals have to be restored between tests, because all three are
deliberately process-wide in production: the registry's published community
overlay, the extension catalog it labels, and the pending staging tokens. The
content store needs no reset -- it is derived from the monkeypatched database
path, so each test already gets its own.

The ``client`` fixture does not run the FastAPI lifespan (see the parent
conftest), so nothing reconciles extensions automatically. That is convenient
rather than a gap: a test that wants startup behavior calls ``reconcile()``
explicitly and gets to say *when* the restart happened.
"""

from __future__ import annotations

import pytest

from backend.features.extensions import runtime, staging
from backend.workflows import registry as reg
from tests.extension_packages import (  # noqa: F401 -- re-exported for tests
    full_package,
    metadata_package,
)


@pytest.fixture(autouse=True)
def _isolate_extension_runtime():
    published = reg._PUBLISHED
    state = runtime._STATE
    staging.clear()
    reg._PUBLISHED = reg._Published(generation=published.generation)
    runtime._STATE = runtime.RuntimeState(generation=published.generation, packages={})
    yield
    staging.clear()
    reg._PUBLISHED = published
    runtime._STATE = state


async def install(client, package: bytes, *, enabled: bool = True, permissions: list | None = None) -> dict:
    """Inspect then install one archive, approving every requested permission.

    The two-call shape is the product contract, so the helper keeps it rather
    than reaching past the routes -- a test that installed through the
    lifecycle module directly would not notice a route that stopped binding the
    token to the consent screen.
    """
    inspection = (await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", package)})).json()
    approved = permissions if permissions is not None else [entry["value"] for entry in inspection["permissions"]]
    response = await client.post(
        "/api/extensions/install",
        json={"token": inspection["token"], "permissions": approved, "enabled": enabled},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def catalog(client) -> dict:
    return (await client.get("/api/extensions")).json()


async def entry(client, extension_id: str) -> dict:
    body = await catalog(client)
    return next(item for item in body["extensions"] if item["id"] == extension_id)
