"""Shared local-model artifacts, dependencies, and runtimes."""

from __future__ import annotations

from . import assets, catalog, dependencies
from .assets import (
    delete_model,
    download,
    model_dir,
    present,
    prune_stale,
    resolve_path,
    variant_path,
    variant_present,
    variant_spec,
)
from .catalog import MODELS, ModelSpec, ModelVariantSpec, RuntimeKind
from .dependencies import deps_ok, import_llama, install_cmd


def available(feature: str = "autocomplete") -> tuple[bool, str]:
    """Feature readiness: extras installed AND this feature's model present.

    The one function that spans both halves, which is why it lives on the
    facade rather than in either module.

    Reached through the owning modules rather than the names re-exported above,
    so a test that patches ``dependencies.deps_ok`` or ``assets.present`` — the
    modules that define them — actually changes what this answers.
    """
    ok, reason = dependencies.deps_ok(feature)
    if not ok:
        return False, reason
    if not assets.present(feature):
        return False, f"model file not found: {assets.resolve_path(feature)}"
    return True, ""


__all__ = [
    "MODELS",
    "ModelSpec",
    "ModelVariantSpec",
    "RuntimeKind",
    "assets",
    "available",
    "catalog",
    "delete_model",
    "dependencies",
    "deps_ok",
    "download",
    "import_llama",
    "install_cmd",
    "model_dir",
    "present",
    "prune_stale",
    "resolve_path",
    "variant_path",
    "variant_present",
    "variant_spec",
]
