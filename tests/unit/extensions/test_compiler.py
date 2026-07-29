"""The compiler: reference-graph validation and requirement derivation.

The property under test throughout is that the *code* decides what a package
needs, and the manifest's declarations are checked against it. A package that
under-declares is rejected; a package that reaches a capability behind an
unsatisfiable predicate still declares it.
"""

from __future__ import annotations

import pytest

from backend.features.extensions.compiler import (
    compile_package,
    derive_flow_requirements,
    expand_permissions,
)
from backend.features.extensions.errors import (
    PackageIncompatible,
    PackageValidationError,
)
from backend.features.extensions.sources import ArchiveSource
from tests.extension_packages import (
    PNG_BYTES,
    fragment_meter_config_view,
    fragment_meter_manifest,
    fragment_meter_package,
    fragment_meter_reduce_flow,
    fragment_meter_value_view,
    full_manifest,
    full_package,
    manifest,
    metadata_package,
    meter_view,
    orbext,
    scoring_flow,
)


def compile_bytes(data: bytes):
    with ArchiveSource(data) as source:
        return compile_package(source)


# ── the happy path ──────────────────────────────────────────────────────────


def test_compiles_a_metadata_only_package():
    compiled = compile_bytes(metadata_package())
    assert compiled.extension_id == "scene-meter"
    assert sorted(compiled.files) == ["orb-extension.json"]
    assert compiled.requirements.permissions == frozenset()


def test_selects_only_referenced_files():
    """An unreferenced file is not compiled, persisted, or hashed."""
    with_readme = compile_bytes(
        orbext(
            {
                "orb-extension.json": full_manifest(),
                "flows/score-scene.json": scoring_flow(),
                "ui/inspector.json": meter_view(),
                "README.md": b"# Scene Meter\n",
                "src/build.ts": b"export const x = 1;",
            }
        )
    )
    assert sorted(with_readme.files) == ["flows/score-scene.json", "orb-extension.json", "ui/inspector.json"]
    # Same digest as the archive without the extra files: editing an
    # unreferenced README must not produce a new revision.
    assert with_readme.digest == compile_bytes(full_package()).digest


def test_derives_capabilities_from_operations_and_context_reads():
    compiled = compile_bytes(full_package())
    derived = compiled.requirements.permissions
    assert ("model.call", "agent") in derived  # from model.structured's lane
    assert ("state.write", "conversation") in derived  # from state.set's scope
    assert ("context.read", "draft") in derived  # from {{ctx.draft}} in the prompt
    assert ("ui.contribute", "inspector") in derived  # from the placement's slot


def test_compilation_is_repeatable_across_sources(tmp_path, monkeypatch):
    """Archive and content store must produce the same digest and requirements.

    If they could disagree, a package would activate as something other than
    what the user consented to at inspection.
    """
    import backend.database.connection as connection
    from backend.features.extensions import content_store
    from backend.features.extensions.sources import StoredSource

    monkeypatch.setattr(connection, "DB_PATH", str(tmp_path / "app.db"))
    from_archive = compile_bytes(full_package())
    content_store.materialize(from_archive.files)
    from_store = compile_package(StoredSource(content_store.content_path(from_archive.digest)))
    assert from_store.digest == from_archive.digest
    assert from_store.requirements == from_archive.requirements
    assert from_store.contract_fingerprint == from_archive.contract_fingerprint


# ── declaration coverage ────────────────────────────────────────────────────


def test_rejects_a_package_that_omits_a_capability_its_flow_uses():
    broken = full_manifest(
        permissions=[
            {"capability": "context.read", "field": "draft"},
            {"capability": "state.read", "scope": "conversation"},
            {"capability": "state.write", "scope": "conversation"},
            {"capability": "ui.contribute", "slot": "inspector"},
        ]
    )
    with pytest.raises(PackageValidationError, match="model.call"):
        compile_bytes(
            orbext(
                {
                    "orb-extension.json": broken,
                    "flows/score-scene.json": scoring_flow(),
                    "ui/inspector.json": meter_view(),
                }
            )
        )


