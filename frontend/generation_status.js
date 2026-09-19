const PHASES = {
  pending: { label: "Preparing the request…", step: -1, stage: "" },
  directing: { label: "Reading context and planning the scene…", step: 0, stage: "director pass" },
  generating: { label: "Drafting the response…", step: 1, stage: "writer pass" },
  refining: { label: "Reviewing and polishing the response…", step: 2, stage: "editor pass" },
  finalizing: { label: "Finishing the response…", step: 2, stage: "workflow hook" },
};
const PHASE_NAMES = Object.keys(PHASES);
const FALLBACK = { label: "Processing…", step: -1, stage: "" };

export function generationPhaseView(phase) {
  return PHASES[phase] || FALLBACK;
}

export function generationPhaseIndex(phase) {
  return PHASE_NAMES.indexOf(phase);
}

export function renderGenerationPhase(el, phase) {
  const { label, step: activeStep } = generationPhaseView(phase);
  el.dataset.phase = phase;
  el.querySelector(".gen-text").textContent = label;
  el.querySelector(".gen-dot").className = `gen-dot${activeStep === 2 ? " spin" : ""}`;
  for (const [index, step] of el.querySelectorAll("[data-generation-step]").entries()) {
    step.dataset.state = index < activeStep ? "complete" : index === activeStep ? "active" : "upcoming";
  }
}
