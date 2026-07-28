// Pure helpers for the host-owned fallback editor for contributed fragment
// configuration. The accepted shapes mirror the backend's closed JSON-Schema
// subset; no package callback or executable code is involved.

function clone(value) {
  return structuredClone(value);
}

function boundedNumber(schema, integer) {
  let value = 0;
  if (schema.minimum !== undefined) value = Math.max(value, Number(schema.minimum));
  if (schema.maximum !== undefined) value = Math.min(value, Number(schema.maximum));
  return integer ? Math.ceil(value) : value;
}

export function schemaDefaultValue(schema) {
  if (Object.hasOwn(schema || {}, "const")) return clone(schema.const);
  if (Object.hasOwn(schema || {}, "default")) return clone(schema.default);
  if (Array.isArray(schema?.enum) && schema.enum.length) return clone(schema.enum[0]);

  if (schema?.type === "integer") return boundedNumber(schema, true);
  if (schema?.type === "number") return boundedNumber(schema, false);
  if (schema?.type === "boolean") return false;
  if (schema?.type === "string") return "x".repeat(schema.minLength || 0);
  if (schema?.type === "array") {
    return Array.from({ length: schema.minItems || 0 }, () => schemaDefaultValue(schema.items));
  }
  if (schema?.type === "object") {
    const required = new Set(schema.required || []);
    return Object.fromEntries(
      Object.entries(schema.properties || {})
        .filter(([key, property]) => required.has(key) || Object.hasOwn(property, "default"))
        .map(([key, property]) => [key, schemaDefaultValue(property)]),
    );
  }
  return "";
}

export function schemaConfigDefaults(schema) {
  if (schema?.type !== "object") return {};
  return schemaDefaultValue(schema);
}

export function parseStructuredConfigValue(raw) {
  try {
    const value = JSON.parse(raw);
    return { ok: true, value };
  } catch {
    return { ok: false };
  }
}

export function setConfigDraftPath(config, path, value) {
  if (!path.length) return;
  let target = config;
  for (const segment of path.slice(0, -1)) {
    const child = target[segment];
    if (!child || typeof child !== "object" || Array.isArray(child)) target[segment] = {};
    target = target[segment];
  }
  target[path.at(-1)] = value;
}
