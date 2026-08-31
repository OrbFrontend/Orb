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
const MAX_USER_PROFILE_NAME = 50;
const MAX_USER_PROFILE_DESC = 1000;
const MAX_PERSONA_NAME = 50;
const MAX_PERSONA_DESC = 1000;
const MAX_PHRASE_VARIANT = 100;
const MAX_BROWSE_SEARCH = 200;
const MAX_CONVERSATION_TITLE = 100;
const MAX_IMAGE_SIZE = 10 * 1024 * 1024; // 10 MB
const ALLOWED_IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const FRAGMENT_ID_REGEX = /^[a-z0-9][a-z0-9_-]*$/;
const VALID_URL_REGEX = /^https?:\/\/.+$/;

export function required(value, fieldName = "Field") {
  const trimmed = typeof value === "string" ? value.trim() : "";
  if (!trimmed) {
    return { valid: false, error: `${fieldName} is required` };
  }
  return { valid: true };
}

export function maxLength(value, max, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length > max) {
    return { valid: false, error: `${fieldName} must be ${max} characters or less` };
  }
  return { valid: true };
}

export function minLength(value, min, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length < min) {
    return { valid: false, error: `${fieldName} must be at least ${min} characters` };
  }
  return { valid: true };
}

export function isNumber(value, fieldName = "Field") {
  if (value === "" || value == null) return { valid: true };
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (Number.isNaN(num)) {
    return { valid: false, error: `${fieldName} must be a valid number` };
  }
  return { valid: true, parsed: num };
}

export function numberRange(value, min, max, fieldName = "Field") {
  if (typeof value !== "number" || Number.isNaN(value)) return { valid: true };
  if (value < min || value > max) {
    return { valid: false, error: `${fieldName} must be between ${min} and ${max}` };
  }
  return { valid: true };
}

export function isInteger(value, fieldName = "Field") {
  if (typeof value !== "number" || Number.isNaN(value)) return { valid: true };
  if (!Number.isInteger(value)) {
    return { valid: false, error: `${fieldName} must be a whole number` };
  }
  return { valid: true };
}

export function formatMatch(value, _fieldName, format = "url") {
  if (typeof value !== "string" || !value.trim()) return { valid: true };
  const regex = format === "url" ? VALID_URL_REGEX : /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  if (!regex.test(value.trim())) {
    return { valid: false, error: `Please enter a valid ${format}` };
  }
  return { valid: true };
}

export function patternMatch(value, regex, fieldName, hint) {
  if (typeof value !== "string" || !value.trim()) return { valid: true };
  if (!regex.test(value.trim())) {
    return { valid: false, error: `${fieldName} must match format: ${hint}` };
  }
  return { valid: true };
}

export function validateImageFile(file, maxSize = MAX_IMAGE_SIZE, allowedMimes = ALLOWED_IMAGE_MIMES) {
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

export function validateImageFiles(files, maxCount = 10, maxSize = MAX_IMAGE_SIZE, totalMaxSize = 20 * 1024 * 1024) {
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

export function validateChatInput(content) {
  const trimmed = (content || "").trim();
  if (!trimmed) {
    return { valid: false, error: "Message cannot be empty" };
  }
  if (trimmed.length > MAX_CHAT_INPUT) {
    return { valid: false, error: `Message must be ${MAX_CHAT_INPUT} characters or less` };
  }
  return { valid: true };
}

export function validateCharacterName(name) {
  const trimmed = (name || "").trim();
  if (!trimmed) {
    return { valid: false, error: "Character name is required" };
  }
  if (trimmed.length > MAX_CHARACTER_NAME) {
    return { valid: false, error: `Character name must be ${MAX_CHARACTER_NAME} characters or less` };
  }
  return { valid: true };
}

export function validateCharacterField(value, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length > MAX_CHARACTER_FIELD) {
    return { valid: false, error: `${fieldName} must be ${MAX_CHARACTER_FIELD} characters or less` };
  }
  return { valid: true };
}

export function validateCharacterAdvancedField(value, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length > MAX_CHARACTER_ADVANCED) {
    return { valid: false, error: `${fieldName} must be ${MAX_CHARACTER_ADVANCED} characters or less` };
  }
  return { valid: true };
}

