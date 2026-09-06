"""Provider-facing structured-output schema normalization."""

from __future__ import annotations


def strictify_schema(schema: dict) -> dict:
    """Copy *schema* into OpenAI strict-mode shape recursively."""
    node = dict(schema)
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
