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

function injectStyles() {
  if (document.getElementById("tts-workflow-styles")) return;
  const link = document.createElement("link");
  link.id = "tts-workflow-styles";
  link.rel = "stylesheet";
  link.href = `/static/workflows/${WORKFLOW_ID}/tts.css`;
  document.head.appendChild(link);
}

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
registerAttachmentRenderer(WORKFLOW_ID, attachmentRenderer);
registerWorkflowToolsPanelCard(WORKFLOW_ID, configPanelRenderer);
registerWorkflowEventHandler(WORKFLOW_ID, `${WORKFLOW_ID}_autoplay`, autoplayHandler);

loadConfig();
