// Labels for backend step_start ids; director_start uses "director".
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

export const WAITING_LABEL = "Waiting for response…";

export function generationStepLabel(step) {
  return Object.hasOwn(STEP_LABELS, step) ? STEP_LABELS[step] : "";
}
