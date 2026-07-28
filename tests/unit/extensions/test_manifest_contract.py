"""Manifest contract: what a v1 package may say, and what it may not.

The corpus is written the way an attacker would write a manifest -- claiming
less than the code does, occupying a slot it never asked for, pointing at a
wildcard origin, declaring an artifact producer with no way to regenerate --
because the manifest's job is not to describe the package, it is to be the
thing the user's consent is measured against.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.extensions.contracts import (
    UI_SLOTS,
    Capability,
    ExtensionManifest,
    namespaced_fragment_type,
    permission_key,
    split_fragment_type,
)

BASE = {
    "extension_api": 1,
    "id": "scene-meter",
    "name": "Scene Meter",
    "version": "1.0.0",
}


def manifest(**overrides) -> ExtensionManifest:
    return ExtensionManifest.model_validate({**BASE, **overrides})


# ── identity ─────────────────────────────────────────────────────────────────


def test_minimal_manifest_validates():
    m = manifest()
    assert m.id == "scene-meter"
    assert m.permissions == []
    assert m.requires.unknown() == []


@pytest.mark.parametrize(
    "bad_id",
    [
        "Scene-Meter",
        "scene meter",
        "-scene",
        "scene/meter",
        "",
        "a" * 65,
        "scene:meter",
        "../evil",
    ],
)
def test_id_grammar_is_enforced(bad_id):
    with pytest.raises(ValidationError):
        manifest(id=bad_id)


def test_an_unsupported_extension_api_is_rejected():
    with pytest.raises(ValidationError):
        manifest(extension_api=3)


def test_unknown_top_level_field_is_rejected():
    # Not ignored: a field this build does not understand would silently do
    # nothing now and something later, without an update or fresh consent.
    with pytest.raises(ValidationError):
        manifest(minimum_orb_version="9.9.9")


def test_homepage_must_be_https():
    with pytest.raises(ValidationError):
        manifest(homepage="javascript:alert(1)")


def test_version_must_be_semver_shaped():
    with pytest.raises(ValidationError):
        manifest(version="latest")


# ── permissions ──────────────────────────────────────────────────────────────


def test_scoped_permissions_are_distinct_grants():
    m = manifest(
        permissions=[
            {"capability": "state.write", "scope": "conversation"},
            {"capability": "state.write", "scope": "character"},
        ]
    )
    assert len(m.granted()) == 2


def test_duplicate_permission_request_is_rejected():
    with pytest.raises(ValidationError, match="duplicate permission"):
        manifest(
            permissions=[
                {"capability": "state.write", "scope": "conversation"},
                {"capability": "state.write", "scope": "conversation"},
            ]
        )


def test_permission_key_is_stable_and_parameterized():
    m = manifest(permissions=[{"capability": "model.call", "lane": "agent"}])
    assert permission_key(m.permissions[0]) == ("model.call", "agent")


def test_unknown_capability_is_rejected():
    with pytest.raises(ValidationError):
        manifest(permissions=[{"capability": "filesystem.read"}])


def test_capability_parameters_are_required_not_optional():
    with pytest.raises(ValidationError):
        manifest(permissions=[{"capability": "model.call"}])


@pytest.mark.parametrize(
    "bad_origin",
    [
        "https://*.example.com",
        "https://example.com/path",
        "https://user:pw@example.com",
        "file:///etc/passwd",
        "ftp://example.com",
        "https://example.com:99999",
        "https://exa mple.com",
        "example.com",
        "http://::1:8188",  # unbracketed IPv6: ambiguous with the port separator
        "https://[not-an-address]",
    ],
)
def test_hostile_origins_are_rejected(bad_origin):
    with pytest.raises(ValidationError):
        manifest(permissions=[{"capability": "network.request", "origin": bad_origin}])


@pytest.mark.parametrize(
    "ok_origin",
    [
        "https://api.example.com",
        "http://127.0.0.1:8188",
        "https://example.com:8443",
        "http://[::1]:8188",
    ],
)
def test_exact_origins_are_accepted(ok_origin):
    m = manifest(permissions=[{"capability": "network.request", "origin": ok_origin}])
    assert m.origins() == [ok_origin]


def test_unknown_ui_slot_is_rejected():
    with pytest.raises(ValidationError):
        manifest(permissions=[{"capability": "ui.contribute", "slot": "settings.root"}])


def test_every_declared_slot_is_a_known_slot():
    assert "composer.menu" in UI_SLOTS and "workspace" in UI_SLOTS


# ── entry points and intra-manifest references ───────────────────────────────


def test_placement_requires_matching_slot_consent():
    # Approving "inspector" is not approving "composer.menu": consent is per
    # slot, and a placement that outran its grant is a validation error rather
    # than a silently dropped placement.
    with pytest.raises(ValidationError, match="ui.contribute"):
        manifest(
            permissions=[{"capability": "ui.contribute", "slot": "inspector"}],
            views={"main": {"source": "ui/main.json"}},
            placements=[{"slot": "composer.menu", "view": "main"}],
        )


def test_placement_naming_an_undeclared_view_is_rejected():
    with pytest.raises(ValidationError, match="undeclared view"):
        manifest(
            permissions=[{"capability": "ui.contribute", "slot": "inspector"}],
            placements=[{"slot": "inspector", "view": "ghost"}],
        )


def test_placement_must_name_exactly_one_target():
    with pytest.raises(ValidationError, match="exactly one"):
        manifest(
            permissions=[{"capability": "ui.contribute", "slot": "inspector"}],
            views={"main": {"source": "ui/main.json"}},
            commands=[{"id": "go", "label": "Go", "opens": "main"}],
            placements=[{"slot": "inspector", "view": "main", "command": "go"}],
        )


def test_command_must_name_exactly_one_target():
    with pytest.raises(ValidationError, match="exactly one"):
        manifest(commands=[{"id": "go", "label": "Go"}])


def test_command_opening_an_undeclared_view_is_rejected():
    with pytest.raises(ValidationError, match="undeclared view"):
        manifest(commands=[{"id": "go", "label": "Go", "opens": "ghost"}])


def test_command_icon_must_be_a_known_symbolic_name():
    # Icons are Orb-owned names, never asset paths or markup.
    with pytest.raises(ValidationError):
        manifest(
            views={"main": {"source": "ui/main.json"}},
            commands=[
                {
                    "id": "go",
                    "label": "Go",
                    "opens": "main",
                    "icon": "https://evil/x.svg",
                }
            ],
        )


def test_duplicate_command_id_is_rejected():
    with pytest.raises(ValidationError, match="duplicate command"):
        manifest(
            views={"main": {"source": "ui/main.json"}},
            commands=[
                {"id": "go", "label": "A", "opens": "main"},
                {"id": "go", "label": "B", "opens": "main"},
            ],
        )


def test_hook_paths_must_be_contained_relative_paths():
    with pytest.raises(ValidationError):
        manifest(hooks={"pre_pipeline": {"flow": "../../etc/passwd"}})


def test_post_hook_stage_is_required():
    with pytest.raises(ValidationError):
        manifest(hooks={"post_pipeline": {"flow": "flows/f.json"}})


def test_post_hook_stage_must_be_known():
    with pytest.raises(ValidationError):
        manifest(hooks={"post_pipeline": {"flow": "flows/f.json", "stage": "rewrite"}})


# ── artifacts ────────────────────────────────────────────────────────────────


def test_artifact_producer_must_supply_both_recovery_flows():
    with pytest.raises(ValidationError, match="artifact_flows"):
        manifest(produces_artifacts=True, permissions=[{"capability": "artifact.write"}])


def test_artifact_producer_must_hold_the_artifact_permission():
    with pytest.raises(ValidationError, match="artifact.write"):
        manifest(
            produces_artifacts=True,
            artifact_flows={
                "regenerate": "flows/r.json",
                "reroll_gen": "flows/rr.json",
            },
        )


def test_artifact_flows_without_the_declaration_are_rejected():
    with pytest.raises(ValidationError, match="only meaningful"):
        manifest(artifact_flows={"regenerate": "flows/r.json", "reroll_gen": "flows/rr.json"})


def test_valid_artifact_producer_is_accepted():
    m = manifest(
        produces_artifacts=True,
        permissions=[{"capability": "artifact.write"}],
        artifact_flows={"regenerate": "flows/r.json", "reroll_gen": "flows/rr.json"},
    )
    assert "flows/rr.json" in m.referenced_flow_paths()


# ── requires / feature detection ─────────────────────────────────────────────


def test_unknown_requirements_are_reported_not_rejected():
    # An unknown requirement means "this Orb is too old", which keeps the
    # package installed with a diagnostic -- a different outcome, and a
    # different user action, than a malformed package.
    m = manifest(requires={"operations": ["quantum.entangle"], "components": ["hologram"]})
    assert m.requires.unknown() == [
        "component 'hologram'",
        "operation 'quantum.entangle'",
    ]


def test_known_requirements_report_nothing():
    m = manifest(
        requires={
            "operations": ["state.set", "model.text"],
            "components": ["meter", "table"],
        }
    )
    assert m.requires.unknown() == []


# ── fragment types ───────────────────────────────────────────────────────────

METER_TYPE = {
    "id": "meter",
    "label": "Meter",
    "storage": "assistant_progressive",
    "config_schema": {
        "type": "object",
        "properties": {
            "minimum": {"type": "integer"},
            "maximum": {"type": "integer"},
            "initial": {"type": "integer"},
            "max_delta": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["minimum", "maximum", "initial", "max_delta"],
        "additionalProperties": False,
    },
    "director_schema": {
        "type": "object",
        "properties": {
            "delta": {
                "type": "integer",
                "minimum": {"$neg_config": "max_delta"},
                "maximum": {"$config": "max_delta"},
            },
            "reason": {"type": "string", "maxLength": 160},
        },
        "required": ["delta", "reason"],
        "additionalProperties": False,
    },
    "prior_context": {"$template": "{{fragment.injection_label}} is currently {{fragment.previous.value}}."},
    "reduce_flow": "flows/reduce-meter.json",
    "writer_context": {"$template": "{{fragment.injection_label}}: {{fragment.current.value}}"},
}


def test_reference_meter_descriptor_validates():
    m = manifest(
        permissions=[{"capability": "fragment_type.contribute"}],
        contributions={"fragment_types": [METER_TYPE]},
    )
    assert m.contributions.fragment_types[0].id == "meter"
    assert Capability.FRAGMENT_TYPE_CONTRIBUTE in m.capabilities()


def test_contributing_a_type_requires_its_permission():
    with pytest.raises(ValidationError, match="fragment_type.contribute"):
        manifest(contributions={"fragment_types": [METER_TYPE]})


def test_config_template_must_name_a_declared_integer_config_key():
    broken = {
        **METER_TYPE,
        "director_schema": {
            **METER_TYPE["director_schema"],
            "properties": {
                **METER_TYPE["director_schema"]["properties"],
                "delta": {"type": "integer", "maximum": {"$config": "nonexistent"}},
            },
        },
    }
    with pytest.raises(ValidationError, match="undeclared config key"):
        manifest(
            permissions=[{"capability": "fragment_type.contribute"}],
            contributions={"fragment_types": [broken]},
        )


def test_config_template_key_must_be_an_integer():
    broken = {
        **METER_TYPE,
        "config_schema": {
            **METER_TYPE["config_schema"],
            "properties": {
                **METER_TYPE["config_schema"]["properties"],
                "max_delta": {"type": "string"},
            },
        },
    }
    with pytest.raises(ValidationError, match="must be an integer"):
        manifest(
            permissions=[{"capability": "fragment_type.contribute"}],
            contributions={"fragment_types": [broken]},
        )


def test_config_template_key_must_be_required():
    broken = {
        **METER_TYPE,
        "config_schema": {
            **METER_TYPE["config_schema"],
            "required": ["minimum", "maximum", "initial"],
        },
    }
    with pytest.raises(ValidationError, match="must be required"):
        manifest(
            permissions=[{"capability": "fragment_type.contribute"}],
            contributions={"fragment_types": [broken]},
        )


def test_descriptor_template_cannot_reference_an_unknown_namespace():
    broken = {**METER_TYPE, "writer_context": {"$template": "{{settings.api_key}}"}}
    with pytest.raises(ValidationError):
        manifest(
            permissions=[{"capability": "fragment_type.contribute"}],
            contributions={"fragment_types": [broken]},
        )


def test_unknown_storage_policy_is_rejected():
    with pytest.raises(ValidationError):
        manifest(
            permissions=[{"capability": "fragment_type.contribute"}],
            contributions={"fragment_types": [{**METER_TYPE, "storage": "conversation_row"}]},
        )


# ── namespaced fragment type ids ─────────────────────────────────────────────


def test_namespacing_round_trips():
    stored = namespaced_fragment_type("scene-meter", "meter")
    assert stored == "scene-meter:meter"
    assert split_fragment_type(stored) == ("scene-meter", "meter")


@pytest.mark.parametrize("core", ["string", "array", "progressive", "feedback", "direction_note"])
def test_core_types_are_not_namespaced(core):
    # split returns None for a core type, which is what preserves the existing
    # legacy fallback-to-string behavior for non-namespaced unknowns.
    assert split_fragment_type(core) is None


@pytest.mark.parametrize("malformed", ["a:b:c", ":meter", "scene-meter:", "Scene:Meter", "a:B"])
def test_malformed_namespaced_values_are_not_split(malformed):
    assert split_fragment_type(malformed) is None
