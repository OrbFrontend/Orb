// Validators return { valid, error? } so forms can share one error path.

const MAX_FRAGMENT_ID = 64;
const MAX_IMAGE_SIZE = 10 * 1024 * 1024; // 10 MB
const ALLOWED_IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const FRAGMENT_ID_REGEX = /^[a-z0-9][a-z0-9_-]*$/;
const VALID_URL_REGEX = /^https?:\/\/.+$/;

function maxLength(value, max, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length > max) {
    return { valid: false, error: `${fieldName} must be ${max} characters or less` };
  }
  return { valid: true };
}

function isNumber(value, fieldName = "Field") {
  if (value === "" || value == null) return { valid: true };
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (Number.isNaN(num)) {
    return { valid: false, error: `${fieldName} must be a valid number` };
  }
  return { valid: true, parsed: num };
}

function numberRange(value, min, max, fieldName = "Field") {
  if (typeof value !== "number" || Number.isNaN(value)) return { valid: true };
  if (value < min || value > max) {
    return { valid: false, error: `${fieldName} must be between ${min} and ${max}` };
  }
  return { valid: true };
}

function isInteger(value, fieldName = "Field") {
  if (typeof value !== "number" || Number.isNaN(value)) return { valid: true };
  if (!Number.isInteger(value)) {
    return { valid: false, error: `${fieldName} must be a whole number` };
  }
  return { valid: true };
}

function formatMatch(value, _fieldName, format = "url") {
  if (typeof value !== "string" || !value.trim()) return { valid: true };
  const regex = format === "url" ? VALID_URL_REGEX : /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  if (!regex.test(value.trim())) {
    return { valid: false, error: `Please enter a valid ${format}` };
  }
  return { valid: true };
}

function patternMatch(value, regex, fieldName, hint) {
  if (typeof value !== "string" || !value.trim()) return { valid: true };
  if (!regex.test(value.trim())) {
    return { valid: false, error: `${fieldName} must match format: ${hint}` };
  }
  return { valid: true };
}

function validateImageFile(file, maxSize = MAX_IMAGE_SIZE, allowedMimes = ALLOWED_IMAGE_MIMES) {
  if (!file) {
    return { valid: false, error: "No file selected" };
  }

  if (!allowedMimes.includes(file.type)) {
    return { valid: false, error: `Only ${allowedMimes.join(", ")} files are allowed` };
  }

  if (file.size > maxSize) {
    const mb = (maxSize / 1024 / 1024).toFixed(0);
    return { valid: false, error: `File size must be under ${mb} MB` };
  }

  return { valid: true };
}

function validateImageFiles(files, maxCount = 10, maxSize = MAX_IMAGE_SIZE, totalMaxSize = 20 * 1024 * 1024) {
  const warnings = [];

  if (!files || files.length === 0) {
    return { valid: false, error: "No files selected" };
  }

  if (files.length > maxCount) {
    return { valid: false, error: `Maximum ${maxCount} files allowed` };
  }

  let totalSize = 0;
  for (const file of files) {
    const fileValidation = validateImageFile(file, maxSize, ALLOWED_IMAGE_MIMES);
    if (!fileValidation.valid) {
      return fileValidation;
    }
    totalSize += file.size;
  }

  if (totalSize > totalMaxSize) {
    const mb = (totalMaxSize / 1024 / 1024).toFixed(0);
    return { valid: false, error: `Total attachment size must be under ${mb} MB` };
  }

  return { valid: true, warnings };
}

function required(value, error) {
  return (value || "").trim() ? { valid: true } : { valid: false, error };
}

function validateChatInput(value) {
  return required(value, "Message cannot be empty");
}

function validateCharacterName(value) {
  return required(value, "Character name is required");
}

const FRAGMENT_LABELS = {
  id: "Fragment ID",
  label: "Label",
  prompt_text: "Prompt text",
  injection_label: "Injection label",
};

