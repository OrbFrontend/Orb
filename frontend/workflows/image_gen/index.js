import {
  api,
  registerAttachmentRenderer,
  registerWorkflowMessageButton,
  registerWorkflowToolsPanelCard,
} from "/static/workflow_api.js";
import { configPanelRenderer, initConfigPanel, refreshCardReadiness } from "./config_panel.js";
import { attachmentRenderer, createButtonRenderer, initWidget } from "./widget.js";

const WORKFLOW_ID = "image_gen";
const config = {
  source: "external_comfy",
  default_style: "realistic",
  scene_analysis: false,
  timeout_seconds: 180,
  external_comfy: {
    api_url: "http://127.0.0.1:8188",
    api_key: "",
    checkpoint: "",
    workflow: "external_core",
    styles: [],
    user_graphs: [],
  },
};

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
    if (!config.external_comfy || typeof config.external_comfy !== "object") config.external_comfy = {};
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
// Readiness is cached into the card renderer, so prime it once at load rather
// than leaving the first tools-panel open to paint an empty line and fill in.
loadConfig().then(refreshCardReadiness);
