"""Bounded, process-local pictures for the assistant log, separate from workflow attachments and avatars."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass

MAX_IMAGES = 32
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


@dataclass(frozen=True)
class AssistantImage:
    data: bytes
    mime: str

    @property
    def filename(self) -> str:
        return f"generated-image.{_EXTENSIONS[self.mime]}"


_images: OrderedDict[str, AssistantImage] = OrderedDict()


def keep_image(data: bytes, mime: str) -> str:
    """Keep one raster image, evicting oldest pictures to meet both count and byte limits."""
    if mime not in _EXTENSIONS or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("The generated picture is empty, too large, or in an unsupported image format.")
    held = sum(len(image.data) for image in _images.values())
    while _images and (len(_images) >= MAX_IMAGES or held + len(data) > MAX_TOTAL_BYTES):
        held -= len(_images.popitem(last=False)[1].data)
    _images[image_id := secrets.token_hex(16)] = AssistantImage(data, mime)
    return image_id


def get_image(image_id: str) -> AssistantImage | None:
    """An image still held by this server, or None after eviction or a restart."""
    return _images.get(image_id)
