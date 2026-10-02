"""Fence dataset replacement against writes already admitted by this process."""

from contextlib import asynccontextmanager

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..database.queries.dataset import get_dataset_epoch, regenerate_dataset_epoch
from .deps import active_work, require_resource_available

_maintenance = False
_mutations: dict[object, str] = {}


@asynccontextmanager
async def dataset_maintenance():
    global _maintenance
    if _maintenance:
        raise HTTPException(status_code=409, detail="Dataset maintenance is running")
    _maintenance = True
    try:
        busy = list(dict.fromkeys([*active_work(), *_mutations.values()]))
        if busy:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Running work must finish before restore",
                    "work": busy,
                },
            )
        yield
        await regenerate_dataset_epoch()
    finally:
        _maintenance = False


class DatasetAdmissionMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        headers = dict(scope.get("headers", []))
        mutating = method not in {"GET", "HEAD", "OPTIONS"}
        exempt = path.endswith("/stop") or path.endswith("/in-flight")
        library = path.startswith("/api/presets/") and (path.endswith("/export") or path.endswith("/import"))
        replacing = path.startswith("/api/presets/") and (path.endswith("/apply") or path.endswith("/restore"))
        key = object()
        # Reserve synchronously before the first DB await: replacement sees even
        # a request still loading its epoch, not only requests inside a route.
        blocked = mutating and not exempt and not library and _maintenance
        if mutating and not exempt and not library and not replacing and not blocked:
            _mutations[key] = f"{method} {path}"
        try:
            epoch = await get_dataset_epoch()
            reason = None
            if mutating and not exempt and not library:
                if blocked or _maintenance:
                    reason = {"message": "Dataset maintenance is running"}
                elif headers.get(b"x-orb-epoch", epoch.encode()).decode() != epoch:
                    reason = {"message": "The dataset changed; refresh required", "code": "refresh_required"}
                parts = path.split("/")
                if len(parts) > 3 and parts[2] in {"conversations", "documents"}:
                    try:
                        require_resource_available(("doc:" if parts[2] == "documents" else "") + parts[3])
                    except HTTPException as exc:
                        reason = {"message": str(exc.detail)}
            if reason:
                await JSONResponse({"detail": reason}, status_code=409, headers={"X-Orb-Epoch": epoch})(scope, receive, send)
                _mutations.pop(key, None)
                return
        except BaseException:
            _mutations.pop(key, None)
            raise

        async def epoch_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-orb-epoch", (await get_dataset_epoch()).encode()))
            await send(message)

        try:
            await self.app(scope, receive, epoch_send)
        finally:
            _mutations.pop(key, None)
