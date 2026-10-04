// Validators return { valid, error? } so forms can share one error path.

const MAX_CHAT_INPUT = 100000;
const MAX_CHARACTER_NAME = 200;
const MAX_CHARACTER_FIELD = 100000;
const MAX_CHARACTER_ADVANCED = 5000;
const MAX_ALT_GREETING = 10000;
const MAX_ALT_GREETINGS_COUNT = 30;
const MAX_FRAGMENT_ID = 64;
const MAX_FRAGMENT_LABEL = 100;
const MAX_FRAGMENT_DESCRIPTION = 1000;
const MAX_FRAGMENT_PROMPT = 10000;
const MAX_FRAGMENT_NEGATIVE_PROMPT = 5000;
const MAX_SETTINGS_PROMPT = 50000;
const MAX_PERSONA_NAME = 50;
const MAX_PERSONA_DESC = 1000;
const MAX_PHRASE_VARIANT = 100;
// Mirrors MAX_PHRASE_REGEX in backend/analysis/detectors/slop_detector.py. A
// suggested shape with two pronoun slots runs past the literal variant cap.
const MAX_PHRASE_REGEX = 300;
const MAX_BROWSE_SEARCH = 200;
const MAX_CONVERSATION_TITLE = 100;
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

function boundedRequired(value, max, label, emptyError) {
  const trimmed = (value || "").trim();
  if (!trimmed) return { valid: false, error: emptyError };
  return trimmed.length > max ? { valid: false, error: `${label} must be ${max} characters or less` } : { valid: true };
}

export function validateChatInput(value) {
  return boundedRequired(value, MAX_CHAT_INPUT, "Message", "Message cannot be empty");
}

function validateCharacterName(value) {
  return boundedRequired(value, MAX_CHARACTER_NAME, "Character name", "Character name is required");
}

function validateCharacterField(value, fieldName = "Field") {
  return maxLength(value, MAX_CHARACTER_FIELD, fieldName);
}

function validateCharacterAdvancedField(value, fieldName = "Field") {
  return maxLength(value, MAX_CHARACTER_ADVANCED, fieldName);
}

function validateAlternateGreetings(greetings) {
  if (!Array.isArray(greetings)) return { valid: true };

  const valid = greetings.filter((g) => typeof g === "string" && g.trim());

  if (valid.length > MAX_ALT_GREETINGS_COUNT) {
    return { valid: false, error: `Maximum ${MAX_ALT_GREETINGS_COUNT} alternate greetings allowed` };
  }

  for (let i = 0; i < greetings.length; i++) {
    const g = greetings[i];
    if (typeof g === "string" && g.trim()) {
      if (g.length > MAX_ALT_GREETING) {
        return { valid: false, error: `Alternate greeting ${i + 1} must be ${MAX_ALT_GREETING} characters or less` };
      }
    }
  }

  return { valid: true };
}

const FRAGMENT_TEXT_FIELDS = {
  id: [MAX_FRAGMENT_ID, "ID"],
  label: [MAX_FRAGMENT_LABEL, "Label"],
  description: [MAX_FRAGMENT_DESCRIPTION, "Description"],
  prompt_text: [MAX_FRAGMENT_PROMPT, "Prompt text"],
  negative_prompt: [MAX_FRAGMENT_NEGATIVE_PROMPT, "Negative prompt"],
  injection_label: [MAX_FRAGMENT_LABEL, "Injection label"],
};

function fragmentText(data, keys, requiredKeys) {
  const values = Object.fromEntries(keys.map((key) => [key, (data[key] || "").trim()]));
  for (const key of requiredKeys) {
    if (!values[key]) {
      const label = key === "id" ? "Fragment ID" : FRAGMENT_TEXT_FIELDS[key][1];
      return { valid: false, error: `${label} is required` };
    }
  }
  const idCheck = patternMatch(
    values.id,
    FRAGMENT_ID_REGEX,
    "ID",
    "lowercase letters, numbers, hyphens, and underscores (must start with letter or number)",
  );
  if (!idCheck.valid) return idCheck;
  for (const key of keys) {
    const length = maxLength(values[key], ...FRAGMENT_TEXT_FIELDS[key]);
    if (!length.valid) return length;
  }
  return { valid: true };
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
  return text.valid ? integerRange(data.cooldown_turns, 0, 50, "Cooldown") : text;
}

