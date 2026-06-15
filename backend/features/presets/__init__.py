"""Presets slice — DB-facing preset library + snapshot/restore maintenance.

Depends only downward on ``database/``. ``engine`` holds the logic; this facade
re-exports the public API. Consumers that reach private internals (the schema
model, ``_library_path``, ``_merge``, …) import ``features.presets.engine``
directly — ``database/bootstrap.py`` needs only ``schema_safety_problems`` and
uses this facade.
"""

from __future__ import annotations

from .engine import (
    ALL_DOMAINS,
    PresetError,
    apply_preset,
    assert_schema_safe,
    build_preset,
    create_snapshot,
    delete_library_entry,
    ingest_upload,
    list_library,
    prune_auto,
    read_meta,
    restore_full,
    restore_partial,
    schema_coverage_problems,
    schema_equivalence_problems,
    schema_safety_problems,
)

__all__ = [
    "ALL_DOMAINS",
    "PresetError",
    "apply_preset",
    "assert_schema_safe",
    "build_preset",
    "create_snapshot",
    "delete_library_entry",
    "ingest_upload",
    "list_library",
    "prune_auto",
    "read_meta",
    "restore_full",
    "restore_partial",
    "schema_coverage_problems",
    "schema_equivalence_problems",
    "schema_safety_problems",
]
