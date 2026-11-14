"""Shared local-model artifacts, dependencies, and runtimes."""

from __future__ import annotations

from . import assets, catalog, dependencies
from .assets import (
    delete_model,
    download,
    file_path,
    missing_files,
    model_dir,
    present,
    prune_stale,
    resolve_path,
    variant_path,
    variant_present,
    variant_spec,
)
from .catalog import MODELS, ModelFileSpec, ModelSpec, ModelVariantSpec, RuntimeKind
from .dependencies import deps_ok, install_cmd
from .llama_server import binary as llama_binary
from .zonos_server import binary as zonos_binary


def available(feature: str = "autocomplete") -> tuple[bool, str]:
    """Feature readiness: extras installed, this feature's model present, and its runtime binary installed.

    The one function that spans both halves, which is why it lives on the facade rather than in either module.

    Reached through the owning modules rather than the names re-exported above, so a test that patches ``dependencies.deps_ok``
    or ``assets.present`` -- the modules that define them -- actually changes what this answers.
    """
    ok, reason = dependencies.deps_ok(feature)
    if not ok:
        return False, reason
    if not assets.present(feature):
        return False, f"model file not found: {', '.join(assets.missing_files(feature)) or assets.resolve_path(feature)}"
    if catalog.MODELS[feature].runtime == "llama_server" and not llama_binary.runtime_ok():
        return False, "llama.cpp runtime not installed"
    if catalog.MODELS[feature].runtime == "zonos_server" and not zonos_binary.runtime_ok():
        return False, "ZONOS3 runtime not installed"
    return True, ""
