// Frontend entry point for the "tts" workflow. loadWorkflowModules()
// dynamic-imports this file from the /static mount when the workflow is listed
// in the manifest. It registers the per-message create button, the attachment
// playback widget, the auto-play signal handler, and the config panel, and
// loads the workflow's global config slot into a snapshot the widget and panel
// share by reference.

import {
  api,
  registerAttachmentRenderer,
  registerWorkflowEventHandler,
  registerWorkflowMessageButton,
  registerWorkflowToolsPanelCard,
} from "/static/workflow_api.js";
import { configPanelRenderer, initConfigPanel } from "./config_panel.js";
import { initKaraoke } from "./karaoke.js";
import { attachmentRenderer, autoplayHandler, createButtonRenderer, initWidget } from "./widget.js";

const WORKFLOW_ID = "tts";

// Ship the workflow's stylesheet by injecting a <link> once, rather than
// editing the core stylesheet -- the /static mount serves it alongside this
// module.
function injectStyles() {
  if (document.getElementById("tts-workflow-styles")) return;
  const link = document.createElement("link");
  link.id = "tts-workflow-styles";
  link.rel = "stylesheet";
  link.href = `/static/workflows/${WORKFLOW_ID}/tts.css`;
  document.head.appendChild(link);
}

// The config slot is stored as a full replacement (not merged with defaults),
// so a persisted slot may lack these keys; defaulting them here to the same
// values as the backend config_defaults keeps that case consistent.
const config = {
  auto_play: false,
  volume: 0.75,
  click_granularity: "block",
  click_play_scope: "unit",
  show_karaoke: true,
};

async function loadConfig() {
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/config`);
    const c = res?.config || {};
    if (typeof c.auto_play === "boolean") config.auto_play = c.auto_play;
    if (typeof c.volume === "number") config.volume = c.volume;
    if (typeof c.click_granularity === "string") config.click_granularity = c.click_granularity;
    if (typeof c.click_play_scope === "string") config.click_play_scope = c.click_play_scope;
    if (typeof c.show_karaoke === "boolean") config.show_karaoke = c.show_karaoke;
  } catch (e) {
    console.warn("tts: config load failed", e);
  }
}

injectStyles();
initWidget(config);
initKaraoke(config);
initConfigPanel(config);

registerWorkflowMessageButton(WORKFLOW_ID, createButtonRenderer);
// The attachment renderer is a consumption surface (it replays already-produced
// bytes), so it is never gated by the toggle (registerAttachmentRenderer is
// ungated by design).
registerAttachmentRenderer(WORKFLOW_ID, attachmentRenderer);
registerWorkflowToolsPanelCard(WORKFLOW_ID, configPanelRenderer);
registerWorkflowEventHandler(WORKFLOW_ID, `${WORKFLOW_ID}_autoplay`, autoplayHandler);

loadConfig();
