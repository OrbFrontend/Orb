// Labels for backend step_start ids; director_start uses "director".
const STEP_LABELS = {
  director: "Directing the scene…",
  judge: "Judging decisions…",
  lorebook: "Consulting the lorebook…",
  state: "Updating state…",
  writer: "Writing the reply…",
  output_auditor: "Auditing the draft…",
  length_guard: "Checking the length…",
  subject_fixation: "Cutting repeated descriptions…",
  post_processing: "Applying post-processing…",
  feedback: "Preparing feedback…",
  world_changes: "Checking for world changes…",
  sheet_updates: "Reviewing character sheets…",
};

export const WAITING_LABEL = "Waiting for the model";

export function generationStepLabel(step) {
  return Object.hasOwn(STEP_LABELS, step) ? STEP_LABELS[step] : "";
}

const statusMarquees = new WeakMap();

// Keep short labels centered; long labels travel right to left at 36px/second.
// Observe both widths so workflow progress and panel resizing update the loop.
export function syncGenerationStatusMarquee(bar) {
  const viewport = bar.querySelector(".gen-status-content");
  const track = viewport?.querySelector(".gen-status-track");
  if (!track) return;
  let refresh = statusMarquees.get(bar);
  if (!refresh) {
    refresh = () => {
      const visibleWidth = viewport.clientWidth;
      const textWidth = track.offsetWidth;
      const scrolling = !bar.classList.contains("hidden") && visibleWidth > 0 && textWidth > visibleWidth;
      if (scrolling) {
        const start = visibleWidth + 24;
        viewport.style.setProperty("--gen-scroll-start", `${start}px`);
        viewport.style.setProperty("--gen-scroll-duration", `${(start + textWidth + 24) / 36}s`);
        // Start with the beginning visible, then loop in from the right edge.
        viewport.style.setProperty("--gen-scroll-delay", `${-start / 36}s`);
      }
      viewport.classList.toggle("is-scrolling", scrolling);
    };
    statusMarquees.set(bar, refresh);
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(refresh);
      observer.observe(viewport);
      observer.observe(track);
    }
  }
  refresh();
}