export function validateAlternateGreetings(greetings) {
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

export function validateMoodFragment(data) {
  const id = (data.id || "").trim();
  const label = (data.label || "").trim();
  const description = (data.description || "").trim();
  const promptText = (data.prompt_text || "").trim();
  const negativePrompt = (data.negative_prompt || "").trim();

  if (!id) return { valid: false, error: "Fragment ID is required" };
  if (!label) return { valid: false, error: "Label is required" };
  if (!promptText) return { valid: false, error: "Prompt text is required" };

  const idCheck = patternMatch(
    id,
    FRAGMENT_ID_REGEX,
    "ID",
    "lowercase letters, numbers, hyphens, and underscores (must start with letter or number)",
  );
  if (!idCheck.valid) return idCheck;

  const idLen = maxLength(id, MAX_FRAGMENT_ID, "ID");
  if (!idLen.valid) return idLen;

  const labelLen = maxLength(label, MAX_FRAGMENT_LABEL, "Label");
  if (!labelLen.valid) return labelLen;

  const descLen = maxLength(description, MAX_FRAGMENT_DESCRIPTION, "Description");
  if (!descLen.valid) return descLen;

  const promptLen = maxLength(promptText, MAX_FRAGMENT_PROMPT, "Prompt text");
  if (!promptLen.valid) return promptLen;

  const negLen = maxLength(negativePrompt, MAX_FRAGMENT_NEGATIVE_PROMPT, "Negative prompt");
  if (!negLen.valid) return negLen;

  return { valid: true };
}

const FRAGMENT_FIELD_TYPES = ["string", "array", "progressive", "feedback", "direction_note"];

export function validateInteractiveFragment(data) {
  const id = (data.id || "").trim();
  const label = (data.label || "").trim();
  const injectionLabel = (data.injection_label || "").trim();
  const description = (data.description || "").trim();

  if (!id) return { valid: false, error: "Fragment ID is required" };
  if (!label) return { valid: false, error: "Label is required" };
  if (!injectionLabel) return { valid: false, error: "Injection label is required" };

  const idCheck = patternMatch(
    id,
    FRAGMENT_ID_REGEX,
    "ID",
    "lowercase letters, numbers, hyphens, and underscores (must start with letter or number)",
  );
  if (!idCheck.valid) return idCheck;

  const idLen = maxLength(id, MAX_FRAGMENT_ID, "ID");
  if (!idLen.valid) return idLen;

  const labelLen = maxLength(label, MAX_FRAGMENT_LABEL, "Label");
  if (!labelLen.valid) return labelLen;

  const injLen = maxLength(injectionLabel, MAX_FRAGMENT_LABEL, "Injection label");
  if (!injLen.valid) return injLen;

  const descLen = maxLength(description, MAX_FRAGMENT_DESCRIPTION, "Description");
  if (!descLen.valid) return descLen;

  if (data.field_type !== undefined && !FRAGMENT_FIELD_TYPES.includes(data.field_type)) {
    return { valid: false, error: `Field type must be one of: ${FRAGMENT_FIELD_TYPES.join(", ")}` };
  }

  return { valid: true };
}

export function validateSetting(key, value) {
  switch (key) {
    case "endpoint_url": {
      if (typeof value === "string" && value.trim()) {
        return formatMatch(value, "Endpoint URL", "url");
      }
      return { valid: true };
    }
    case "api_key": {
      if (typeof value === "string") {
        return maxLength(value, 1024, "API Key");
      }
      return { valid: true };
    }
    case "model_name": {
      if (typeof value === "string") {
        return maxLength(value, 256, "Model name");
      }
      return { valid: true };
    }
    case "system_prompt": {
      if (typeof value === "string") {
        return maxLength(value, MAX_SETTINGS_PROMPT, "System prompt");
      }
      return { valid: true };
    }
    case "temperature": {
      const numCheck = isNumber(value, "Temperature");
      if (!numCheck.valid) return numCheck;
      return numberRange(numCheck.parsed, 0, 2, "Temperature");
    }
    case "max_tokens": {
      const numCheck = isNumber(value, "Max tokens");
      if (!numCheck.valid) return numCheck;
      const range = numberRange(numCheck.parsed, 64, 32768, "Max tokens");
      if (!range.valid) return range;
      return isInteger(numCheck.parsed, "Max tokens");
    }
    case "top_p": {
      const numCheck = isNumber(value, "Top P");
      if (!numCheck.valid) return numCheck;
      return numberRange(numCheck.parsed, 0, 1, "Top P");
    }
    case "min_p": {
      const numCheck = isNumber(value, "Min P");
      if (!numCheck.valid) return numCheck;
      return numberRange(numCheck.parsed, 0, 1, "Min P");
    }
    case "top_k": {
      const numCheck = isNumber(value, "Top K");
      if (!numCheck.valid) return numCheck;
      const range = numberRange(numCheck.parsed, 0, 200, "Top K");
      if (!range.valid) return range;
      return isInteger(numCheck.parsed, "Top K");
    }
    case "repetition_penalty": {
      const numCheck = isNumber(value, "Repetition penalty");
      if (!numCheck.valid) return numCheck;
      return numberRange(numCheck.parsed, 1, 2, "Repetition penalty");
    }
    case "length_guard_max_words": {
      const numCheck = isNumber(value, "Max words");
      if (!numCheck.valid) return numCheck;
      const range = numberRange(numCheck.parsed, 50, 4000, "Max words");
      if (!range.valid) return range;
      return isInteger(numCheck.parsed, "Max words");
    }
    case "length_guard_max_paragraphs": {
      const numCheck = isNumber(value, "Max paragraphs");
      if (!numCheck.valid) return numCheck;
      const range = numberRange(numCheck.parsed, 1, 20, "Max paragraphs");
      if (!range.valid) return range;
      return isInteger(numCheck.parsed, "Max paragraphs");
    }
    case "reasoning_effort_param": {
      if (typeof value === "string") {
        return maxLength(value, 128, "Reasoning param name");
      }
      return { valid: true };
    }
    case "reasoning_effort_value": {
      if (typeof value === "string") {
        return maxLength(value, 4096, "Reasoning param value");
      }
      return { valid: true };
    }
    case "extra_headers": {
      if (typeof value === "string") {
        return maxLength(value, 4096, "Extra request headers");
      }
      return { valid: true };
    }
    case "extra_body": {
      if (typeof value === "string") {
        return maxLength(value, 4096, "Extra request body");
      }
      return { valid: true };
    }
    default:
      return { valid: true };
  }
}

export function validateUserProfile(name, description) {
  const nameTrimmed = (name || "").trim();
  if (!nameTrimmed) {
    return { valid: false, error: "Name is required" };
  }
  if (nameTrimmed.length > MAX_USER_PROFILE_NAME) {
    return { valid: false, error: `Name must be ${MAX_USER_PROFILE_NAME} characters or less` };
  }

  if (typeof description === "string" && description.length > MAX_USER_PROFILE_DESC) {
    return { valid: false, error: `Description must be ${MAX_USER_PROFILE_DESC} characters or less` };
  }

  return { valid: true };
}

export function validatePersona(name, description) {
  const nameTrimmed = (name || "").trim();
  if (!nameTrimmed) {
    return { valid: false, error: "Persona name is required" };
  }
  if (nameTrimmed.length > MAX_PERSONA_NAME) {
    return { valid: false, error: `Name must be ${MAX_PERSONA_NAME} characters or less` };
  }

  if (typeof description === "string" && description.length > MAX_PERSONA_DESC) {
    return { valid: false, error: `Description must be ${MAX_PERSONA_DESC} characters or less` };
  }

  return { valid: true };
}

export function validatePhraseVariants(variants) {
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

export function validatePhraseRegex(pattern) {
  const src = (pattern || "").trim();
  if (!src) {
    return { valid: false, error: "A regex pattern is required" };
  }
  if (src.length > MAX_PHRASE_VARIANT) {
    return { valid: false, error: `Pattern must be ${MAX_PHRASE_VARIANT} characters or less` };
  }
  try {
    new RegExp(src);
    return { valid: true };
  } catch (e) {
    return { valid: false, error: e.message };
  }
}

export function validateBrowseSearch(query) {
  if (typeof query !== "string") return { valid: true };
  if (query.length > MAX_BROWSE_SEARCH) {
    return { valid: false, error: `Search query must be ${MAX_BROWSE_SEARCH} characters or less` };
  }
  return { valid: true };
}

export function validateConversationTitle(title) {
  const trimmed = (title || "").trim();
  if (!trimmed) return { valid: false, error: "Title cannot be empty" };
  if (trimmed.length > MAX_CONVERSATION_TITLE) {
    return { valid: false, error: `Title must be ${MAX_CONVERSATION_TITLE} characters or less` };
  }
  return { valid: true };
}

export const validateEditMessage = validateChatInput;

export const validate = {
  required,
  maxLength,
  minLength,
  isNumber,
  numberRange,
  isInteger,
  formatMatch,
  patternMatch,
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
  validateUserProfile,
  validatePersona,
  validatePhraseVariants,
  validatePhraseRegex,
  validateBrowseSearch,
  validateEditMessage,
  validateConversationTitle,
};
