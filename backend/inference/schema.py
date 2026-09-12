"""Provider-facing structured-output schema normalization."""

from __future__ import annotations

# Validation keywords that strict structured output does not accept. Each one
# only *narrows* which documents are valid, so dropping it can widen what the
# model may emit but never changes the shape the caller parses back -- which is
# why the whole set can be stripped in one pass, where composition keywords
# (allOf / oneOf / not / if) could not be, and are simply never emitted here.
#
# Measured on nano-gpt.com against ibm-granite/granite-4.2-8b: a strict
# json_schema carrying uniqueItems, contains, minContains, unevaluatedItems,
# propertyNames, unevaluatedProperties, or dependentRequired is answered HTTP
# 400 "Invalid request parameters", naming no field -- the library auto-tagger's
# uniqueItems was the first to hit it. The remaining names sit outside the same
# specification; they are dropped alongside rather than waiting for the route
# that enforces the list in full. Kept because the subset includes them:
# enum, const, min/maxItems, min/maxLength, pattern, format, numeric bounds.
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


def strictify_schema(schema: dict) -> dict:
    """Copy *schema* into OpenAI strict-mode shape recursively."""
    node = {key: value for key, value in schema.items() if key not in _NON_STRICT_KEYWORDS}
    properties = node.get("properties")
    if isinstance(properties, dict):
        required = set(node.get("required") or [])
        output_properties: dict = {}
        for key, property_schema in properties.items():
            sub = strictify_schema(property_schema) if isinstance(property_schema, dict) else property_schema
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
        node["items"] = strictify_schema(node["items"])
    return node
