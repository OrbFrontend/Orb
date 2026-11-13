"""Provider-facing structured-output schema normalization."""

from __future__ import annotations

# Strip unsupported narrowing validation keywords for strict structured output. This may widen accepted values but preserves
# parsed shape. Do not strip composition keywords; they are never emitted here. Supported constraints include enum/const,
# bounds, pattern and format.
_NON_STRICT_KEYWORDS = frozenset(
    {
        "contains",
        "dependentRequired",
        "dependentSchemas",
        "maxContains",
        "maxProperties",
        "minContains",
        "minProperties",
        "patternProperties",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "uniqueItems",
    }
)


# Anthropic's strict tool use also rejects value and size bounds.
_BOUND_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
)


def strictify_schema(schema: dict, *, bounds: bool = True) -> dict:
    """Copy *schema* into OpenAI strict-mode shape recursively; ``bounds=False`` also drops value and size bounds."""
    dropped = _NON_STRICT_KEYWORDS if bounds else _NON_STRICT_KEYWORDS | _BOUND_KEYWORDS
    node = {key: value for key, value in schema.items() if key not in dropped}
    properties = node.get("properties")
    if isinstance(properties, dict):
        required = set(node.get("required") or [])
        output_properties: dict = {}
        for key, property_schema in properties.items():
            sub = strictify_schema(property_schema, bounds=bounds) if isinstance(property_schema, dict) else property_schema
            if key not in required and isinstance(sub, dict) and "type" in sub:
                property_type = sub["type"]
                if isinstance(property_type, list):
                    property_type = property_type if "null" in property_type else [*property_type, "null"]
                elif property_type != "null":
                    property_type = [property_type, "null"]
                sub = {**sub, "type": property_type}
            output_properties[key] = sub
        node["properties"] = output_properties
        node["required"] = list(properties.keys())
        node["additionalProperties"] = False
    if isinstance(node.get("items"), dict):
        node["items"] = strictify_schema(node["items"], bounds=bounds)
    return node
