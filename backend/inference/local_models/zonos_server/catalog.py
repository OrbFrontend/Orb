"""Locate ZONOS3 artifacts, report their readiness and describe the server launch."""

from __future__ import annotations

import os

from .. import assets, dependencies
from ..catalog import MODELS
from . import binary
from .host import Launch, ZonosServerHost

FEATURE = "zonos_tts"
FEATURE_REFERENCE = "zonos_reference"

#: Seconds without work before the server is stopped.
IDLE_TIMEOUT = float(os.environ.get("ORB_ZONOS3_IDLE", "300"))

HOST = ZonosServerHost(name="zonos_tts", idle_timeout=IDLE_TIMEOUT)


def _files(feature: str) -> list[str]:
    return [assets.resolve_path(feature), *(assets.file_path(f) for f in MODELS[feature].extra_files)]


def synthesis_ready() -> tuple[bool, str]:
    """Whether a line can be spoken: the voice model and the runtime are on disk."""
    ok, reason = dependencies.deps_ok(FEATURE)
    if not ok:
        return False, reason
    if assets.missing_files(FEATURE):
        return False, "The ZONOS3 voice model is not downloaded."
    try:
        binary.find_binary()
    except binary.ZonosServerMissing as exc:
        return False, str(exc)
    return True, ""


def enrollment_ready() -> tuple[bool, str]:
    """Whether a clip can be read into reference codes: synthesis plus the reference reader."""
    ok, reason = synthesis_ready()
    if not ok:
        return False, reason
    if assets.missing_files(FEATURE_REFERENCE):
        return False, "The ZONOS3 reference reader is not downloaded."
    return True, ""


def launch(*, gpu: bool = True) -> Launch:
    """The launch for the files on disk; the reference reader is passed once both of its files are there."""
    backbone, decoder, steering = _files(FEATURE)
    encoder, teacher = ("", "") if assets.missing_files(FEATURE_REFERENCE) else _files(FEATURE_REFERENCE)
    return Launch(backbone, decoder, steering, encoder, teacher, gpu and binary.gpu_capable())
