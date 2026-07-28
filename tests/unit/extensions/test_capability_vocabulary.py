"""The capability table and everything derived from it.

``CAPABILITY_SPECS`` replaced six independent tables -- consent copy, the loud
set, the data-reading set, the resource map, the prerequisite map, and the
Pydantic permission union -- that nothing linked. The failures that arrangement
allowed were all silent: a grant with no consent line rendered as a bare
identifier, a read grant missing from the data-reading set turned off the
combination banner for exactly the packages that needed it, and a resource could
name a capability that did not exist.

These tests hold the derivations closed. They are deliberately about the
*shape* of the vocabulary rather than about any one grant, so adding a
capability does not mean editing them -- which is the point: a table that has to
be updated in lockstep with another is the thing being removed here.
"""

from __future__ import annotations

import pytest

from backend.features.extensions.contracts import (
    CAPABILITY_SPECS,
    GRANT_PREREQUISITES,
    OPERATION_SPECS,
    RESOURCE_CAPABILITIES,
    Capability,
    DataClass,
    Sensitivity,
    describe,
    missing_prerequisites,
    parameter_values,
    with_prerequisites,
)
from backend.features.extensions.contracts.capabilities import UNKNOWN_GRANT_COPY


def test_every_capability_has_a_spec():
    assert set(CAPABILITY_SPECS) == set(Capability)


@pytest.mark.parametrize("capability", sorted(Capability))
def test_every_grant_has_consent_copy(capability):
    """Including every value of a parameterized grant.

    The old table was keyed by capability with a ``.get`` fallback, so a missing
    line degraded to shipped text reading "a capability this Orb build does not
    describe" instead of failing anywhere a developer would see it.
    """
    values = parameter_values(capability) or {None}
    for value in values:
        description = describe(capability.value, value)
        assert description.copy and description.copy != UNKNOWN_GRANT_COPY
        assert description.copy[0].isupper() and description.copy.endswith((".", "!"))


def test_unknown_capability_describes_itself_as_unknown_and_loud():
    """A grant this build cannot explain is never rendered as routine."""
    description = describe("invented.capability")
    assert description.copy == UNKNOWN_GRANT_COPY
    assert description.sensitivity is Sensitivity.HIGH


def test_parameterized_copy_differs_per_value():
    """The consent row's identity includes the parameter, so its copy must too."""
    config = describe(Capability.STATE_WRITE.value, "config").copy
    character = describe(Capability.STATE_WRITE.value, "character").copy
    assert config != character


def test_operation_capabilities_and_parameters_exist_in_the_vocabulary():
    for name, spec in OPERATION_SPECS.items():
        if spec.capability is None:
            assert spec.parameter is None and spec.parameter_field is None, name
            continue
        assert spec.capability in CAPABILITY_SPECS, name
        if spec.parameter is not None:
            assert spec.parameter in parameter_values(spec.capability), name


def test_every_resource_is_gated_by_a_real_grant():
    for resource, (capability, parameter) in RESOURCE_CAPABILITIES.items():
        assert capability in {c.value for c in Capability}, resource
        values = parameter_values(Capability(capability))
        assert parameter in values if values else parameter is None, resource


def test_resource_map_covers_exactly_the_served_resources():
    """The compiler's admissible resource names and the adapters agree."""
    from backend.features.extensions.resources import RESOURCE_NAMES

    assert set(RESOURCE_CAPABILITIES) == set(RESOURCE_NAMES)


def test_prerequisites_name_grants_that_exist():
    for grant, required in GRANT_PREREQUISITES.items():
        for pair in (grant, *required):
            capability, parameter = pair
            values = parameter_values(Capability(capability))
            assert parameter in values if values else parameter is None, pair


def test_prerequisites_are_transitively_resolved():
    """A derivation states what it reaches; the table supplies the rest."""
    resolved = with_prerequisites({(Capability.CARD_WRITE.value, "tags")})
    assert (Capability.CONTEXT_READ.value, "character") in resolved
    assert not missing_prerequisites(resolved)


def test_a_grant_missing_its_prerequisite_is_reported():
    unmet = missing_prerequisites({(Capability.CONVERSATION_TREE_READ.value, "preview")})
    assert unmet == [
        (
            (Capability.CONVERSATION_TREE_READ.value, "preview"),
            (Capability.CONVERSATION_TREE_READ.value, "structure"),
        )
    ]


def test_read_grants_declare_what_they_read():
    """A read grant exposing nothing is a claim, and it should be a visible one.

    This is what the combination banner keys off. The old hand-listed set could
    omit a grant and nothing would notice; here an omission means the grant
    asserts it reads no user data, which a reviewer can check against the
    projection.
    """
    from backend.features.extensions.contracts import GrantKind, reads_user_data

    for capability, spec in CAPABILITY_SPECS.items():
        if spec.kind is not GrantKind.READ:
            continue
        values = parameter_values(capability) or {None}
        exposed = reads_user_data({(capability.value, value) for value in values})
        assert exposed, f"{capability.value} is a read grant that declares no data class"
        assert exposed <= set(DataClass)
