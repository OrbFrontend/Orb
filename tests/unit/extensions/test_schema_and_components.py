"""The JSON Schema subset and the host-rendered component tree.

Both are allowlists. The tests that matter are the ones proving an unsupported
construct is an *error* rather than an ignored annotation: a schema keyword
that silently does nothing constrains less than its author believes, and a
component property that silently does nothing is one renderer change away from
becoming a DOM attribute.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.extensions.contracts import (
    COMPONENT_NAMES,
    View,
    parse_schema,
    referenced_actions,
    referenced_assets,
    used_components,
)
from backend.features.extensions.contracts.schema_subset import (
    MAX_ENUM_MEMBERS,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_PROPERTIES,
    SchemaError,
)

# ── schema subset ────────────────────────────────────────────────────────────


def test_object_schema_validates_and_closes():
    s = parse_schema({"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]})
    assert s.schema["additionalProperties"] is False
    assert s.validate({"a": 1}) is None
    assert s.validate({"a": 1, "b": 2}) is not None
    assert s.validate({}) is not None


def test_additional_properties_true_is_rejected():
    # An open object defeats every size bound derived from the declaration.
    with pytest.raises(SchemaError, match="additionalProperties"):
        parse_schema({"type": "object", "properties": {}, "additionalProperties": True})


@pytest.mark.parametrize(
    "keyword",
    [
        {"$ref": "#/definitions/x"},
        {"oneOf": [{"type": "string"}]},
        {"allOf": [{"type": "string"}]},
        {"not": {"type": "string"}},
        {"patternProperties": {".*": {"type": "string"}}},
        {"pattern": "^(a+)+$"},
        {"$id": "https://evil.invalid/schema"},
    ],
)
def test_unsupported_keywords_are_rejected(keyword):
    with pytest.raises(SchemaError, match="unsupported schema keyword"):
        parse_schema({"type": "object", "properties": {}, **keyword})


def test_missing_type_is_rejected():
    with pytest.raises(SchemaError, match="'type'"):
        parse_schema({"properties": {}})


def test_required_naming_an_undeclared_property_is_rejected():
    with pytest.raises(SchemaError, match="undeclared"):
        parse_schema(
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "required": ["b"],
            }
        )


def test_default_must_satisfy_its_own_schema():
    # A bad default fires only when a consumer omits the field, which can be
    # long after install -- so it is checked at parse time.
    with pytest.raises(SchemaError, match="'default'"):
        parse_schema({"type": "integer", "minimum": 0, "default": -5})


def test_valid_default_is_kept():
    s = parse_schema({"type": "integer", "minimum": 0, "default": 5})
    assert s.schema["default"] == 5


def test_schema_depth_is_bounded():
    node = {"type": "integer"}
    for _ in range(MAX_SCHEMA_DEPTH + 2):
        node = {"type": "object", "properties": {"x": node}}
    with pytest.raises(SchemaError, match="nests deeper"):
        parse_schema(node)


def test_property_count_is_bounded():
    props = {f"p{i}": {"type": "integer"} for i in range(MAX_SCHEMA_PROPERTIES + 1)}
    with pytest.raises(SchemaError, match="exceeds the limit"):
        parse_schema({"type": "object", "properties": props})


def test_enum_length_is_bounded():
    with pytest.raises(SchemaError, match="enum has"):
        parse_schema({"type": "integer", "enum": list(range(MAX_ENUM_MEMBERS + 1))})


def test_booleans_are_not_integers():
    s = parse_schema({"type": "integer"})
    assert s.validate(True) is not None
    assert s.validate(1) is None


def test_non_finite_numbers_fail_validation():
    s = parse_schema({"type": "number"})
    assert s.validate(float("inf")) is not None


def test_array_bounds_are_enforced():
    s = parse_schema({"type": "array", "items": {"type": "string"}, "maxItems": 2})
    assert s.validate(["a", "b"]) is None
    assert s.validate(["a", "b", "c"]) is not None


def test_array_without_items_is_rejected():
    with pytest.raises(SchemaError, match="must declare 'items'"):
        parse_schema({"type": "array"})


def test_config_templates_require_opt_in():
    numeric = {"type": "integer", "maximum": {"$config": "max_delta"}}
    with pytest.raises(SchemaError, match=r"\$config"):
        parse_schema(numeric)
    s = parse_schema(numeric, allow_config_templates=True)
    assert s.config_keys == {"max_delta"}


def test_unresolved_config_bound_fails_runtime_validation():
    # A template that reached validation unresolved means the per-turn schema
    # build was skipped; failing closed beats validating against a dict.
    s = parse_schema(
        {"type": "integer", "maximum": {"$config": "max_delta"}},
        allow_config_templates=True,
    )
    assert s.validate(5) is not None


# ── components ───────────────────────────────────────────────────────────────


def view(root: dict) -> View:
    return View.model_validate({"view_version": 1, "root": root})


def test_reference_inspector_view_validates():
    v = view(
        {
            "component": "card",
            "title": "Scene",
            "children": [
                {
                    "component": "meter",
                    "value": {"$ref": "ctx.state.tension"},
                    "minimum": 0,
                    "maximum": 100,
                },
                {"component": "button", "label": "Rescore", "action": "rescore"},
                {
                    "component": "image",
                    "source": {"kind": "asset", "path": "assets/icon.webp"},
                    "alt": "icon",
                },
            ],
        }
    )
    assert used_components(v) == {"card", "meter", "button", "image"}
    assert referenced_actions(v) == {"rescore"}
    assert referenced_assets(v) == {"assets/icon.webp"}


def test_component_catalog_is_derived_from_the_union():
    assert {"stack", "markdown", "conversation-tree", "meter"} <= COMPONENT_NAMES


def test_a_view_cannot_run_an_action_implicitly_as_a_data_source():
    """Opening a view must never incur a model call or mutation on its own."""
    with pytest.raises(ValidationError):
        View.model_validate(
            {
                "view_version": 1,
                "data": {"result": {"kind": "action", "action": "rewrite"}},
                "root": {"component": "text", "value": {"$ref": "data.result"}},
            }
        )


def test_unknown_component_is_rejected():
    with pytest.raises(ValidationError):
        view({"component": "iframe", "src": "https://evil.invalid"})


@pytest.mark.parametrize(
    "prop",
    [
        {"onclick": "alert(1)"},
        {"onerror": "alert(1)"},
        {"style": "background:url(javascript:alert(1))"},
        {"innerHTML": "<img src=x onerror=alert(1)>"},
        {"class": "evil"},
        {"dangerouslySetInnerHTML": {"__html": "<script>"}},
    ],
)
def test_unknown_properties_never_survive_to_the_renderer(prop):
    # They fail validation, so there is no path by which they could become DOM
    # attributes -- the renderer never has to decide what to do with them.
    with pytest.raises(ValidationError):
        view({"component": "text", "value": "hi", **prop})


def test_style_tokens_are_closed_enumerations():
    with pytest.raises(ValidationError):
        view({"component": "text", "value": "hi", "tone": "url(javascript:alert(1))"})


def test_xss_payloads_are_ordinary_text_values():
    # A script tag in a value is data. It is the renderer's textContent that
    # makes it safe; the contract's job is only to not reject it, so a package
    # displaying user text about HTML still works.
    v = view({"component": "text", "value": "<script>alert(1)</script>"})
    assert v.root.value == "<script>alert(1)</script>"


def test_media_source_must_be_a_declared_kind():
    with pytest.raises(ValidationError):
        view(
            {
                "component": "image",
                "source": {"kind": "url", "href": "https://evil.invalid/x.png"},
                "alt": "x",
            }
        )


def test_media_asset_paths_go_through_normalization():
    with pytest.raises(ValidationError):
        view(
            {
                "component": "image",
                "source": {"kind": "asset", "path": "../../etc/passwd"},
                "alt": "x",
            }
        )


@pytest.mark.parametrize(
    "bad_bind",
    [
        "settings.api_key",
        "state.settings.api_key",
        "config",
        "config.a.b",
        "state.conversation",
        "endpoints.0.api_key",
    ],
)
def test_form_bindings_reach_only_this_extensions_own_slots(bad_bind):
    with pytest.raises(ValidationError):
        view({"component": "text-input", "bind": bad_bind, "label": "x"})


@pytest.mark.parametrize("ok_bind", ["config.api_url", "state.conversation.tension", "state.character.mood"])
def test_valid_bindings_are_accepted(ok_bind):
    v = view({"component": "text-input", "bind": ok_bind, "label": "x"})
    assert v.root.bind == ok_bind


def test_conversation_tree_takes_a_named_select_action():
    v = view(
        {
            "component": "conversation-tree",
            "nodes": {"$ref": "ctx.tree.nodes"},
            "active_path": {"$ref": "ctx.tree.active_path"},
            "select_action": "activate-branch",
        }
    )
    assert referenced_actions(v) == {"activate-branch"}