def test_a_grant_on_one_scope_does_not_cover_another():
    """Approving ``state.write`` on ``conversation`` is not approving it on
    ``character``: the parameter is part of the grant, not decoration."""
    flow = {
        "flow_version": 1,
        "steps": [{"op": "state.set", "scope": "character", "path": "x", "value": 1}],
    }
    declared = manifest(
        requires={"operations": ["state.set"], "components": []},
        permissions=[{"capability": "state.write", "scope": "conversation"}],
        actions={"go": {"flow": "flows/go.json"}},
    )
    with pytest.raises(PackageValidationError, match="'character'"):
        compile_bytes(orbext({"orb-extension.json": declared, "flows/go.json": flow}))


def test_a_resumable_library_sweep_derives_character_state_read():
    flow = {"flow_version": 1, "steps": [{"op": "return", "value": None}]}
    sweep = {
        "view_version": 1,
        "data": {"library": {"kind": "resource", "resource": "library.cards"}},
        "root": {
            "component": "library-sweep",
            "action": "classify",
            "label": "Classify",
            "unclassified_key": "done",
        },
    }
    declared = manifest(
        requires={"operations": ["return"], "components": ["library-sweep"]},
        permissions=[
            {"capability": "context.read", "field": "character"},
            {"capability": "library.cards.read"},
        ],
        actions={"classify": {"flow": "flows/classify.json"}},
        views={"workspace": {"source": "ui/workspace.json"}},
    )
    with pytest.raises(PackageValidationError, match=r"state\.read.*character"):
        compile_bytes(
            orbext(
                {
                    "orb-extension.json": declared,
                    "flows/classify.json": flow,
                    "ui/workspace.json": sweep,
                }
            )
        )


def test_rejects_an_operation_missing_from_requires():
    declared = full_manifest(requires={"operations": ["model.structured", "state.set"], "components": ["card", "meter"]})
    with pytest.raises(PackageValidationError, match="ui.invalidate"):
        compile_bytes(
            orbext(
                {
                    "orb-extension.json": declared,
                    "flows/score-scene.json": scoring_flow(),
                    "ui/inspector.json": meter_view(),
                }
            )
        )


def test_a_capability_behind_an_unsatisfiable_predicate_still_counts():
    """Validation is conservative over all branches.

    A consent diff that could be shrunk by adding ``when: {"eq": [1, 2]}``
    would not be a consent diff.
    """
    flow = {
        "flow_version": 1,
        "steps": [
            {
                "op": "if",
                "when": {"eq": [1, 2]},
                "then": [{"op": "state.set", "scope": "conversation", "path": "x", "value": 1}],
            }
        ],
    }
    declared = manifest(
        requires={"operations": ["if", "state.set"], "components": []},
        actions={"go": {"flow": "flows/go.json"}},
    )
    with pytest.raises(PackageValidationError, match="state.write"):
        compile_bytes(orbext({"orb-extension.json": declared, "flows/go.json": flow}))


def test_persona_is_a_view_resource_not_a_flow_context_field():
    flow = {
        "flow_version": 1,
        "steps": [{"op": "return", "value": {"$ref": "ctx.persona.name"}}],
    }
    declared = manifest(
        requires={"operations": ["return"], "components": []},
        permissions=[{"capability": "context.read", "field": "persona"}],
        actions={"go": {"flow": "flows/go.json"}},
    )
    with pytest.raises(PackageValidationError, match=r"ctx\.persona.*resource"):
        compile_bytes(orbext({"orb-extension.json": declared, "flows/go.json": flow}))


def test_rejects_an_undeclared_secret_reference():
    flow = {
        "flow_version": 1,
        "steps": [
            {
                "op": "http.request",
                "method": "GET",
                "url": "https://api.example.com/v1",
                "headers": {"Authorization": {"$secret": "api_key"}},
            }
        ],
    }
    declared = manifest(
        requires={"operations": ["http.request"], "components": []},
        permissions=[{"capability": "network.request", "origin": "https://api.example.com"}],
        actions={"go": {"flow": "flows/go.json"}},
    )
    with pytest.raises(PackageValidationError, match="undeclared secret"):
        compile_bytes(orbext({"orb-extension.json": declared, "flows/go.json": flow}))


