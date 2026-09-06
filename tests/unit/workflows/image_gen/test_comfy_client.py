from __future__ import annotations

import httpx
import pytest

from backend.workflows.image_gen.engine.comfy_client import (
    ComfyClient,
    invalidate_object_info,
)
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
    queue = {"queue_running": [[2, "other-a"]], "queue_pending": [[3, "other-b"], [5, "p1"], [9, "later"]]}
    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(_server([queue])))
    # 2 and 3 are ahead; this job's own entry and the one behind it are not.
    assert await client.queue_ahead(5) == 2


_BUSY = {"queue_running": [[2, "other-a"]], "queue_pending": [[3, "other-b"]]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queue_bodies", "expected"),
    [
        # Queued behind two, no repeat while the position is unchanged, then rendering.
        ([_BUSY, _BUSY, {"queue_running": [[5, "p1"]], "queue_pending": []}], [("queued", 2), ("rendering", 0)]),
        ([], [("rendering", 0)]),
        # An unavailable /queue leaves the position unknown, never guessed, and the
        # render proceeds regardless.
        ([httpx.Response(404)], [("rendering", None)]),
    ],
    ids=["queued then rendering", "queue empty", "queue endpoint missing"],
)
async def test_progress_reports_the_position_it_can_actually_see(queue_bodies, expected):
    seen, progress = _recorder()
    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(_server(queue_bodies)))
    result = await client.generate({"9": {}}, "9", timeout_seconds=10, progress=progress)
    assert result.mime == "image/png"
    assert seen == expected


@pytest.mark.asyncio
async def test_reference_upload_posts_multipart_and_returns_the_widget_value():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/upload/image"
        seen["auth"] = request.headers.get("authorization") or ""
        seen["body"] = request.content.decode("latin-1")
        return httpx.Response(200, json={"name": "orb_0123456789abcdef.webp", "subfolder": "orb", "type": "input"})

    client = ComfyClient("http://comfy.test", "sekrit", transport=httpx.MockTransport(handler))
    value = await client.upload_image(b"RIFFxxxxWEBP", "image/webp", digest="0123456789abcdef" + "f" * 48)

    # `folder_paths.get_annotated_filepath` resolves a bare "<subfolder>/<name>"
    # under the input directory, which is what the LoadImage widget must carry.
    assert value == "orb/orb_0123456789abcdef.webp"
    assert seen["auth"] == "Bearer sekrit"
    body = seen["body"]
    assert 'name="image"; filename="orb_0123456789abcdef.webp"' in body
    assert 'name="subfolder"' in body and "orb" in body
    assert 'name="type"' in body and "input" in body
    # ComfyUI compares `overwrite` against the strings "true"/"1"; a bool would
    # silently mean "no" and leave a new file behind on every render.
    assert 'name="overwrite"' in body and "true" in body


@pytest.mark.asyncio
async def test_a_refused_reference_upload_funnels_through_the_one_error_type():
    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(lambda _: httpx.Response(413)))
    with pytest.raises(ImageGenerationError, match="reference image"):
        await client.upload_image(b"x", "image/png", digest="a" * 64)


@pytest.mark.asyncio
async def test_malformed_queue_entries_are_ignored():
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(_server([{"queue_running": "nope", "queue_pending": [[], ["x"], [True], [1, "real"]]}])),
    )
    assert await client.queue_ahead(5) == 1
    assert await client.queue_ahead("5") is None


@pytest.mark.asyncio
async def test_one_node_class_is_read_without_pulling_the_whole_catalogue():
    """`/object_info` is tens of megabytes and cached for a minute; a render that
    needs one widget's declared bounds must not pay for it on every miss."""
    paths: list[str] = []
    catalogue = {"Seed (rgthree)": {"input": {"required": {"seed": ["INT", {"max": 2**50}]}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())  # the on-the-wire form, not the decoded one
        return httpx.Response(200, json=catalogue if request.url.path == "/object_info" else {})

    invalidate_object_info()
    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))

    assert await client.node_info("Seed (rgthree)") == {}
    # Spaces and parentheses are ordinary in custom node class names, and the path
    # has to carry them encoded or the lookup 404s on the nodes that need it most.
    assert paths == ["/object_info/Seed%20%28rgthree%29"]

    # Warm catalogue: served from it, no second request of either shape.
    await client.object_info()
    assert await client.node_info("Seed (rgthree)") == catalogue["Seed (rgthree)"]
    assert paths == ["/object_info/Seed%20%28rgthree%29", "/object_info"]
    invalidate_object_info()
