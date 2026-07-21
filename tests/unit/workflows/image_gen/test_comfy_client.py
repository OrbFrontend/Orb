from __future__ import annotations

import httpx
import pytest

from backend.workflows.image_gen.engine.comfy_client import ComfyClient
from backend.workflows.image_gen.engine.contracts import ImageGenerationError


@pytest.mark.asyncio
async def test_queue_history_view_contract_returns_valid_image():
    png = b"\x89PNG\r\n\x1a\n" + b"payload"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p1", "number": 3})
        if request.url.path == "/history/p1":
            return httpx.Response(
                200,
                json={
                    "p1": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "x.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(404)

    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))
    result = await client.generate({"9": {}}, "9", timeout_seconds=2)
    assert result.image_bytes == png
    assert result.mime == "image/png"
    assert result.backend_info["prompt_id"] == "p1"


@pytest.mark.asyncio
async def test_validation_error_is_sanitized_and_names_checkpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "prompt_outputs_failed_validation",
                    "traceback": "/secret/ComfyUI/execution.py",
                },
                "node_errors": {
                    "4": {
                        "errors": [
                            {
                                "type": "value_not_in_list",
                                "details": "ckpt_name",
                                "extra_info": {
                                    "input_name": "ckpt_name",
                                    "exception_message": "/secret/model",
                                },
                            }
                        ]
                    }
                },
            },
        )

    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))
    with pytest.raises(ImageGenerationError) as raised:
        await client.generate({}, "9", timeout_seconds=2)
    assert "checkpoint" in str(raised.value)
    assert "/secret" not in str(raised.value)


def _completed_history(prompt_id: str = "p1") -> dict:
    return {
        prompt_id: {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}},
        }
    }


def _server(queue_bodies, *, number: int = 5, png: bytes = b"\x89PNG\r\n\x1a\n" + b"payload"):
    """Fake ComfyUI that hands out `queue_bodies` one per /queue poll.

    /history only reports completion once the queue script is exhausted, so a
    test controls exactly how many queue transitions the render observes.
    """
    remaining = list(queue_bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p1", "number": number})
        if path == "/queue":
            body = remaining.pop(0) if remaining else {"queue_running": [], "queue_pending": []}
            return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)
        if path == "/history/p1":
            return httpx.Response(200, json=_completed_history() if not remaining else {})
        if path == "/view":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(404)

    return handler


def _recorder():
    seen: list[tuple[str, object]] = []

    def progress(stage, detail):
        seen.append((stage, detail.get("ahead")))

    return seen, progress


@pytest.mark.asyncio
async def test_queue_position_counts_only_entries_ahead():
    queue = {
        "queue_running": [[2, "other-a"]],
        "queue_pending": [[3, "other-b"], [5, "p1"], [9, "later"]],
    }
    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(_server([queue])))
    # 2 and 3 are ahead; this job's own entry and the one behind it are not.
    assert await client.queue_ahead(5) == 2


@pytest.mark.asyncio
async def test_progress_reports_position_then_rendering():
    busy = {"queue_running": [[2, "other-a"]], "queue_pending": [[3, "other-b"]]}
    seen, progress = _recorder()
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(_server([busy, busy, {"queue_running": [[5, "p1"]], "queue_pending": []}])),
    )
    result = await client.generate({"9": {}}, "9", timeout_seconds=10, progress=progress)
    assert result.mime == "image/png"
    # Queued behind two, no repeat while the position is unchanged, then rendering.
    assert seen == [("queued", 2), ("rendering", 0)]


@pytest.mark.asyncio
async def test_progress_reports_rendering_when_queue_is_empty():
    seen, progress = _recorder()
    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(_server([])))
    await client.generate({"9": {}}, "9", timeout_seconds=10, progress=progress)
    assert seen == [("rendering", 0)]


@pytest.mark.asyncio
async def test_unavailable_queue_endpoint_does_not_fail_the_render():
    seen, progress = _recorder()
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(_server([httpx.Response(404)])),
    )
    result = await client.generate({"9": {}}, "9", timeout_seconds=10, progress=progress)
    # Position is unknown, never guessed, and the render proceeds regardless.
    assert result.mime == "image/png"
    assert seen == [("rendering", None)]


@pytest.mark.asyncio
async def test_malformed_queue_entries_are_ignored():
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(
            _server([{"queue_running": "nope", "queue_pending": [[], ["x"], [True], [1, "real"]]}])
        ),
    )
    assert await client.queue_ahead(5) == 1
    assert await client.queue_ahead("5") is None
