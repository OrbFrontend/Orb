// Frontend entry for the prose_format_llm workflow. The framework owns the
// Tools-panel card frame (name + on/off toggle) and the Secondary reasoning
// rail; this module supplies the card body, the rail's pass list, and the
// settings modal wiring.

import { registerWorkflowPipeline, registerWorkflowToolsPanelCard } from "/static/workflow_api.js";
import { configCardRenderer, initConfigPanel } from "./config_panel.js";

const WORKFLOW_ID = "prose_format_llm";

// Ship the workflow's stylesheet via a guarded <link> rather than editing the
// core stylesheet; the /static mount serves it next to this module.
function injectStyles() {
  if (document.getElementById("pf-workflow-styles")) return;
  const link = document.createElement("link");
  link.id = "pf-workflow-styles";
  link.rel = "stylesheet";
  link.href = "/static/workflows/" + WORKFLOW_ID + "/prose_format.css";
  document.head.appendChild(link);
}

injectStyles();

// Pass ids must match the backend's reasoning pass_ids so the rail can route
// their deltas (see loop._rail / _forced).
registerWorkflowPipeline({
  id: WORKFLOW_ID,
  label: "Prose Format",
  passes: [
    { id: WORKFLOW_ID + ":analyze", label: "Analyze" },
    { id: WORKFLOW_ID + ":judge", label: "Judge" },
    { id: WORKFLOW_ID + ":enforce", label: "Enforce" },
  ],
});

initConfigPanel();
registerWorkflowToolsPanelCard(WORKFLOW_ID, configCardRenderer);
