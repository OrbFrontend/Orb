"""Detect heads with a single-class YOLOv8 graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import onnx_runtime
from . import catalog

if TYPE_CHECKING:
    from PIL import Image

_INPUT = 640
_PAD = (114, 114, 114)
_CONFIDENCE = 0.3
_OVERLAP = 0.5


@dataclass(frozen=True)
class Head:
    """A head box in the image's own pixels."""

    left: float
    top: float
    right: float
    bottom: float
    score: float

    @property
    def side(self) -> float:
        return max(self.right - self.left, self.bottom - self.top)


def _iou(a: Head, b: Head) -> float:
    width = max(0.0, min(a.right, b.right) - max(a.left, b.left))
    height = max(0.0, min(a.bottom, b.bottom) - max(a.top, b.top))
    shared = width * height
    total = (a.right - a.left) * (a.bottom - a.top) + (b.right - b.left) * (b.bottom - b.top) - shared
    return shared / total if total > 0 else 0.0


@onnx_runtime.using
def heads(image: Image.Image) -> list[Head]:
    """The heads in an RGB *image*, most confident first."""
    import numpy as np  # noqa: PLC0415 -- deferred; numpy arrives with onnxruntime
    from PIL import Image  # noqa: PLC0415 -- deferred with numpy

    width, height = image.size
    scale = _INPUT / max(width, height)
    fitted = (max(1, round(width * scale)), max(1, round(height * scale)))
    offset_x, offset_y = (_INPUT - fitted[0]) // 2, (_INPUT - fitted[1]) // 2
    canvas = Image.new("RGB", (_INPUT, _INPUT), _PAD)
    canvas.paste(image.resize(fitted, Image.Resampling.BILINEAR), (offset_x, offset_y))
    tensor = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0

    session = onnx_runtime.load(catalog.model_path())
    rows = np.asarray(session.run(None, {session.get_inputs()[0].name: tensor})[0])[0].T
    scores = rows[:, 4:].max(axis=1)
    found: list[Head] = []
    for index in np.argsort(-scores):
        score = float(scores[index])
        if score < _CONFIDENCE:
            break
        cx, cy, w, h = (float(v) for v in rows[index, :4])
        head = Head(
            left=(cx - w / 2 - offset_x) / scale,
            top=(cy - h / 2 - offset_y) / scale,
            right=(cx + w / 2 - offset_x) / scale,
            bottom=(cy + h / 2 - offset_y) / scale,
            score=score,
        )
        if all(_iou(head, other) < _OVERLAP for other in found):
            found.append(head)
    return found
