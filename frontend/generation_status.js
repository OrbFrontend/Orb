// Status-bar text for the core pipeline steps, keyed by the ids the backend
// sends in `step_start` (plus `director_start`). Workflow hooks describe their
// own step through a `phase_status` label.
const STEP_LABELS = {
  director: "Directing the scene…",
  lorebook: "Consulting the lorebook…",
  direction_notes: "Updating direction notes…",
  writer: "Writing the reply…",
  output_auditor: "Auditing the draft…",
  length_guard: "Checking the length…",
  post_processing: "Applying post-processing…",
  feedback: "Preparing feedback…",
  world_changes: "Checking for world changes…",
  sheet_updates: "Reviewing character sheets…",
};

// Shown from the request until the first step starts.
export const WAITING_LABEL = "Waiting for response…";

export function generationStepLabel(step) {
  return Object.hasOwn(STEP_LABELS, step) ? STEP_LABELS[step] : "";
}