const FRAGMENT_FIELD_TYPES = ["string", "array", "state", "feedback", "post_processing", "decision"];
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

  const cooldown = integerRange(data.cooldown_turns, 0, 50, "Cooldown");
  return cooldown.valid ? integerRange(data.post_processing_gate_replies, 0, 10, "History") : cooldown;
}

const TEXT_SETTINGS = {
  api_key: [1024, "API Key"],
  model_name: [256, "Model name"],
  system_prompt: [MAX_SETTINGS_PROMPT, "System prompt"],
  reasoning_effort_param: [128, "Reasoning param name"],
  reasoning_effort_value: [4096, "Reasoning param value"],
  extra_headers: [4096, "Extra request headers"],
  extra_body: [4096, "Extra request body"],
};

const NUMBER_SETTINGS = {
  temperature: [0, 2, "Temperature"],
  max_tokens: [64, 32768, "Max tokens", true],
  top_p: [0, 1, "Top P"],
  min_p: [0, 1, "Min P"],
  top_k: [0, 200, "Top K", true],
  repetition_penalty: [1, 2, "Repetition penalty"],
  length_guard_max_words: [50, 4000, "Max words", true],
  length_guard_max_paragraphs: [1, 20, "Max paragraphs", true],
};

function boundedNumber(value, min, max, label, integer = false) {
  const number = isNumber(value, label);
  if (!number.valid) return number;
  const range = numberRange(number.parsed, min, max, label);
  return !range.valid || !integer ? range : isInteger(number.parsed, label);
}

export function validateSetting(key, value) {
  if (key === "endpoint_url") {
    return value === "claude-code://local" ? { valid: true } : formatMatch(value, "Endpoint URL", "url");
  }
  if (typeof key !== "string") return { valid: true };
  if (Object.hasOwn(TEXT_SETTINGS, key)) return maxLength(value, ...TEXT_SETTINGS[key]);
  if (Object.hasOwn(NUMBER_SETTINGS, key)) return boundedNumber(value, ...NUMBER_SETTINGS[key]);
  return { valid: true };
}

function validatePersona(name, description) {
  const named = boundedRequired(name, MAX_PERSONA_NAME, "Name", "Persona name is required");
  return named.valid ? maxLength(description, MAX_PERSONA_DESC, "Description") : named;
}

function validatePhraseVariants(variants) {
  if (!Array.isArray(variants)) return { valid: true };

  const validVariants = variants.filter((v) => typeof v === "string" && v.trim());

  if (validVariants.length === 0) {
    return { valid: false, error: "At least one variant is required" };
  }

  for (let i = 0; i < variants.length; i++) {
    const v = variants[i];
    if (typeof v === "string" && v.trim()) {
      if (v.length > MAX_PHRASE_VARIANT) {
        return { valid: false, error: `Variant ${i + 1} must be ${MAX_PHRASE_VARIANT} characters or less` };
      }
    }
  }

  return { valid: true };
}

function validatePhraseRegex(pattern) {
  const src = (pattern || "").trim();
  if (!src) {
    return { valid: false, error: "A regex pattern is required" };
  }
  if (src.length > MAX_PHRASE_REGEX) {
    return { valid: false, error: `Pattern must be ${MAX_PHRASE_REGEX} characters or less` };
  }
  try {
    new RegExp(src);
    return { valid: true };
  } catch (e) {
    return { valid: false, error: e.message };
  }
}

function validateBrowseSearch(query) {
  return maxLength(query, MAX_BROWSE_SEARCH, "Search query");
}

export function validateConversationTitle(value) {
  return boundedRequired(value, MAX_CONVERSATION_TITLE, "Title", "Title cannot be empty");
}

export const validateEditMessage = validateChatInput;

export const validate = {
  validateImageFile,
  validateImageFiles,
  validateChatInput,
  validateCharacterName,
  validateCharacterField,
  validateCharacterAdvancedField,
  validateAlternateGreetings,
  validateMoodFragment,
  validateInteractiveFragment,
  validateSetting,
  validatePersona,
  validatePhraseVariants,
  validatePhraseRegex,
  validateBrowseSearch,
  validateEditMessage,
  validateConversationTitle,
};
