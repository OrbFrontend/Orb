"""HTTP errors keep their meaning across guards and preserve diagnostic logging."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from backend.api.deps import profile_draft_failures
from backend.api.errors import register_error_handlers
from backend.api.routes import workflows
from backend.inference import DecisionTransportError, EndpointConfigError
from backend.inference.errors import llm_call_error
from backend.workflows.errors import WorkflowInputError, WorkflowUnavailableError


def _provider_error():
    request = httpx.Request("POST", "https://provider.invalid")
    response = httpx.Response(401, request=request)
    return llm_call_error(
        response=response,
        body='{"error":{"message":"Key secret-key was rejected"}}',
        url=str(request.url),
        model="writer",
        api_key="secret-key",
    )


@pytest.mark.parametrize("guard", ["none", "profile", "workflow"])
@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (EndpointConfigError("Choose an Agent model"), 409, "Choose an Agent model"),
        (_provider_error(), 502, "Key [redacted] was rejected"),
        (DecisionTransportError("invalid JSON"), 502, "DecisionTransportError: invalid JSON"),
        (WorkflowInputError("Upload a readable file"), 400, "Upload a readable file"),
        (WorkflowUnavailableError("Download the model first"), 503, "Download the model first"),
        (HTTPException(404, detail={"message": "Missing attachment"}), 404, {"message": "Missing attachment"}),
    ],
)
async def test_expected_http_failures_keep_their_status_and_message(guard, error, status, message):
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/failure")
    async def fail():
        if guard == "profile":
            with profile_draft_failures("Profile draft"):
                raise error
        if guard == "workflow":
            with workflows._hook_failures("query hook", "test", defect="Hook failed"):
                raise error
        raise error

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://orb") as client:
        response = await client.get("/failure")
    assert response.status_code == status
    assert response.json() == {"detail": message}


async def test_unexpected_http_failure_returns_json_and_logs_a_traceback(caplog):
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/failure")
    async def fail():
        raise RuntimeError("diagnostic only")

    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://orb"
        ) as client:
            response = await client.get("/failure")
    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong inside Orb; see server logs."}
    assert "diagnostic only" not in response.text
    assert any(record.exc_info and record.exc_info[0] is RuntimeError for record in caplog.records)


async def test_an_unread_upstream_error_body_does_not_mask_the_status():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/failure")
    async def fail():
        request = httpx.Request("POST", "https://provider.invalid")
        response = httpx.Response(503, request=request, stream=httpx.ByteStream(b"unread"))
        response.raise_for_status()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://orb") as client:
        response = await client.get("/failure")
    assert response.status_code == 502
    assert "503" in response.json()["detail"]
