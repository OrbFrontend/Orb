"""The assistant's temporary raster store stays bounded and expires old ids."""

from collections import OrderedDict

import pytest

from backend.api.assistant import images


def test_image_store_bounds_count_bytes_and_formats(monkeypatch):
    monkeypatch.setattr(images, "_images", OrderedDict())
    monkeypatch.setattr(images, "MAX_IMAGES", 2)
    monkeypatch.setattr(images, "MAX_TOTAL_BYTES", 6)
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 4)
    first = images.keep_image(b"aa", "image/png")
    second = images.keep_image(b"bb", "image/jpeg")
    third = images.keep_image(b"cc", "image/webp")
    assert images.get_image(first) is None
    assert images.get_image(second) is not None
    fourth = images.keep_image(b"dddd", "image/png")
    assert images.get_image(second) is None
    assert images.get_image(third) is not None and images.get_image(fourth) is not None
    for data, mime in [(b"", "image/png"), (b"12345", "image/png"), (b"svg", "image/svg+xml")]:
        with pytest.raises(ValueError):
            images.keep_image(data, mime)
    assert len(images._images) == 2
