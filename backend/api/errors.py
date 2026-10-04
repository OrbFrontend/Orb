"""Translate uncaught failures at the HTTP and SSE boundaries."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..inference import DecisionTransportError, EndpointConfigError
from ..inference.claude_code import ClaudeCodeError
from ..pipeline.events import FailureData, FailureEvent
from ..pipeline.failures import describe_failure
from ..workflows.errors import WorkflowInputError, WorkflowUnavailableError, WorkflowUserFacingError

logger = logging.getLogger(__name__)

# Local guards may add context for defects, but must let these failures reach the
# shared boundary instead of turning a missing setting or provider outage into 500.
API_PASSTHROUGH_ERRORS = (
    HTTPException,
    EndpointConfigError,
    httpx.HTTPError,
    ClaudeCodeError,
    DecisionTransportError,
    WorkflowUserFacingError,
)


def failure_event(exc: Exception) -> FailureEvent:
    """Describe and log one uncaught stream failure using the turn contract."""
    if isinstance(exc, HTTPException):
        detail = exc.detail
        sentence = detail if isinstance(detail, str) else detail.get("message", "") if isinstance(detail, dict) else ""
        data: FailureData = {
            "headline": "Orb refused the request.",
            "sentence": sentence,
            "kind": "request",
            "stage": "",
            "status": exc.status_code,
            "body": json.dumps(detail),
        }
    else:
        data = describe_failure(exc)
    if data["kind"] == "internal":
        logger.error("SSE generation failed", exc_info=exc)
    else:
        logger.warning("SSE generation failed: %s %s", data["headline"], data["sentence"])
    return {"event": "error", "data": data}


def http_failure(exc: Exception) -> HTTPException:
    """Translate a known failure for HTTP or a stream's HTTP-shaped verdict."""
    if isinstance(exc, HTTPException):
        return exc
    data = describe_failure(exc)
    status = (
        409
        if isinstance(exc, EndpointConfigError)
        else 400
        if isinstance(exc, WorkflowInputError)
        else 503
        if isinstance(exc, WorkflowUnavailableError)
        else 502
    )
    return HTTPException(status_code=status, detail=data["sentence"] or data["headline"])


def register_error_handlers(app: FastAPI) -> None:
    """Keep ordinary API failures in FastAPI's ``detail`` response shape."""

    async def expected_failure(request: Request, exc: Exception) -> JSONResponse:
        error = http_failure(exc)
        logger.warning("%s %s failed: %s", request.method, request.url.path, error.detail)
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    async def unexpected_failure(request: Request, exc: Exception) -> JSONResponse:
        logger.error("%s %s failed", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Something went wrong inside Orb; see server logs."})

    for error_type in API_PASSTHROUGH_ERRORS[1:]:
        app.add_exception_handler(error_type, expected_failure)
    app.add_exception_handler(Exception, unexpected_failure)