def test_a_literal_url_must_match_a_granted_origin():
    flow = {
        "flow_version": 1,
        "steps": [{"op": "http.request", "method": "GET", "url": "https://evil.example/v1"}],
    }
    declared = manifest(
        requires={"operations": ["http.request"], "components": []},
        permissions=[{"capability": "network.request", "origin": "https://api.example.com"}],
        actions={"go": {"flow": "flows/go.json"}},
    )
    with pytest.raises(PackageValidationError, match="https://evil.example"):
        compile_bytes(orbext({"orb-extension.json": declared, "flows/go.json": flow}))


# ── flow context rules ──────────────────────────────────────────────────────


def test_rejects_draft_replace_in_an_observe_hook():
    """An observer sees the final immutable draft; the stage is not a flag it
    can talk its way past."""
    flow = {"flow_version": 1, "steps": [{"op": "draft.replace", "value": "rewritten"}]}
    declared = manifest(
        requires={"operations": ["draft.replace"], "components": []},
        permissions=[{"capability": "draft.replace"}],
        hooks={"post_pipeline": {"flow": "flows/go.json", "stage": "observe"}},
    )
    with pytest.raises(PackageValidationError, match="post_observe"):
        compile_bytes(orbext({"orb-extension.json": declared, "flows/go.json": flow}))


def test_rejects_context_append_outside_the_pre_hook():
    flow = {
        "flow_version": 1,
        "steps": [{"op": "context.append", "targets": ["writer"], "label": "Note", "text": "hi"}],
    }
    declared = manifest(
        requires={"operations": ["context.append"], "components": []},
        permissions=[{"capability": "prompt.context.append", "targets": ["writer"]}],
        actions={"go": {"flow": "flows/go.json"}},
    )
    with pytest.raises(PackageValidationError, match="not allowed in a action flow"):
        compile_bytes(orbext({"orb-extension.json": declared, "flows/go.json": flow}))


def test_rejects_a_view_dispatching_an_undeclared_action():
    view = {
        "view_version": 1,
        "root": {"component": "button", "label": "Go", "action": "nope"},
    }
    declared = manifest(
        requires={"operations": [], "components": ["button"]},
        permissions=[{"capability": "ui.contribute", "slot": "inspector"}],
        views={"inspector": {"source": "ui/inspector.json"}},
        placements=[{"slot": "inspector", "view": "inspector"}],
    )
    with pytest.raises(PackageValidationError, match="undeclared action"):
        compile_bytes(orbext({"orb-extension.json": declared, "ui/inspector.json": view}))


# ── fragment contribution views ─────────────────────────────────────────────


def _fragment_package(*, config_view=None, value_view=None) -> bytes:
    return orbext(
        {
            "orb-extension.json": fragment_meter_manifest(),
            "flows/reduce-meter.json": fragment_meter_reduce_flow(),
            "ui/meter-config.json": config_view or fragment_meter_config_view(),
            "ui/meter-value.json": value_view or fragment_meter_value_view(),
        }
    )


def test_fragment_views_use_host_owned_data_without_state_permissions():
    compiled = compile_bytes(fragment_meter_package())
    assert compiled.requirements.permissions == frozenset({("fragment_type.contribute", None)})


def test_fragment_config_view_cannot_bind_an_undeclared_config_key():
    view = fragment_meter_config_view()
    view["root"]["children"][0]["bind"] = "config.not_declared"
    with pytest.raises(PackageValidationError, match="undeclared config key"):
        compile_bytes(_fragment_package(config_view=view))


def test_fragment_value_view_is_display_only():
    view = fragment_meter_value_view()
    view["root"] = {
        "component": "number-input",
        "bind": "config.initial",
        "label": "Initial",
    }
    with pytest.raises(PackageValidationError, match="display-only"):
        compile_bytes(_fragment_package(value_view=view))


def test_fragment_views_cannot_read_extension_state():
    view = fragment_meter_config_view()
    view["data"] = {"saved": {"kind": "state", "scope": "config"}}
    with pytest.raises(PackageValidationError, match="may not declare data sources"):
        compile_bytes(_fragment_package(config_view=view))


# ── assets ──────────────────────────────────────────────────────────────────