function fragmentText(data, keys, requiredKeys) {
  const values = Object.fromEntries(keys.map((key) => [key, (data[key] || "").trim()]));
  for (const key of requiredKeys) {
    if (!values[key]) return { valid: false, error: `${FRAGMENT_LABELS[key]} is required` };
  }
  const idCheck = patternMatch(
    values.id,
    FRAGMENT_ID_REGEX,
    "ID",
    "lowercase letters, numbers, hyphens, and underscores (must start with letter or number)",
  );
  return idCheck.valid ? maxLength(values.id, MAX_FRAGMENT_ID, "ID") : idCheck;
}

function integerRange(value, min, max, label) {
  const range = numberRange(value, min, max, label);
  return range.valid ? isInteger(value, label) : range;
}

function validateMoodFragment(data) {
  const text = fragmentText(
    data,
    ["id", "label", "description", "prompt_text", "negative_prompt"],
    ["id", "label", "prompt_text"],
  );
  return text.valid ? integerRange(data.cooldown_turns, 0, Infinity, "Cooldown") : text;
}

const FRAGMENT_FIELD_TYPES = ["string", "array", "state", "feedback", "recast", "rewrite", "decision"];
const STATE_SETTINGS = {
  state_mode: ["value", "entries"],
  state_update: ["after_reply", "before_writer", "manual"],
  state_inject: ["off", "director", "writer", "both"],
};

function validateInteractiveFragment(data) {
  const text = fragmentText(
    data,
    ["id", "label", "injection_label", "description"],
    ["id", "label", "injection_label"],
  );
  if (!text.valid) return text;

  if (data.field_type !== undefined && !FRAGMENT_FIELD_TYPES.includes(data.field_type)) {
    return { valid: false, error: `Field type must be one of: ${FRAGMENT_FIELD_TYPES.join(", ")}` };
  }
  if (data.field_type === "state") {
    for (const [key, allowed] of Object.entries(STATE_SETTINGS)) {
      if (data[key] != null && !allowed.includes(data[key])) {
        return { valid: false, error: `${key} must be one of: ${allowed.join(", ")}` };
      }
    }
  }

  const maxEntries = integerRange(data.state_max_entries, 0, 1000, "Max entries");
  if (!maxEntries.valid) return maxEntries;
  const cooldown = integerRange(data.cooldown_turns, 0, Infinity, "Cooldown");
  return cooldown.valid ? integerRange(data.gate_replies, 0, 10, "History") : cooldown;
}

const NUMBER_SETTINGS = {
  temperature: [0, 2, "Temperature"],
  max_tokens: [64, Infinity, "Max tokens", true],
  top_p: [0, 1, "Top P"],
  min_p: [0, 1, "Min P"],
  top_k: [0, Infinity, "Top K", true],
  repetition_penalty: [1, 2, "Repetition penalty"],
  length_guard_max_words: [50, Infinity, "Max words", true],
  length_guard_max_paragraphs: [1, Infinity, "Max paragraphs", true],
};

function boundedNumber(value, min, max, label, integer = false) {
  const number = isNumber(value, label);
  if (!number.valid) return number;
  const range = numberRange(number.parsed, min, max, label);
  return !range.valid || !integer ? range : isInteger(number.parsed, label);
}

function validateSetting(key, value) {
  if (key === "endpoint_url") {
    return value === "claude-code://local" ? { valid: true } : formatMatch(value, "Endpoint URL", "url");
  }
  if (typeof key !== "string") return { valid: true };
  if (Object.hasOwn(NUMBER_SETTINGS, key)) return boundedNumber(value, ...NUMBER_SETTINGS[key]);
  return { valid: true };
}

function validatePersona(name) {
  return required(name, "Persona name is required");
}

function validatePhraseRegex(pattern) {
  const src = (pattern || "").trim();
  if (!src) {
    return { valid: false, error: "A regex pattern is required" };
  }
  try {
    new RegExp(src);
    return { valid: true };
  } catch (e) {
    return { valid: false, error: e.message };
  }
}

const validateEditMessage = validateChatInput;

export const validate = {
  validateImageFile,
  validateImageFiles,
  validateChatInput,
  validateCharacterName,
  validateMoodFragment,
  validateInteractiveFragment,
  validateSetting,
  validatePersona,
  validatePhraseRegex,
  validateEditMessage,
};
