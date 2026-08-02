import {
  api,
  registerAttachmentRenderer,
  registerWorkflowMessageButton,
  registerWorkflowToolsPanelCard,
} from "/static/workflow_api.js";
import { configPanelRenderer, initConfigPanel, refreshCardReadiness, refreshCardStyles } from "./config_panel.js";
import { attachmentRenderer, createButtonRenderer, initWidget } from "./widget.js";

const WORKFLOW_ID = "image_gen";
// Mirrors backend CONFIG_DEFAULTS. Shared by reference into initWidget and
// initConfigPanel, which run at module load — before loadConfig resolves, and
// instead of it when that fetch throws — so every block the panel reads must exist
// here or it reads `undefined` off the literal.
const config = {
  source: "external_comfy",
  default_style: "realistic",
  styles: [],
  pov_mode: "auto",
  scene_analysis: false,
  prompter_reasoning: false,
  timeout_seconds: 180,
  external_comfy: {
    api_url: "http://127.0.0.1:8188",
    api_key: "",
    user_graphs: [],
  },
  cloud: {
    provider: "xai",
    width: 1024,
    height: 1024,
    quality: "",
    reference_source: "",
    providers: {},
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
    // One guard per nested block: Object.assign copies a malformed value through
    // verbatim, and the panel dereferences all three without checking.
    if (!config.external_comfy || typeof config.external_comfy !== "object") config.external_comfy = {};
    if (!config.cloud || typeof config.cloud !== "object") config.cloud = {};
    if (!Array.isArray(config.styles)) config.styles = [];
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
// Readiness and the style list are cached into the card renderer, so prime them
// once at load rather than leaving the first tools-panel open to paint empty and
// fill in.
loadConfig().then(() => {
  refreshCardReadiness();
  refreshCardStyles();
});
