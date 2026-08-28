"""Shared local-model infrastructure: artifacts, dependencies, runtimes.

The half of Orb's local-ML support that is not about any one feature. It owns
what can be downloaded (:mod:`catalog`), where those files live and how they
are fetched, pruned and deleted (:mod:`assets`), what Python extras each
runtime needs (:mod:`dependencies`), and the supervised ``llama-server`` child
a continuously-batched feature runs on (:mod:`llama_server`).

WHAT IS NOT HERE: prompts, sampling, selection policy, launch tuning, and
anything that reads settings. Those belong to the feature above —
``features/prose_rewriter/`` is the worked example — and this package must
never import one. Nothing below ``features/`` may.

The in-process ``llama-cpp-python`` call paths (autocomplete, the classifiers)
stay in the sibling :mod:`backend.inference.local_ml`, which reads this
package's manifest and re-exports its asset surface for the workflow authors
and callers that address it by that name.
"""

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
