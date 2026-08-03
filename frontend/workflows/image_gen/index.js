import {
  api,
  getManifestEntry,
  registerAttachmentRenderer,
  registerWorkflowMessageButton,
  registerWorkflowToolsPanelCard,
} from "/static/workflow_api.js";
import { configPanelRenderer, initConfigPanel, refreshCardReadiness, refreshCardStyles } from "./config_panel.js";
import { attachmentRenderer, createButtonRenderer, initWidget } from "./widget.js";

const WORKFLOW_ID = "image_gen";
const config = structuredClone(getManifestEntry(WORKFLOW_ID)?.config_defaults || {});

function injectStyles() {
  if (document.getElementById("image-gen-workflow-styles")) return;
  const link = document.createElement("link");
  link.id = "image-gen-workflow-styles";
  link.rel = "stylesheet";
  link.href = `/static/workflows/${WORKFLOW_ID}/image_gen.css`;
  document.head.appendChild(link);
}

async function loadConfig() {
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/config`);
    const c = res?.config;
    if (c && typeof c === "object") Object.assign(config, c);
  } catch (e) {
    console.warn("image_gen config load failed", e);
  }
}

injectStyles();
initWidget(config);
initConfigPanel(config);
registerWorkflowMessageButton(WORKFLOW_ID, createButtonRenderer);
registerAttachmentRenderer(WORKFLOW_ID, attachmentRenderer);
registerWorkflowToolsPanelCard(WORKFLOW_ID, configPanelRenderer);
loadConfig().then(() => {
  refreshCardReadiness();
  refreshCardStyles();
});
