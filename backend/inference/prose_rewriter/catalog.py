"""The prose-rewriter GGUFs, as seen from inside the feature.

``local_ml.MODELS`` owns the download plumbing — repo id, pinned revision,
on-disk basename, ``prune_stale``'s claim list — and knows nothing about
llama-server. This module is the other half: the label the selector shows, the
size the KV cache is budgeted against, and which of the three is selected. Both
read the *same* ``Variant`` rows off the spec, so a fourth checkpoint is one
entry in ``local_ml.MODELS`` and reaches both at once.

THERE IS NO ``resolve_default()`` HERE, unlike the reference. Orb persists the
choice in ``settings.local_ml_config`` and passes it in; a feature that silently
booted "the largest one on disk" would disagree with the radio button the user
is looking at.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

FEATURE = "prose_rewriter"


@dataclass(frozen=True)
class Variant:
    """One downloadable checkpoint of the rewriter.

    ``path`` is the path *inside the HF repo* (upstream's ``GGUF/`` layout);
    ``local_name`` is the flat basename ``local_ml.download`` writes under
    ``data/models/``, and the name ``prune_stale`` must claim.
    """

    id: str
    label: str
    detail: str
    repo_id: str
    path: str
    revision: str  # pinned commit sha — a repo re-point can't swap the weights under us
    size_mb: int

    @property
    def local_name(self) -> str:
        return os.path.basename(self.path)


def variants() -> tuple[Variant, ...]:
    """The registered variants, read off the local-ML spec (single source of truth)."""
    # Deferred: local_ml imports Variant from this module, so a top-level
    # import here would close the cycle at load time.
    from ..local_ml import MODELS  # noqa: PLC0415

    return MODELS[FEATURE].variants


def resolve(variant_id: str | None) -> Variant | None:
    """The selected variant, or ``None`` when nothing usable is selected.

    ``None`` is a supported state, not an error: a fresh install has an empty
    ``data/models/`` and the feature simply does not run. A stored id that no
    longer names a registered variant reads the same way rather than raising —
    the selector is user data and a registry bump must not break a turn.
    """
    if not variant_id:
        return None
    return next((v for v in variants() if v.id == variant_id), None)


def variant_path(variant: Variant) -> str:
    """Absolute path of *variant*'s GGUF under ``data/models/`` (may not exist)."""
    from ..local_ml import model_dir  # noqa: PLC0415 — deferred, same cycle as above

    return os.path.join(model_dir(), variant.local_name)


def on_disk(variant: Variant) -> bool:
    return os.path.exists(variant_path(variant))
