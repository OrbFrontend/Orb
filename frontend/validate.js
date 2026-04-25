/**
 * Frontend Input Validation Module
 *
 * Provides reusable validation functions for all user inputs across the Orb application.
 * Each function returns { valid: boolean, error?: string } for consistent error handling.
 */

// ── Constants ──

const MAX_CHAT_INPUT = 10000;
const MAX_CHARACTER_NAME = 200;
const MAX_CHARACTER_FIELD = 10000;
const MAX_CHARACTER_ADVANCED = 5000;
const MAX_ALT_GREETING = 10000;
const MAX_ALT_GREETINGS_COUNT = 10;
const MAX_FRAGMENT_ID = 64;
const MAX_FRAGMENT_LABEL = 100;
const MAX_FRAGMENT_DESCRIPTION = 500;
const MAX_FRAGMENT_PROMPT = 10000;
const MAX_FRAGMENT_NEGATIVE_PROMPT = 5000;
const MAX_SETTINGS_TEXT = 2048;
const MAX_SETTINGS_PROMPT = 10000;
const MAX_USER_PROFILE_NAME = 100;
const MAX_USER_PROFILE_DESC = 2000;
const MAX_PERSONA_NAME = 100;
const MAX_PERSONA_DESC = 2000;
const MAX_PHRASE_VARIANT = 500;
const MAX_BROWSE_SEARCH = 200;
const MAX_CONVERSATION_TITLE = 200;
const MAX_IMAGE_SIZE = 10 * 1024 * 1024; // 10 MB
const MAX_AVATAR_SIZE = 5 * 1024 * 1024; // 5 MB
const MIN_AVATAR_DIMENSION = 200;
const ALLOWED_IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const FRAGMENT_ID_REGEX = /^[a-z0-9][a-z0-9_-]*$/;
const VALID_URL_REGEX = /^https?:\/\/.+$/;

// ── Generic Validators ──

/**
 * Validate that a string is not empty (after trimming).
 * @param {string} value - The value to check
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function required(value, fieldName = "Field") {
  const trimmed = typeof value === "string" ? value.trim() : "";
  if (!trimmed) {
    return { valid: false, error: `${fieldName} is required` };
  }
  return { valid: true };
}

/**
 * Validate maximum string length.
 * @param {string} value - The value to check
 * @param {number} max - Maximum allowed length
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function maxLength(value, max, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true }; // non-strings passed elsewhere
  if (value.length > max) {
    return { valid: false, error: `${fieldName} must be ${max} characters or less` };
  }
  return { valid: true };
}

/**
 * Validate minimum string length.
 * @param {string} value - The value to check
 * @param {number} min - Minimum required length
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function minLength(value, min, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length < min) {
    return { valid: false, error: `${fieldName} must be at least ${min} characters` };
  }
  return { valid: true };
}

/**
 * Validate that a value is a valid number (not NaN).
 * @param {number|string} value - The value to check
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function isNumber(value, fieldName = "Field") {
  if (value === "" || value == null) return { valid: true }; // handled by required/empty checks
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num)) {
    return { valid: false, error: `${fieldName} must be a valid number` };
  }
  return { valid: true, parsed: num };
}

/**
 * Validate a number is within a range.
 * @param {number} value - The parsed number value
 * @param {number} min - Minimum allowed value
 * @param {number} max - Maximum allowed value
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function numberRange(value, min, max, fieldName = "Field") {
  if (typeof value !== "number" || isNaN(value)) return { valid: true };
  if (value < min || value > max) {
    return { valid: false, error: `${fieldName} must be between ${min} and ${max}` };
  }
  return { valid: true };
}

/**
 * Validate that a value is a whole number (integer).
 * @param {number} value - The value to check
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function isInteger(value, fieldName = "Field") {
  if (typeof value !== "number" || isNaN(value)) return { valid: true };
  if (!Number.isInteger(value)) {
    return { valid: false, error: `${fieldName} must be a whole number` };
  }
  return { valid: true };
}

/**
 * Validate an email-like or URL format string.
 * @param {string} value - The value to check
 * @param {string} fieldName - Display name for error messages
 * @param {"url"|"email"} format - The format to validate against
 * @returns {{ valid: boolean, error?: string }}
 */
export function formatMatch(value, fieldName, format = "url") {
  if (typeof value !== "string" || !value.trim()) return { valid: true };
  const regex = format === "url" ? VALID_URL_REGEX : /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  if (!regex.test(value.trim())) {
    return { valid: false, error: `Please enter a valid ${format}` };
  }
  return { valid: true };
}

/**
 * Validate a string matches a regex pattern.
 * @param {string} value - The value to check
 * @param {RegExp} regex - The regex pattern
 * @param {string} fieldName - Display name for error messages
 * @param {string} hint - Human-readable format hint
 * @returns {{ valid: boolean, error?: string }}
 */
export function patternMatch(value, regex, fieldName, hint) {
  if (typeof value !== "string" || !value.trim()) return { valid: true };
  if (!regex.test(value.trim())) {
    return { valid: false, error: `${fieldName} must match format: ${hint}` };
  }
  return { valid: true };
}

