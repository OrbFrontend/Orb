import { api, localMlReady, registerAction, registerWorkflowToolsPanelCard } from "/static/workflow_api.js";

const WORKFLOW_ID = "format_consistency";
const FEATURE = "pov_classifier";

// Filled by the load below and read synchronously by the renderer, which the
// tools panel calls on every repaint (the tts card's shape).
const state = { voiceOn: false };

async function load() {
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/config`);
    state.voiceOn = Boolean(res?.config?.voice_consistency);
  } catch (e) {
    console.warn("format_consistency: config load failed", e);
  }
}

async function toggleVoice(el) {
  const wanted = el.checked;
  try {
    await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: { voice_consistency: wanted } });
    state.voiceOn = wanted;
  } catch (e) {
    console.warn("format_consistency: config save failed", e);
    el.checked = state.voiceOn;
  }
}

registerAction(WORKFLOW_ID, "toggleVoice", (el) => toggleVoice(el));

// lg-config / lg-enforce-label are the tools panel's shared sub-option row (Length
// Guard's "Enforce", the editor's "Show diff highlights", Local ML's "Run on GPU"):
// a terse label with the long form in its title, not a sentence in the desc style.
registerWorkflowToolsPanelCard(
  WORKFLOW_ID,
  () =>
    `<div class="tool-card-desc">Keeps quotes and *asterisks* in replies consistent with the style of your recent messages.</div>
    <div class="lg-config">
      <label class="lg-enforce-label" title="Also match the point of view and tense of replies to your recent messages. Costs one LLM call on turns that drift.">
        <input type="checkbox"${state.voiceOn ? " checked" : ""} data-wf-action="${WORKFLOW_ID}:toggleVoice" data-wf-on="change">
        Also keep POV and tense
      </label>${
        // Read at render time, not cached from a load-time fetch: the Local ML
        // card repaints this panel when Auto-POV is downloaded or toggled, so
        // the precondition line clears the moment the model is usable.
        localMlReady(FEATURE)
          ? ""
          : `<div class="tool-card-desc"><em>Auto-POV model not enabled — check in Settings under Local ML.</em></div>`
      }
    </div>`,
);

load();
