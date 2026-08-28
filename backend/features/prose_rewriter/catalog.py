"""The prose-rewriter GGUFs, as seen from inside the feature.

``local_models.catalog.MODELS`` owns the artifact facts — repo id, pinned
revision, on-disk basename, ``prune_stale``'s claim list — and knows nothing
about llama-server. This module is the other half: which of the three is
selected, and where the selected one is. Both read the *same*
``ModelVariantSpec`` rows off the spec, so a fourth checkpoint is one entry in
the shared manifest and reaches both at once.

THERE IS NO ``resolve_default()`` HERE, unlike the reference. Orb persists the
choice in ``settings.local_ml_config`` and passes it in; a feature that silently
booted "the largest one on disk" would disagree with the radio button the user
is looking at.
"""

from __future__ import annotations

from ...inference.local_models import assets
from ...inference.local_models.catalog import MODELS, ModelVariantSpec

FEATURE = "prose_rewriter"


def variants() -> tuple[ModelVariantSpec, ...]:
    """The registered variants, read off the shared manifest (single source of truth)."""
    return MODELS[FEATURE].variants


def resolve(variant_id: str | None) -> ModelVariantSpec | None:
    """The selected variant, or ``None`` when nothing usable is selected.

    ``None`` is a supported state, not an error: a fresh install has an empty
    ``data/models/`` and the feature simply does not run. A stored id that no
    longer names a registered variant reads the same way rather than raising —
    the selector is user data and a registry bump must not break a turn.
    """
    if not variant_id:
        return None
    return next((v for v in variants() if v.id == variant_id), None)


def variant_path(variant: ModelVariantSpec) -> str:
    """Absolute path of *variant*'s GGUF under ``data/models/`` (may not exist)."""
    return assets.variant_path(variant)


def on_disk(variant: ModelVariantSpec) -> bool:
    return assets.variant_present(variant)
