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