def test_compiles_a_referenced_image_asset():
    view = {
        "view_version": 1,
        "root": {"component": "image", "source": {"kind": "asset", "path": "assets/icon.png"}, "alt": "Icon"},
    }
    declared = manifest(
        requires={"operations": [], "components": ["image"]},
        permissions=[{"capability": "ui.contribute", "slot": "inspector"}],
        views={"inspector": {"source": "ui/inspector.json"}},
        placements=[{"slot": "inspector", "view": "inspector"}],
    )
    compiled = compile_bytes(orbext({"orb-extension.json": declared, "ui/inspector.json": view, "assets/icon.png": PNG_BYTES}))
    assert compiled.asset_types["assets/icon.png"] == "image/png"


def test_rejects_an_svg_asset():
    """SVG renders in an <img> and carries script; "it is an image" is not the
    same claim as "it is inert"."""
    view = {
        "view_version": 1,
        "root": {"component": "image", "source": {"kind": "asset", "path": "assets/icon.svg"}, "alt": "Icon"},
    }
    declared = manifest(
        requires={"operations": [], "components": ["image"]},
        permissions=[{"capability": "ui.contribute", "slot": "inspector"}],
        views={"inspector": {"source": "ui/inspector.json"}},
        placements=[{"slot": "inspector", "view": "inspector"}],
    )
    with pytest.raises(PackageValidationError, match="active format"):
        compile_bytes(orbext({"orb-extension.json": declared, "ui/inspector.json": view, "assets/icon.svg": b"<svg/>"}))


def test_rejects_an_asset_whose_bytes_contradict_its_extension():
    view = {
        "view_version": 1,
        "root": {"component": "image", "source": {"kind": "asset", "path": "assets/icon.png"}, "alt": "Icon"},
    }
    declared = manifest(
        requires={"operations": [], "components": ["image"]},
        permissions=[{"capability": "ui.contribute", "slot": "inspector"}],
        views={"inspector": {"source": "ui/inspector.json"}},
        placements=[{"slot": "inspector", "view": "inspector"}],
    )
    with pytest.raises(PackageValidationError, match="does not contain image/png"):
        compile_bytes(
            orbext(
                {
                    "orb-extension.json": declared,
                    "ui/inspector.json": view,
                    "assets/icon.png": b"<html><script>alert(1)</script>",
                }
            )
        )


# ── compatibility ───────────────────────────────────────────────────────────


def test_a_future_extension_api_is_incompatible_not_invalid():
    """The distinction decides the user's next step: update Orb, or report a
    bug to the author.

    Checked against the raw integer before any strict parsing, which is the
    whole reason the versioned dispatch exists: every field of a future
    manifest would otherwise look unknown, and ``extra="forbid"`` would report
    "malformed package" for "package from a newer Orb"."""
    with pytest.raises(PackageIncompatible, match="extension_api 3"):
        compile_bytes(metadata_package(extension_api=3))


def test_extension_api_2_is_supported():
    """API 2 is implemented, so a v2 manifest compiles rather than reporting
    that this build is too old."""
    compiled = compile_bytes(metadata_package(extension_api=2))
    assert compiled.manifest.extension_api == 2
    assert compiled.writer_tool is None


def test_an_unknown_declared_requirement_compiles_but_is_unavailable():
    compiled = compile_bytes(metadata_package(requires={"operations": ["quantum.entangle"], "components": []}))
    assert compiled.unavailable == ("operation 'quantum.entangle'",)


# ── helpers used by the publisher ───────────────────────────────────────────


def test_a_parameterized_grant_also_satisfies_the_bare_form():
    expanded = expand_permissions([{"capability": "state.read", "scope": "conversation"}])
    assert ("state.read", None) in expanded
    assert ("state.read", "conversation") in expanded
    assert ("state.read", "character") not in expanded


def test_multi_valued_parameters_expand_per_value():
    expanded = expand_permissions([{"capability": "prompt.context.append", "targets": ["director", "writer"]}])
    assert ("prompt.context.append", "director") in expanded
    assert ("prompt.context.append", "writer") in expanded


def test_flow_requirements_match_the_whole_package_derivation():
    compiled = compile_bytes(full_package())
    flow = compiled.flows["flows/score-scene.json"]
    per_flow = derive_flow_requirements(flow)
    assert per_flow <= compiled.requirements.permissions
    assert ("model.call", "agent") in per_flow
