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

// Seed from the manifest so registration does not wait for the config request.
const state = { voiceOn: Boolean(getManifestEntry(WORKFLOW_ID)?.config_defaults?.voice_consistency) };

async function load() {
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/config`);
    state.voiceOn = Boolean(res?.config?.voice_consistency);
  } catch (e) {
    console.warn("format_consistency: config load failed", e);
  }
}

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
        // Read at render time because the Local ML card can repaint this panel.
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

// Do not await config at module scope; one slow workflow must not block others.
load().then(refreshCard);