/**
 * Validate an image file.
 * @param {File} file - The file to validate
 * @param {number} maxSize - Maximum file size in bytes
 * @param {string[]} allowedMimes - Allowed MIME types
 * @returns {{ valid: boolean, error?: string }}
 */
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

/**
 * Validate multiple image files.
 * @param {File[]} files - Array of files to validate
 * @param {number} maxCount - Maximum number of files
 * @param {number} maxSize - Maximum size per file
 * @param {number} totalMaxSize - Maximum total size for all files
 * @returns {{ valid: boolean, error?: string, warnings?: string[] }}
 */
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

// ── Domain-Specific Validators ──

/**
 * Validate a chat message.
 * @param {string} content - The message content
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateChatInput(content) {
  const trimmed = (content || "").trim();
  if (!trimmed) {
    return { valid: false, error: "Message cannot be empty" };
  }
  if (trimmed.length < 2) {
    return { valid: false, error: "Message is too short" };
  }
  if (trimmed.length > MAX_CHAT_INPUT) {
    return { valid: false, error: `Message must be ${MAX_CHAT_INPUT} characters or less` };
  }
  return { valid: true };
}

/**
 * Validate a character name.
 * @param {string} name - The character name
 * @returns {{ valid: boolean, error?: string }}
 */
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

/**
 * Validate a character text field (description, personality, scenario, first_mes, mes_example).
 * @param {string} value - The field value
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateCharacterField(value, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length > MAX_CHARACTER_FIELD) {
    return { valid: false, error: `${fieldName} must be ${MAX_CHARACTER_FIELD} characters or less` };
  }
  return { valid: true };
}

/**
 * Validate a character advanced field (system_prompt, post_history_instructions).
 * @param {string} value - The field value
 * @param {string} fieldName - Display name for error messages
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateCharacterAdvancedField(value, fieldName = "Field") {
  if (typeof value !== "string") return { valid: true };
  if (value.length > MAX_CHARACTER_ADVANCED) {
    return { valid: false, error: `${fieldName} must be ${MAX_CHARACTER_ADVANCED} characters or less` };
  }
  return { valid: true };
}

/**
 * Validate alternate greetings.
 * @param {string[]} greetings - Array of greeting strings
 * @returns {{ valid: boolean, error?: string }}
 */
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

/**
 * Validate a mood fragment.
 * @param {object} data - The fragment data
 * @returns {{ valid: boolean, error?: string }}
 */
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

/**
 * Validate a director fragment.
 * @param {object} data - The fragment data
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateDirectorFragment(data) {
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

  return { valid: true };
}

/**
 * Validate a settings field value.
 * @param {string} key - The settings key
 * @param {any} value - The value to validate
 * @returns {{ valid: boolean, error?: string }}
 */
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
      const range = numberRange(numCheck.parsed, 64, 8192, "Max tokens");
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
    default:
      return { valid: true };
  }
}

/**
 * Validate a user profile.
 * @param {string} name - The profile name
 * @param {string} description - The profile description
 * @returns {{ valid: boolean, error?: string }}
 */
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

/**
 * Validate a persona.
 * @param {string} name - The persona name
 * @param {string} description - The persona description
 * @returns {{ valid: boolean, error?: string }}
 */
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

/**
 * Validate phrase bank variants.
 * @param {string[]} variants - Array of variant strings
 * @returns {{ valid: boolean, error?: string }}
 */
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

/**
 * Validate character browser search query.
 * @param {string} query - The search query
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateBrowseSearch(query) {
  if (typeof query !== "string") return { valid: true };
  if (query.length > MAX_BROWSE_SEARCH) {
    return { valid: false, error: `Search query must be ${MAX_BROWSE_SEARCH} characters or less` };
  }
  return { valid: true };
}

/**
 * Validate a conversation title.
 * @param {string} title - The conversation title
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateConversationTitle(title) {
  const trimmed = (title || "").trim();
  if (!trimmed) return { valid: false, error: "Title cannot be empty" };
  if (trimmed.length > MAX_CONVERSATION_TITLE) {
    return { valid: false, error: `Title must be ${MAX_CONVERSATION_TITLE} characters or less` };
  }
  return { valid: true };
}

/**
 * Validate an edit message.
 * @param {string} content - The message content
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateEditMessage(content) {
  const trimmed = (content || "").trim();
  if (!trimmed) {
    return { valid: false, error: "Message cannot be empty" };
  }
  if (trimmed.length > MAX_CHAT_INPUT) {
    return { valid: false, error: `Message must be ${MAX_CHAT_INPUT} characters or less` };
  }
  return { valid: true };
}

// ── Export all validators to window for inline handler access ──

// Note: These are primarily for internal module use, but exposed for
// any inline onclick handlers that may need direct access.
export const validate = {
  // Generic
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
  // Domain-specific
  validateChatInput,
  validateCharacterName,
  validateCharacterField,
  validateCharacterAdvancedField,
  validateAlternateGreetings,
  validateMoodFragment,
  validateDirectorFragment,
  validateSetting,
  validateUserProfile,
  validatePersona,
  validatePhraseVariants,
  validateBrowseSearch,
  validateEditMessage,
  validateConversationTitle,
};
