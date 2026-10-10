"""Byte-preserving localhost inference recorder for native benchmark runs.

Run from the repository root:
    python -m scripts.bench.recorder --output /path/to/block/requests

The optional binding is an atomically replaced JSON file written by the driver
before each turn. It labels requests without modifying upstream traffic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
import uvicorn

HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
SECRET_HEADERS = {b"authorization", b"proxy-authorization", b"cookie", b"set-cookie", b"x-api-key"}


def forwarded_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Remove HTTP connection framing; preserve end-to-end header values."""
    connection_names = {
        part.strip().lower() for name, value in headers if name.lower() == b"connection" for part in value.split(b",")
    }
    return [(name, value) for name, value in headers if name.lower() not in HOP_HEADERS | connection_names]


def saved_headers(headers: list[tuple[bytes, bytes]]) -> list[list[str]]:
    return [
        [name.decode("latin1"), "[redacted]" if name.lower() in SECRET_HEADERS else value.decode("latin1")]
        for name, value in headers
    ]


def response_facts(raw: bytes) -> dict:
    """Extract usage/timings off the timed forwarding path. Absent stays absent."""
    candidates = []
    try:
        candidates.append(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        for line in raw.splitlines():
            if line.startswith(b"data:"):
                try:
                    candidates.append(json.loads(line[5:].strip()))
                except (ValueError, UnicodeDecodeError):
                    pass
    facts = {}
    for body in candidates:
        if isinstance(body, dict):
            for key in ("usage", "timings", "model", "id", "error"):
                if body.get(key) is not None:
                    facts[key] = body[key]
    return facts


class Recorder:
    def __init__(self, upstream: str, output: Path, binding: Path | None = None, *, transport=None):
        self.upstream = upstream.rstrip("/")
        self.output = output
        self.binding = binding
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10), trust_env=False, transport=transport)
        self.output.mkdir(parents=True, exist_ok=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await self.client.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        ingress = time.monotonic_ns()
        call_dir = self.output / f"{ingress}-{uuid.uuid4().hex[:8]}"
        call_dir.mkdir()
        metadata = {
            "ingress_ns": ingress,
            "utc_unix_ns": time.time_ns(),
            "method": scope["method"],
            "request_headers": saved_headers(scope["headers"]),
            "upstream_complete": False,
            "downstream_complete": False,
        }
        response = None
        started_response = False
        try:
            if self.binding is not None:
                metadata["binding"] = json.loads(self.binding.read_text())
            raw_path = scope.get("raw_path", scope["path"].encode())
            query = scope.get("query_string", b"")
            target = raw_path + (b"?" + query if query else b"")
            metadata["target"] = target.decode("ascii")
            body_parts = []
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    raise ConnectionError("client disconnected before request body completed")
                body_parts.append(event.get("body", b""))
                if not event.get("more_body", False):
                    break
            body = b"".join(body_parts)
            (call_dir / "request.bin").write_bytes(body)
            headers = [(name, value) for name, value in forwarded_headers(scope["headers"]) if name.lower() != b"host"]
            request = self.client.build_request(
                scope["method"], self.upstream + target.decode("ascii"), headers=headers, content=body
            )
            metadata["upstream_start_ns"] = time.monotonic_ns()
            response = await self.client.send(request, stream=True)
            metadata["response_headers_ns"] = time.monotonic_ns()
            metadata["status"] = response.status_code
            metadata["response_headers"] = saved_headers(response.headers.raw)
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": forwarded_headers(response.headers.raw),
                }
            )
            started_response = True
            chunks = []
            # No SSE parsing, decompression, rewriting, or whole-response buffering.
            # File writes preserve each delivered chunk; extraction happens afterwards.
            with (call_dir / "response.bin").open("wb") as raw:
                async for chunk in response.aiter_raw():
                    now = time.monotonic_ns()
                    if "first_data_ns" not in metadata:
                        metadata["first_data_ns"] = now
                    chunks.append({"arrival_ns": now, "bytes": len(chunk)})
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    raw.write(chunk)
            metadata["stream_end_ns"] = time.monotonic_ns()
            metadata["upstream_complete"] = True
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            metadata["downstream_complete"] = True
            (call_dir / "chunks.json").write_text(json.dumps(chunks, indent=2) + "\n")
        except (Exception, asyncio.CancelledError) as exc:
            metadata["error"] = f"{type(exc).__name__}: {exc}"
            metadata["failure_ns"] = time.monotonic_ns()
            if not started_response and not isinstance(exc, asyncio.CancelledError):
                await send({"type": "http.response.start", "status": 502, "headers": [(b"content-type", b"application/json")]})
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error":"benchmark recorder forwarding failed"}',
                        "more_body": False,
                    }
                )
            # A started response must remain truncated on failure: never forge an SSE terminator.
            if started_response or isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if response is not None:
                await response.aclose()
            raw_file = call_dir / "response.bin"
            if raw_file.exists():
                metadata.update(response_facts(raw_file.read_bytes()))
            (call_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="http://127.0.0.1:5000")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binding", type=Path)
    args = parser.parse_args()
    uvicorn.run(Recorder(args.upstream, args.output, args.binding), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
