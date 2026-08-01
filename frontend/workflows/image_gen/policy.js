// DOM-free, facade-free policy predicates shared by the settings panel.
//
// Separate from config_panel.js for the same reason render.js is separate from
// widget.js: anything importing the plugin facade pulls in the chat spine and
// touches the DOM at load, so it cannot be exercised under `node --test`. These
// rules decide what the user is warned about, which is exactly the kind of thing
// that should have a test.

// Loopback gets no privacy banner: none of the warning's claims — your prompts
// leave this machine, other clients can read the queue, files stay on that disk
// — describe a boundary being crossed when the server is this machine. A warning
// shown on every configuration is one users learn to click through.
export function isLoopbackUrl(apiUrl) {
  let parsed;
  try {
    parsed = new URL(apiUrl);
  } catch {
    // An unparseable URL is rejected by the backend normalizer before it can
    // reach a server, so there is no remote boundary to warn about.
    return true;
  }
  // URL.hostname keeps the brackets on an IPv6 literal, so `[::1]` is the form
  // that actually arrives here; comparing against a bare `::1` never matches.
  const host = parsed.hostname.toLowerCase().replace(/^\[/, "").replace(/\]$/, "");
  return host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "0:0:0:0:0:0:0:1";
}

// Prompt formats, mirroring backend config.PROMPT_FORMATS. The format decides how
// the composer writes the scene -- booru tags, mixed, or plain sentences -- so two
// styles with the same name produce very different prompts. Both pickers name it
// beside the style rather than only inside the style's own form.
export const PROMPT_FORMATS = [
  ["tags", "Tags"],
  ["hybrid", "Hybrid"],
  ["prose", "Prose"],
];
export const DEFAULT_PROMPT_FORMAT = "hybrid";

// What a stored value actually means, mirroring backend `_normalize_prompt_format`:
// anything unknown or missing renders as the default, because that is what the
// backend substitutes for it. Every surface reads the format through here, so the
// picker, the summary and the card can never disagree about a style's format.
export function normalizePromptFormat(value) {
  return PROMPT_FORMATS.some(([id]) => id === value) ? value : DEFAULT_PROMPT_FORMAT;
}

export function promptFormatLabel(value) {
  const id = normalizePromptFormat(value);
  return PROMPT_FORMATS.find(([f]) => f === id)[1];
}

// Camera modes, mirroring backend pov.POV_MODES. "auto" runs the local POV
// classifier; the other two pin the camera by hand. Global, like the style: it
// lives in the workflow config, not on a conversation.
export const POV_MODES = [
  ["auto", "Auto"],
  ["first", "First-person"],
  ["third", "Third-person"],
];

// What the picker offers, and which entry is selected.
//
// Without the classifier, "Auto" is a second name for the fallback camera --
// `pov.resolve` degrades it to exactly that -- so offering both would be two
// options that draw the same shot. Auto is dropped and the fallback shown in its
// place, which is the camera the next image will actually use.
//
// A config already set to "auto" keeps that stored value; the coerced selection
// is display only, and nothing writes until the user picks something.
// So installing or re-enabling the classifier brings Auto back, still selected.
export function povChoices({ classifier, mode, fallback }) {
  if (classifier) return { modes: POV_MODES, selected: mode };
  return {
    modes: POV_MODES.filter(([id]) => id !== "auto"),
    selected: mode === "auto" ? fallback : mode,
  };
}
