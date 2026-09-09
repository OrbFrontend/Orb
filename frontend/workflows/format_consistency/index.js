import {
  api,
  getManifestEntry,
  localMlReady,
  registerAction,
  registerWorkflowToolsPanelCard,
} from "/static/workflow_api.js";

const WORKFLOW_ID = "format_consistency";
const FEATURE = "pov_classifier";
const CARD_BODY_ID = "fc-card-config";

// Seeded from the manifest the loader already has, so the card can register
// before any request goes out. Filled in by the load below and read
// synchronously by the renderer, which the tools panel calls on every repaint.
const state = { voiceOn: Boolean(getManifestEntry(WORKFLOW_ID)?.config_defaults?.voice_consistency) };

async function load() {
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/config`);
    state.voiceOn = Boolean(res?.config?.voice_consistency);
  } catch (e) {
    console.warn("format_consistency: config load failed", e);
  }
}

// The panel may not be open yet, and that is fine: the renderer reads `state`
// at paint time, so a later open picks the value up on its own.
function refreshCard() {
  const el = document.getElementById(CARD_BODY_ID);
  if (el) el.innerHTML = cardBody();
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

function cardBody() {
  return `<label class="lg-enforce-label" title="Also match the point of view and tense of replies to your recent messages. Costs one LLM call on turns that drift.">
        <input type="checkbox"${state.voiceOn ? " checked" : ""} data-wf-action="${WORKFLOW_ID}:toggleVoice" data-wf-on="change">
        Also keep POV and tense
      </label>${
        // Read at render time, not cached from a load-time fetch: the Local ML
        // card repaints this panel when Auto-POV is downloaded or toggled, so
        // the precondition line clears the moment the model is usable.
        localMlReady(FEATURE)
          ? ""
          : `<div class="tool-card-desc"><em>Auto-POV model not enabled — check in Settings under Local ML.</em></div>`
      }`;
}

registerAction(WORKFLOW_ID, "toggleVoice", (el) => toggleVoice(el));

registerWorkflowToolsPanelCard(
  WORKFLOW_ID,
  () =>
    `<div class="tool-card-desc">Keeps quotes and *asterisks* in replies consistent with the style of your recent messages.</div>
    <div class="lg-config" id="${CARD_BODY_ID}">${cardBody()}</div>`,
);

// Registration is synchronous and the config request is not awaited at module
// scope: workflow_loader.js imports these modules one after another, so a slow
// or hung /config call here used to hold up every workflow behind it -- image
// generation included -- rather than just this one checkbox.
load().then(refreshCard);
